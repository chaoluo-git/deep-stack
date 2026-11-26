"""Local persistence for player identities and visible poker actions."""

from contextlib import contextmanager
from math import isfinite
from pathlib import Path
import re
import sqlite3
import unicodedata


_PLAYER_ID = re.compile(r"[0-9]*[1-9][0-9]*")
_STREETS = ("preflop", "flop", "turn", "river")
_ACTIONS = ("fold", "check", "call", "bet", "raise", "all_in")
_TERMINAL = {"fold", "all_in"}
_PROFILE_RATES = ("win_rate", "pot_entry_rate", "showdown_win_rate")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    player_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS seat_bindings (
    session_id TEXT NOT NULL,
    seat TEXT NOT NULL,
    player_id TEXT NOT NULL REFERENCES players(player_id),
    name TEXT,
    paused INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, seat),
    UNIQUE (session_id, player_id)
);
CREATE TABLE IF NOT EXISTS hand_seats (
    session_id TEXT NOT NULL,
    hand_id TEXT NOT NULL,
    seat TEXT NOT NULL,
    player_id TEXT NOT NULL REFERENCES players(player_id),
    PRIMARY KEY (session_id, hand_id, seat)
);
CREATE TABLE IF NOT EXISTS visible_actions (
    event_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    hand_id TEXT NOT NULL,
    seat TEXT NOT NULL,
    player_id TEXT NOT NULL REFERENCES players(player_id),
    street TEXT,
    kind TEXT NOT NULL,
    wager REAL,
    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id, hand_id, seat) REFERENCES hand_seats(session_id, hand_id, seat)
);
CREATE INDEX IF NOT EXISTS visible_actions_hand
    ON visible_actions(session_id, hand_id, seat, player_id, event_id);
CREATE INDEX IF NOT EXISTS visible_actions_player
    ON visible_actions(player_id, event_id);
CREATE TABLE IF NOT EXISTS confirmed_preflop (
    session_id TEXT NOT NULL,
    hand_id TEXT NOT NULL,
    player_id TEXT NOT NULL REFERENCES players(player_id),
    vpip INTEGER NOT NULL,
    pfr INTEGER NOT NULL,
    PRIMARY KEY (session_id, hand_id, player_id)
);
CREATE TABLE IF NOT EXISTS club_profiles (
    profile_id INTEGER PRIMARY KEY,
    player_id TEXT NOT NULL REFERENCES players(player_id),
    name TEXT,
    hands INTEGER NOT NULL,
    win_rate REAL NOT NULL,
    pot_entry_rate REAL NOT NULL,
    showdown_win_rate REAL NOT NULL,
    recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS club_profiles_player ON club_profiles(player_id, profile_id);
"""


def _key(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串")
    return value.strip()


def _player_id(value):
    if not isinstance(value, str) or _PLAYER_ID.fullmatch(value) is None:
        raise ValueError("玩家 ID 必须为正数字符串，前导零会保留")
    return value


def _name(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("昵称必须是字符串")
    return value.strip() or None


def _name_key(value):
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _reliable_name(frame, seat, name):
    if not name:
        return False
    evidence = frame.get("evidence")
    if evidence is None:
        # Explicit readings supplied directly by callers already name the seat.
        return True
    observations = evidence.get(f"{seat}.label", {}).get("observations", ())
    return any(
        isinstance(observation.get("text"), str)
        and _name_key(observation["text"]) == _name_key(name)
        and observation.get("confidence", 0) >= 0.85
        for observation in observations
    )


def _seat_readings(frame):
    if not isinstance(frame, dict) or not isinstance(frame.get("seats"), list):
        raise ValueError("观察帧需要座位列表 seats")
    seats = {}
    for reading in frame["seats"]:
        if not isinstance(reading, dict):
            raise ValueError("观察帧的座位读数必须是对象")
        seat = _key(reading.get("seat"), "座位")
        if seat in seats:
            raise ValueError(f"同一帧出现重复座位 {seat}")
        seats[seat] = reading
    return seats


def _visible_wager(value):
    if value is None:
        return None
    if type(value) not in (int, float) or not 0 <= value <= 1e12 or not isfinite(value):
        raise ValueError("可见本街投入必须是 0 到 10^12 之间的有限数字")
    return value


def _profile_stats(stats):
    if not isinstance(stats, dict) or stats.keys() != {"hands", *_PROFILE_RATES}:
        raise ValueError("画像须包含局数、胜率、入池率和摊牌胜率")
    hands = stats["hands"]
    if type(hands) is not int or not 0 <= hands <= 2**63 - 1:
        raise ValueError("画像局数必须是非负整数")
    result = {"hands": hands}
    for key in _PROFILE_RATES:
        value = stats[key]
        if type(value) not in (int, float) or not 0 <= value <= 1:
            raise ValueError(f"画像 {key} 必须为 0 到 1 之间的数字")
        result[key] = float(value)
    return result


def _history_limit(limit):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("记录条数必须是 1 到 1000 之间的整数")
    return limit


class PlayerStore:
    """Keep manual session bindings and observations in one SQLite file."""

    def __init__(self, path):
        # Prevent SQLite from interpreting a colon-prefixed filename as an in-memory database.
        self._db = sqlite3.connect(Path(path).expanduser().absolute(), timeout=10)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.executescript(_SCHEMA)
        except sqlite3.Error:
            self._db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def close(self):
        self._db.close()

    @contextmanager
    def _write(self):
        # Reserve the write lock before reading current bindings/action history.
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            yield

    def bind(self, session_id, seat, player_id, *, name=None):
        """Bind a confirmed ID, including a move from a paused seat."""
        session_id, seat = _key(session_id, "场次"), _key(seat, "座位")
        player_id, name = _player_id(player_id), _name(name)
        with self._write():
            existing = self._db.execute(
                "SELECT seat, paused FROM seat_bindings WHERE session_id = ? AND player_id = ?",
                (session_id, player_id),
            ).fetchone()
            if existing and existing["seat"] != seat:
                if not existing["paused"]:
                    raise ValueError(f"该 ID 已绑定本场次的 {existing['seat']}，请先解除旧座位绑定")
                self._db.execute(
                    "DELETE FROM seat_bindings WHERE session_id = ? AND seat = ?",
                    (session_id, existing["seat"]),
                )
            self._db.execute("INSERT OR IGNORE INTO players (player_id) VALUES (?)", (player_id,))
            self._db.execute(
                """INSERT INTO seat_bindings (session_id, seat, player_id, name)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT (session_id, seat) DO UPDATE SET
                     player_id = excluded.player_id, name = excluded.name, paused = 0""",
                (session_id, seat, player_id, name),
            )

    def unbind(self, session_id, seat):
        """Remove a live binding without changing previously recorded actions."""
        with self._write():
            self._db.execute(
                "DELETE FROM seat_bindings WHERE session_id = ? AND seat = ?",
                (_key(session_id, "场次"), _key(seat, "座位")),
            )

    def bindings(self, session_id):
        rows = self._db.execute(
            """SELECT seat, player_id, name, paused FROM seat_bindings
               WHERE session_id = ? ORDER BY seat""",
            (_key(session_id, "场次"),),
        )
        return [{**dict(row), "paused": bool(row["paused"])} for row in rows]

    def _pause(self, session_id, seat):
        self._db.execute(
            "UPDATE seat_bindings SET paused = 1 WHERE session_id = ? AND seat = ?",
            (session_id, seat),
        )

    def reconcile_seats(self, session_id, frame, *, changed_seats=()):
        """Pause active bindings on positive seat-change evidence, even between hands."""
        session_id = _key(session_id, "场次")
        seats = _seat_readings(frame)
        changed = set(changed_seats) | set(frame.get("vacant_seats", ()))
        paused = []
        with self._write():
            for binding in self.bindings(session_id):
                if binding["paused"]:
                    continue
                seat = binding["seat"]
                name = _name(seats.get(seat, {}).get("name"))
                name_changed = (
                    name and binding["name"] and _name_key(name) != _name_key(binding["name"])
                    and _reliable_name(frame, seat, name)
                )
                if seat in changed or name_changed:
                    self._pause(session_id, seat)
                    paused.append(seat)
        return paused

    def record_frame(self, session_id, hand_id, frame):
        """Record visible labels under the currently bound stable player IDs."""
        session_id, hand_id = _key(session_id, "场次"), _key(hand_id, "手牌")
        seats = _seat_readings(frame)
        street = frame.get("street")
        if street is not None and street not in _STREETS:
            raise ValueError("观察帧的街次无效")
        added = updated = 0
        vacant = set(frame.get("vacant_seats", []))
        inactive = set(frame.get("inactive_seats", ()))
        paused = set()
        with self._write():
            bindings = {row["seat"]: row for row in self.bindings(session_id)}
            for seat, reading in seats.items():
                binding = bindings.get(seat)
                if binding is None:
                    continue
                if binding["paused"]:
                    paused.add(seat)
                    continue
                name = _name(reading.get("name"))
                name_changed = name and binding["name"] and _name_key(name) != _name_key(binding["name"])
                if seat in vacant or name_changed:
                    if seat in vacant or _reliable_name(frame, seat, name):
                        self._pause(session_id, seat)
                        paused.add(seat)
                    # A weak conflicting read cannot invalidate the binding,
                    # but this frame cannot safely be attributed to the ID.
                    continue
                if not binding["name"] and _reliable_name(frame, seat, name):
                    self._db.execute(
                        "UPDATE seat_bindings SET name = ? WHERE session_id = ? AND seat = ?",
                        (name, session_id, seat),
                    )
                if seat in inactive:
                    continue
                kind = reading.get("action")
                if kind is not None and kind not in _ACTIONS:
                    raise ValueError(f"观察帧的动作无效：{kind}")
                if kind is None and not name and reading.get("stack") is None:
                    continue
                key = (session_id, hand_id, seat, binding["player_id"])
                self._db.execute(
                    """INSERT OR IGNORE INTO hand_seats (session_id, hand_id, seat, player_id)
                       SELECT ?, ?, ?, ? WHERE NOT EXISTS (
                           SELECT 1 FROM hand_seats WHERE session_id = ? AND hand_id = ? AND player_id = ?
                       )""",
                    (*key, session_id, hand_id, binding["player_id"]),
                )
                previous_player = self._db.execute(
                    "SELECT player_id FROM hand_seats WHERE session_id = ? AND hand_id = ? AND seat = ?",
                    key[:3],
                ).fetchone()
                if kind and previous_player and previous_player["player_id"] == binding["player_id"]:
                    change = self._record_action(key, kind, street, _visible_wager(reading.get("wager")))
                    added += change == "added"
                    updated += change == "updated"
        return {"added": added, "updated": updated, "paused": sorted(paused)}

    def _record_action(self, key, kind, street, wager):
        rows = self._db.execute(
            """SELECT event_id, street, kind, wager FROM visible_actions
               WHERE session_id = ? AND hand_id = ? AND seat = ? AND player_id = ?
               ORDER BY event_id DESC""",
            key,
        ).fetchall()
        for index, row in enumerate(rows):
            if row["kind"] != kind:
                continue
            same_street = row["street"] == street
            possible_continuation = index == 0 and (row["street"] is None or street is None)
            if not same_street and not possible_continuation and kind not in _TERMINAL:
                continue
            if wager is not None and row["wager"] is None and same_street and index == 0:
                self._db.execute("UPDATE visible_actions SET wager = ? WHERE event_id = ?", (wager, row["event_id"]))
                return "updated"
            if wager is None or row["wager"] == wager or kind in _TERMINAL:
                return None
            if possible_continuation and row["wager"] is None:
                return None

        self._db.execute(
            """INSERT INTO visible_actions (session_id, hand_id, seat, player_id, street, kind, wager)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (*key, street, kind, wager),
        )
        return "added"

    def confirm_preflop(self, session_id, hand_id, seat, *, vpip, pfr):
        """Add a separately verified preflop participation and betting record."""
        session_id, hand_id, seat = _key(session_id, "场次"), _key(hand_id, "手牌"), _key(seat, "座位")
        if type(vpip) is not bool or type(pfr) is not bool or pfr and not vpip:
            raise ValueError("翻前确认需要有效的 vpip、pfr 布尔值，加注应同时计为自愿入池")
        with self._write():
            row = self._db.execute(
                "SELECT player_id FROM hand_seats WHERE session_id = ? AND hand_id = ? AND seat = ?",
                (session_id, hand_id, seat),
            ).fetchone()
            if row is None:
                raise ValueError("这手牌尚无该座位的稳定 ID 记录")
            self._db.execute(
                """INSERT INTO confirmed_preflop (session_id, hand_id, player_id, vpip, pfr)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (session_id, hand_id, player_id)
                   DO UPDATE SET vpip = excluded.vpip, pfr = excluded.pfr""",
                (session_id, hand_id, row["player_id"], vpip, pfr),
            )

    def save_profile(self, player_id, *, name, stats):
        """Record a changed, visibly displayed club profile without affecting seat bindings."""
        player_id, name, stats = _player_id(player_id), _name(name), _profile_stats(stats)
        with self._write():
            previous = self.profiles(player_id, limit=1)
            if previous and previous[0]["name"] == name and previous[0]["stats"] == stats:
                return False
            self._db.execute("INSERT OR IGNORE INTO players (player_id) VALUES (?)", (player_id,))
            self._db.execute(
                """INSERT INTO club_profiles
                   (player_id, name, hands, win_rate, pot_entry_rate, showdown_win_rate)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (player_id, name, stats["hands"], *(stats[key] for key in _PROFILE_RATES)),
            )
        return True

    def profiles(self, player_id, *, limit=100):
        """Return recent separately recorded club profiles, newest first."""
        rows = self._db.execute(
            """SELECT name, hands, win_rate, pot_entry_rate, showdown_win_rate, recorded_at
               FROM club_profiles WHERE player_id = ? ORDER BY profile_id DESC LIMIT ?""",
            (_player_id(player_id), _history_limit(limit)),
        )
        return [
            {"name": row["name"], "stats": {key: row[key] for key in ("hands", *_PROFILE_RATES)},
             "recorded_at": row["recorded_at"]}
            for row in rows
        ]

    def stats(self, player_id):
        """Return observation counts and rates with an independently verified denominator."""
        player_id = _player_id(player_id)
        with self._db:
            self._db.execute("BEGIN")
            return self._stats_snapshot(player_id)

    def _stats_snapshot(self, player_id):
        registered = self._db.execute(
            "SELECT 1 FROM players WHERE player_id = ?", (player_id,),
        ).fetchone() is not None
        observed_hands = self._db.execute(
            """SELECT COUNT(*) FROM
               (SELECT DISTINCT session_id, hand_id FROM visible_actions WHERE player_id = ?)""",
            (player_id,),
        ).fetchone()[0]
        groups = self._db.execute(
            """SELECT street, kind, COUNT(*) AS actions FROM visible_actions
               WHERE player_id = ? GROUP BY street, kind""",
            (player_id,),
        )
        by_street = {street: dict.fromkeys(_ACTIONS, 0) for street in (*_STREETS, "unknown")}
        actions = dict.fromkeys(_ACTIONS, 0)
        for row in groups:
            actions[row["kind"]] += row["actions"]
            by_street[row["street"] or "unknown"][row["kind"]] = row["actions"]
        wager = self._db.execute(
            """SELECT COUNT(wager) AS count, MIN(wager) AS min, MAX(wager) AS max,
                      AVG(wager) AS mean FROM visible_actions WHERE player_id = ?""",
            (player_id,),
        ).fetchone()
        confirmed, vpip, pfr = self._db.execute(
            """SELECT COUNT(*), COALESCE(SUM(vpip), 0), COALESCE(SUM(pfr), 0)
               FROM confirmed_preflop WHERE player_id = ?""",
            (player_id,),
        ).fetchone()
        profiles = self.profiles(player_id, limit=1)
        return {
            "player_id": player_id, "registered": registered,
            "observed_hands": observed_hands, "action_count": sum(actions.values()),
            "actions": actions, "by_street": by_street, "observed_wager": dict(wager),
            "confirmed_preflop_hands": confirmed, "vpip_hands": vpip, "pfr_hands": pfr,
            "vpip": vpip / confirmed if confirmed else None,
            "pfr": pfr / confirmed if confirmed else None,
            "club_profile": profiles[0]["stats"] if profiles else None,
        }

    def events(self, player_id, *, limit=100):
        """Return visible actions, newest observation first."""
        return [dict(row) for row in self._db.execute(
            """SELECT session_id, hand_id, seat, street, kind AS action, wager, recorded_at
               FROM visible_actions WHERE player_id = ? ORDER BY event_id DESC LIMIT ?""",
            (_player_id(player_id), _history_limit(limit)),
        )]
