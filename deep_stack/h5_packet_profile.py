"""Summarize observable shape of local binary capture packets without exporting bytes."""

from collections import Counter
from dataclasses import dataclass, field
from math import fsum, log2

from .h5_trace import read_binary_packets


_MAX_CONNECTIONS = 32
_MAX_SAMPLES_PER_DIRECTION = 4096
_WINDOW = 16
_MAX_PREFIX_OFFSET = 12
_WIDTHS = (1, 2, 4)
_MAX_DELTA = 64
_MIN_SUPPORT = 4
_MIN_DISTINCT_SIZES = 3
_MAX_HYPOTHESES = 16


@dataclass
class _DirectionSummary:
    sizes: Counter = field(default_factory=Counter)
    total_bytes: int = 0
    packet_count: int = 0
    windows: list[tuple[int, bytes, bytes]] = field(default_factory=list)

    def add(self, body):
        size = len(body)
        self.sizes[size] += 1
        self.total_bytes += size
        self.packet_count += 1
        if len(self.windows) < _MAX_SAMPLES_PER_DIRECTION:
            self.windows.append((size, body[:_WINDOW], body[-_WINDOW:]))


def _entropy(windows):
    counts = Counter(byte for value in windows for byte in value)
    size = counts.total()
    if not size:
        return None
    return round(-fsum((count / size) * log2(count / size) for count in counts.values()), 5)


def _window_counts(values):
    four = Counter(value for value in values if len(value) == 4)
    count = four.total()
    return {"eligible_packet_count": count, "distinct_count": len(four),
            "duplicate_occurrences": count - len(four), "repeated_value_count": sum(n > 1 for n in four.values())}


def _fingerprint_summary(windows, side):
    index = 1 if side == "first" else 2
    values = [item[index] for item in windows]
    full = [value for value in values if len(value) == _WINDOW]
    four = (value[:4] if side == "first" else value[-4:] for value in values)
    return {"four_byte_windows": _window_counts(four),
            "sixteen_byte_entropy": {"eligible_packet_count": len(full), "sampled_byte_count": len(full) * _WINDOW,
                                     "pooled_bits_per_byte": _entropy(full)}}


def _common_offsets(windows):
    if len(windows) < _MIN_SUPPORT:
        return []
    headers = [item[1] for item in windows]
    return [index for index, column in enumerate(zip(*headers)) if len(set(column)) == 1]


def _length_hypotheses(windows):
    hypotheses = []
    strongest_multi_size = 0
    for offset in range(_MAX_PREFIX_OFFSET + 1):
        for width in _WIDTHS:
            end = offset + width
            eligible = [(size, head[offset:end]) for size, head, _ in windows if len(head) >= end]
            for byte_order in (("big",) if width == 1 else ("big", "little")):
                counts = Counter()
                observed_sizes = {}
                for size, prefix in eligible:
                    delta = size - int.from_bytes(prefix, byte_order)
                    if abs(delta) <= _MAX_DELTA:
                        counts[delta] += 1
                        observed_sizes.setdefault(delta, set()).add(size)
                for delta, count in counts.items():
                    sizes = len(observed_sizes[delta])
                    if sizes > 1:
                        strongest_multi_size = max(strongest_multi_size, count)
                    if count >= _MIN_SUPPORT and sizes >= _MIN_DISTINCT_SIZES and count * 4 >= len(windows) * 3:
                        hypotheses.append({
                            "offset_bytes": offset, "width_bytes": width, "byte_order": byte_order,
                            "packet_size_minus_field_bytes": delta, "match_count": count,
                            "eligible_packet_count": len(eligible), "distinct_matching_sizes": sizes,
                            "observed_coverage": round(count / len(windows), 4),
                        })
    hypotheses.sort(key=lambda row: (-row["match_count"], -row["distinct_matching_sizes"], row["offset_bytes"],
                                     row["width_bytes"], row["byte_order"], row["packet_size_minus_field_bytes"]))
    return {"max_support_across_multiple_sizes": strongest_multi_size,
            "candidate_count": len(hypotheses),
            "omitted_candidate_count": max(0, len(hypotheses) - _MAX_HYPOTHESES),
            "candidates": hypotheses[:_MAX_HYPOTHESES]}


def _summary(connection, direction, group):
    windows = group.windows
    frequent_sizes = sorted(group.sizes.items(), key=lambda item: (-item[1], item[0]))[:8]
    return {"connection": connection, "direction": direction, "packet_count": group.packet_count,
            "inspected_packet_count": len(windows), "total_bytes": group.total_bytes,
            "size_bytes": {"min": min(group.sizes), "max": max(group.sizes), "distinct_count": len(group.sizes),
                           "most_frequent": [{"size_bytes": size, "count": count} for size, count in frequent_sizes]},
            "first": _fingerprint_summary(windows, "first"), "last": _fingerprint_summary(windows, "last"),
            "constant_first_window_offsets": _common_offsets(windows), "length_prefix": _length_hypotheses(windows)}


def profile_packets(path):
    """Return per-direction binary packet distributions and bounded layout hypotheses.

    The source parser decodes only in memory. Connection identifiers, packet bytes,
    byte values, and application event labels do not leave the profiler.
    """
    connections, groups = {}, {}
    total = excluded = 0
    for packet in read_binary_packets(path):
        total += 1
        connection = connections.get(packet.stream)
        if connection is None:
            if len(connections) >= _MAX_CONNECTIONS:
                excluded += 1
                continue
            connection = connections[packet.stream] = len(connections) + 1
        key = (connection, packet.direction)
        if key not in groups:
            groups[key] = _DirectionSummary()
        groups[key].add(packet.body)
    return {
        "format": "deep-stack-h5-packet-profile-v1",
        "binary_packet_count": total, "profiled_connection_count": len(connections),
        "unprofiled_connection_packet_count": excluded,
        "method": {
            "sample_order": "first_packets_per_connection_direction", "connection_limit": _MAX_CONNECTIONS,
            "sample_limit_per_direction": _MAX_SAMPLES_PER_DIRECTION, "header_window_bytes": _WINDOW,
            "prefix_offset_range_bytes": [0, _MAX_PREFIX_OFFSET], "prefix_width_bytes": list(_WIDTHS),
            "prefix_max_absolute_delta_bytes": _MAX_DELTA, "prefix_min_support": _MIN_SUPPORT,
            "prefix_min_distinct_sizes": _MIN_DISTINCT_SIZES, "prefix_min_observed_coverage": 0.75,
            "prefix_list_limit": _MAX_HYPOTHESES,
        },
        "directions": [_summary(f"ws-{connection}", direction, group)
                       for (connection, direction), group in sorted(groups.items())],
    }
