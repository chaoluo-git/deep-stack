"""Stream local, read-only observations and evaluate verified decision snapshots."""

from contextlib import nullcontext
from decimal import Decimal
import json
from pathlib import Path
import re
import sys
from time import monotonic, sleep
import unicodedata
from uuid import uuid4

from .ev import analyze
from .layout import SEAT_LABELS
from .player_popup import PlayerPopup, nickname_key, resolve_popup_seat


_SECONDS = re.compile(r"([1-9]\d?)\s*(?:s|秒)", re.IGNORECASE)
_CONTROLS = {
    "fold": ("弃牌", "棄牌", "fold"),
    "check": ("让牌", "讓牌", "过牌", "過牌", "check"),
    "call": ("跟注", "跟註", "call"),
    "bet": ("下注", "下註", "bet"),
    "raise": ("加注到", "加註到", "加注", "加註", "raiseto", "raise"),
    "all_in": ("全下", "全押", "allin"),
}
_AMOUNT = r"\d+(?:,\d{3})*(?:\.\d+)?"
_LIVE_ACTIONS = {"check", "call", "bet", "raise", "all_in"}
_OUT = {"空位", "坐下", "离座", "離座", "等待加入", "sitout", "empty"}
_STREETS = ("preflop", "flop", "turn", "river")
_LABELS = {"fold": "弃牌", "check": "过牌", "call": "跟注", "bet": "下注",
           "raise": "加注", "all_in": "全下"}


def _texts(frame, key):
    observations = frame.get("evidence", {}).get(key, {}).get("observations", [])
    for reading in observations:
        if reading["confidence"] >= 0.65:
            text = unicodedata.normalize("NFKC", reading["text"]).casefold()
            yield "".join(text.split())


def _timer(frame, seat):
    values = {
        int(match[1])
        for key in (f"{seat}.avatar", f"{seat}.status")
        for text in _texts(frame, key)
        if (match := _SECONDS.fullmatch(text))
    }
    return next(iter(values)) if len(values) == 1 else None


def _control(frame, key, kinds):
    """Accept exact control labels, with an optional on-button chip amount."""
    results = set()
    for text in _texts(frame, key):
        for kind in kinds:
            for label in _CONTROLS[kind]:
                match = re.fullmatch(rf"{label}[:+]?({_AMOUNT})?", text)
                if match:
                    amount = match[1]
                    results.add((kind, float(amount.replace(",", "")) if amount else None))
    if len(results) == 1:
        return next(iter(results))
    return None


def _state(frame, model):
    if frame.get("layout_mismatch"):
        return None, "窗口比例偏离现有布局，请加载对应的 --layout", None
    if frame.get("spectating"):
        if frame.get("hero_evidence"):
            return None, "观战模式暂停扫描，画面出现本人入座信号", None
        return None, "观战中，已识别可见牌桌", None
    if frame.get("hero_state") == "waiting_for_hand":
        return None, "已入座，等待下一手发牌", None
    if frame.get("hero_state") == "sitting_out":
        return None, "本人当前留座，等待恢复参与", None
    if frame.get("hero_state") == "folded":
        return None, "本人已弃牌，继续观察本手", None
    seats = frame["seats"]
    timers = {seat["seat"]: _timer(frame, seat["seat"]) for seat in seats}
    deadline = timers.get("hero")
    if deadline is None or any(value is not None for key, value in timers.items() if key != "hero"):
        return None, "等待确认轮到自己行动", deadline
    if frame["hero_cards"] is None or frame["board"] is None or frame["pot"] is None:
        return None, "手牌、公共牌或底池读数不完整", deadline
    if any("取整" in warning for warning in frame.get("warnings", [])):
        return None, "金额使用缩写，当前读数无法精确核对", deadline

    empty = set(model.get("empty_seats", []))
    inactive = set(frame.get("inactive_seats", ()))
    observed_status = frame.get("seat_status")
    cardbacks = set(frame.get("cardbacks", []))
    occupied = []
    for seat in seats:
        key = seat["seat"]
        action = seat["action"]
        active = key == "hero" or key in cardbacks or action in _LIVE_ACTIONS
        visibly_empty = key in frame.get("vacant_seats", [])
        if observed_status is None:
            visibly_empty |= any(text in _OUT for region in ("status", "label")
                                 for text in _texts(frame, f"{key}.{region}"))
        elif observed_status.get(key) == "conflict":
            return None, f"{SEAT_LABELS.get(key, key)}的占座读数冲突", deadline
        if key in empty or visibly_empty:
            if active or action == "fold":
                return None, f"{key} 的空席设置与画面冲突", deadline
            continue
        if key in inactive:
            if active or seat["wager"] not in (None, 0):
                return None, f"{key} 的等待状态与本手参与读数冲突", deadline
            continue
        if action == "fold":
            if key == "hero" or key in cardbacks:
                return None, f"{key} 的牌背与弃牌读数冲突", deadline
            active = False
        elif not active:
            return None, f"{key} 是否仍在本手无法确认", deadline
        if active and (seat["stack"] is None or seat["stack"] <= 0 or action == "all_in"):
            return None, f"{key} 筹码未知或已全下，需完整边池数据", deadline
        occupied.append({
            "seat": key,
            "stack": seat["stack"] if active else 0,
            "in_hand": active,
            "wager": seat["wager"],
            "range": model.get("ranges", {}).get(key, "random"),
        })
    hero = next((seat for seat in occupied if seat["seat"] == "hero"), None)
    opponents = [seat for seat in occupied if seat["in_hand"] and seat["seat"] != "hero"]
    if hero is None or not opponents:
        return None, "还没有确认足够的活跃座位", deadline

    wagers = [seat["wager"] for seat in occupied]
    all_wagers = all(value is not None for value in wagers)
    center = frame.get("center_pot")
    if model.get("pot_includes_wagers") and center is not None:
        seen_total = sum(Decimal(str(value)) for value in wagers if value is not None)
        reconciled = Decimal(str(frame["pot"])) == Decimal(str(center)) + seen_total
        if not reconciled and all_wagers:
            return None, "两行底池与本街投入对不上", deadline
        if reconciled:
            for seat in occupied:
                if seat["wager"] is None:
                    seat["wager"] = 0
            all_wagers = True

    passive = _control(frame, "hero_passive_button", ("check", "call"))
    to_call = None
    if all_wagers:
        high = max([hero["wager"], *(seat["wager"] for seat in opponents)])
        to_call = float(Decimal(str(high)) - Decimal(str(hero["wager"])))
        if passive is None:
            passive = ("call", to_call) if to_call else ("check", None)
    if passive is None:
        return None, "跟注或过牌按钮未读到，且本街投入未能核对", deadline
    kind, amount = passive
    if kind == "call" and amount is None:
        amount = to_call
    if kind == "call" and (amount is None or amount <= 0 or amount >= hero["stack"]):
        return None, "跟注金额未确认，或自己是短码全下", deadline
    if to_call is not None and ((kind == "check" and to_call != 0) or (kind == "call" and amount != to_call)):
        return None, "按钮金额与本街投入不一致", deadline
    if kind == "call" and len(opponents) > 1 and not all_wagers:
        return None, "多人跟注需要核对所有座位的本街投入", deadline

    actions = [{"kind": kind, "amount": amount if kind == "call" else 0}]
    if kind == "call" or _control(frame, "hero_fold_button", ("fold",)):
        actions.insert(0, {"kind": "fold", "amount": 0})
    response_model = model.get("responses", {})
    if kind in response_model and all_wagers:
        actions[-1]["responses"] = response_model[kind]
    aggressive = _control(frame, "hero_aggressive_button", ("bet", "raise", "all_in"))
    sizing = model.get("aggressive_amount")
    if all_wagers and aggressive and sizing and aggressive[0] in response_model:
        move, display_amount = aggressive
        if display_amount is not None:
            amount = display_amount
            if sizing == "street_total":
                amount = float(Decimal(str(display_amount)) - Decimal(str(hero["wager"])))
            actions.append({"kind": move, "amount": amount, "responses": response_model[move]})
    if not all_wagers:
        for seat in occupied:
            seat.pop("wager")
    return {
        "hero_cards": frame["hero_cards"], "board": frame["board"], "pot": frame["pot"],
        "seats": occupied, "actions": actions,
    }, None, deadline


def validate_model(model):
    if not isinstance(model, dict):
        raise ValueError("实时模型应是 JSON 对象")
    from .layout import EXTRA_SEAT_IDS, SEAT_IDS

    seat_ids = SEAT_IDS + EXTRA_SEAT_IDS
    allowed = {"empty_seats", "pot_includes_wagers", "ranges", "responses", "aggressive_amount"}
    if model.keys() - allowed:
        raise ValueError("实时模型包含未知配置字段")
    empty = model.get("empty_seats", [])
    if not isinstance(empty, list) or any(seat not in seat_ids or seat == "hero" for seat in empty):
        raise ValueError("empty_seats 应为除 hero 外的空座位 ID 列表")
    if type(model.get("pot_includes_wagers", False)) is not bool:
        raise ValueError("pot_includes_wagers 应为布尔值")
    if model.get("aggressive_amount") not in (None, "street_total", "additional"):
        raise ValueError("aggressive_amount 应为 street_total 或 additional")
    ranges = model.get("ranges", {})
    if not isinstance(ranges, dict) or any(seat not in seat_ids or seat == "hero" for seat in ranges):
        raise ValueError("ranges 应为对手座位 ID 到范围的对象")
    responses = model.get("responses", {})
    if not isinstance(responses, dict) or any(
        kind not in _LIVE_ACTIONS or not isinstance(values, dict)
        or any(seat not in seat_ids or seat == "hero" for seat in values)
        for kind, values in responses.items()
    ):
        raise ValueError("responses 应为动作到对手座位应答模型的对象")
    return model


class HandTracker:
    """Tag coherent hands using cards or repeated, verified preflop positions."""

    def __init__(self):
        self.hand = None
        self.hero = None
        self.board = []
        self.street = None
        self.dealer = None
        self.dealer_conflict = False
        self.candidate = None

    @staticmethod
    def _positions(frame):
        positions = frame.get("positions") or {}
        sources = positions.get("sources") or {}
        dealer = positions.get("dealer_seat") if sources.get("dealer_seat") == "dealer_marker" else None
        small, big = (positions.get(role) for role in ("small_blind_seat", "big_blind_seat"))
        blind_sources = {"blind_label", "preflop_order_and_wager"}
        if (dealer and small and big and small != big and dealer != big
                and all(sources.get(role) in blind_sources for role in ("small_blind_seat", "big_blind_seat"))):
            return dealer, (dealer, small, big)
        return dealer, None

    def update(self, frame):
        street = frame["street"]
        cards = frame["hero_cards"]
        board = frame["board"]
        dealer, positions = self._positions(frame)
        if dealer and self.dealer and dealer != self.dealer:
            self.dealer_conflict = True
        if street not in _STREETS:
            self.candidate = None
            return None
        hero = tuple(sorted(cards)) if cards else None
        reset = self.street is not None and _STREETS.index(street) < _STREETS.index(self.street)
        hero_changed = hero is not None and self.hero is not None and hero != self.hero
        board_conflict = bool(board and board[:len(self.board)] != self.board)
        preflop_anchor = positions if street == "preflop" and board == [] else None
        same_preflop = (street == self.street == "preflop" and board == []
                        and dealer is not None and dealer == self.dealer)
        boundary = hero_changed or reset or (hero is None and board_conflict)
        if self.dealer_conflict:
            same_cards = ((hero is not None and hero == self.hero)
                          or (bool(board and self.board) and not board_conflict))
            evidence_of_change = preflop_anchor or hero_changed or (board and (not self.board or board_conflict))
            if dealer == self.dealer and same_cards and not boundary:
                self.dealer_conflict = False
            elif dealer is None or not evidence_of_change:
                self.candidate = None
                return None
            else:
                boundary = True
        if hero is None and not (board or (reset and board == []) or preflop_anchor or same_preflop):
            self.candidate = None
            return None
        if self.hand is None or boundary:
            if self.hand is not None or (hero is None and not board):
                fingerprint = (hero, street, tuple(board or ()), dealer, preflop_anchor if hero is None else None)
                if self.candidate != fingerprint:
                    self.candidate = fingerprint
                    return None
            self.hand = uuid4().hex
            self.board = []
            self.hero = None
            self.dealer = dealer
            self.dealer_conflict = False
        elif board_conflict:
            self.candidate = None
            return None
        self.candidate = None
        if self.dealer is None:
            self.dealer = dealer
        if hero is not None:
            self.hero = hero
        self.street = street
        if board is not None:
            self.board = board
        return self.hand


_BOTTOM_SEATS = {"hero", "bottom_center"}


def _near_viewport(first, second):
    # The lower edge of the same window can fluctuate by a few screenshot pixels.
    if first is None or second is None:
        return first is second
    return len(first) == len(second) == 4 and all(abs(a - b) <= 4 for a, b in zip(first, second))


def _seat_evidence(frame, previous_seats=()):
    """Retain top-center evidence, but count the physical bottom seat once."""
    vacant = set(frame.get("vacant_seats", []))
    primary = {seat["seat"]: seat for seat in frame["seats"]}
    seats = primary.copy()
    for seat in frame.get("popup_seat_candidates", ()):
        seats.setdefault(seat["seat"], seat)
    avatars = frame.get("seat_avatars", {})

    bottom = next((seat["seat"] for seat in previous_seats if seat["seat"] in _BOTTOM_SEATS), None)
    bottom = bottom or next((seat for seat in primary if seat in _BOTTOM_SEATS), None)
    if bottom is not None:
        other = next(iter(_BOTTOM_SEATS - {bottom}))
        if other in primary and bottom not in primary:
            # A popup can hide evidence that distinguished a spectator from hero.
            seats[bottom] = {**primary[other], "seat": bottom}
            if other in vacant:
                vacant.add(bottom)
        seats.pop(other, None)
        avatars = {**avatars, bottom: avatars.get(bottom) or avatars.get(other)}

    rows = [{**seat, "vacant": seat_id in vacant or bool(seat.get("vacant"))}
            for seat_id, seat in seats.items()]
    return rows, {seat["seat"]: avatars[seat["seat"]] for seat in rows
                  if not seat["vacant"] and avatars.get(seat["seat"]) is not None}


def _visible_names(seats):
    return {seat["seat"]: seat["name"] for seat in seats if seat.get("name") and not seat["vacant"]}


_POPUP_SEAT_REASONS = {
    "avatar_nickname_conflict": "头像和昵称指向不同座位",
    "duplicate_nickname": "桌上出现同名玩家，头像未能区分",
    "seat_unavailable": "昵称对应的座位已空出或当前不可见",
    "nickname_changed": "同一座位的前后昵称不一致",
    "no_matching_seat": "当前没有能唯一对应的牌桌昵称或头像",
    "popup_nickname_unavailable": "弹窗昵称没读清，头像也无法唯一对应",
    "evidence_varies": "相邻两帧的座位证据不一致",
}


class PopupTracker:
    """Two readings confirm a visible stable ID before it can change a binding."""

    def __init__(self):
        self.reference = None
        self.popup_reference = None
        self.popup_viewport = None
        self.popup_id = None
        self.in_popup = False
        self.candidate = None
        self.saved = None

    def update(self, frame, store, session, now):
        viewport = frame.get("viewport")
        if not frame.get("popup_open"):
            seats, avatars = _seat_evidence(frame)
            names = _visible_names(seats)
            repeated = {}
            if not self.in_popup and self.reference and _near_viewport(self.reference[1], viewport):
                old_names = _visible_names(self.reference[2])
                repeated = {seat: name for seat, name in names.items() if old_names.get(seat) == name}
            self.reference = (now, viewport, seats, avatars, repeated)
            self.in_popup = False
            self.popup_reference = self.popup_id = self.candidate = self.saved = None
            return None
        if not self.in_popup:
            self.in_popup = True
            self.popup_viewport = viewport
            if self.reference and now - self.reference[0] <= 8 and _near_viewport(self.reference[1], viewport):
                self.popup_reference = self.reference
        elif not _near_viewport(self.popup_viewport, viewport):
            self.popup_viewport = viewport
            self.popup_reference = self.candidate = self.saved = None
        raw = frame.get("player_popup")
        if raw is None:
            self.candidate = None
            return {"type": "waiting", "message": "玩家资料已打开，等待读到唯一 ID"}
        popup = PlayerPopup(**raw)
        if self.popup_id is not None and self.popup_id != popup.player_id:
            # A new ID without an observed close has no confirmed unobscured reference.
            self.popup_reference = self.candidate = self.saved = None
        self.popup_id = popup.player_id
        previous, previous_avatars, repeated = self.popup_reference[2:] if self.popup_reference else ((), {}, {})
        seats, avatars = _seat_evidence(frame, previous)
        avatar_views = {seat: [avatars.get(seat), previous_avatars.get(seat)]
                        for seat in avatars.keys() | previous_avatars.keys()}
        seat, seat_reason = resolve_popup_seat(
            popup, seats, previous_seats=previous, popup_avatar=frame.get("popup_avatar"),
            seat_avatars=avatar_views,
        )
        key = (popup.player_id, seat)
        saved_event = self.saved[1] if self.saved else None
        if seat is None and saved_event and saved_event["type"] == "player_bound":
            return saved_event
        last = self.candidate
        self.candidate = (key, popup.stats, popup.name, seat_reason)
        if last is None or last[0] != key:
            return {"type": "waiting", "message": "正在复核弹窗 ID，请保持弹窗片刻"}

        name = popup.name if nickname_key(popup.name) == nickname_key(last[2]) else None
        stats = {name: value for name, value in popup.stats.items() if last[1].get(name) == value}
        if seat is None and last[3] != seat_reason:
            seat_reason = "evidence_varies"
        snapshot = (key, name, stats, seat_reason)
        if self.saved and self.saved[0] == snapshot:
            return saved_event
        event = {"type": "player_profile", "player_id": popup.player_id, "name": name,
                 "seat": seat, "stats": stats}
        if store is not None and len(stats) == 4:
            store.save_profile(popup.player_id, name=name, stats=stats)
        if seat is None:
            event["seat_match_reason"] = seat_reason
            reason = _POPUP_SEAT_REASONS[seat_reason]
            message = f"已读到 ID {popup.player_id}，座位未能唯一确认（{reason}）"
            if not self.popup_reference and seat_reason in {"no_matching_seat", "popup_nickname_unavailable"}:
                message += "。可先关闭资料，等牌桌读数恢复后再打开同一头像"
        elif store is None:
            message = f"已读到{SEAT_LABELS.get(seat, seat)}的 ID {popup.player_id}"
        else:
            desk_name = repeated.get(seat)
            shown_name = _visible_names(seats).get(seat)
            if shown_name and nickname_key(shown_name) != nickname_key(desk_name):
                desk_name = None
            try:
                if not saved_event or saved_event["type"] != "player_bound" or saved_event["seat"] != seat:
                    store.bind(session, seat, popup.player_id, name=desk_name)
            except ValueError as error:
                message = f"已读到 ID {popup.player_id}，绑定待处理：{error}"
            else:
                event["type"] = "player_bound"
                message = f"已将{SEAT_LABELS.get(seat, seat)}绑定到玩家 ID {popup.player_id}"
        event["message"] = message
        event["fingerprint"] = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        self.saved = (snapshot, event)
        return event


def _emit(event, json_lines):
    if json_lines:
        print(json.dumps(event, ensure_ascii=False, allow_nan=False), flush=True)
    elif "analysis" in event:
        recommendation = event["analysis"]["recommendation"]
        alternatives = " / ".join(
            f"{_LABELS[action['kind']]} {action['amount']:g} ({action['ev']:+.2f})"
            for action in event["analysis"]["actions"]
        )
        prefix = "建议" if len(event["analysis"]["actions"]) > 1 else "仅估算"
        print(f"{prefix} {_LABELS[recommendation['kind']]} {recommendation['amount']:g} 筹码"
              f" | 候选 EV {alternatives}", flush=True)
    else:
        print(event["message"], flush=True)


def scan_live(*, frames, viewport, profile, model, samples, interval, max_age,
              json_lines, database=None, session=None, window_id=None,
              force_spectator=False, seat_capacity=None, show_actions=False, show_board=False, scan_avatars=False,
              observe_only=False, on_observation=None, should_stop=None):
    from .avatar_match import avatars_conflict
    from .live_actions import ActionLog
    from .live_board import BoardLog
    from .live_table import TableLog
    from .recognition import recognize

    model = validate_model(model)
    tracker = HandTracker()
    popups = PopupTracker()
    actions = ActionLog()
    boards = BoardLog()
    tables = TableLog()
    last_viewport = None
    table_capacity = pending_capacity = None
    portraits = {}
    scanner = None
    if scan_avatars:
        from .avatar_scan import AvatarScanner

        scanner = AvatarScanner()
    context = nullcontext()
    if database is not None:
        from .player_store import PlayerStore

        context = PlayerStore(database)
    source = iter(frames) if frames else None
    capture = LiveCapture(window_id, prefer_window=scan_avatars or viewport is None, require_window=scan_avatars)
    previous = None
    last_log_status = None
    with context as store:
        if store is not None:
            _emit({"type": "session", "session": session,
                   "message": f"玩家记录会话 {session}，数据库 {Path(database)}"}, json_lines)
        while should_stop is None or not should_stop():
            started = monotonic()
            try:
                if source is None:
                    image, captured_window = capture.read()
                    if captured_window != capture.previous_window:
                        last_viewport = None
                        tracker = HandTracker()
                        popups = PopupTracker()
                        actions = ActionLog()
                        boards = BoardLog()
                        tables = TableLog()
                        portraits.clear()
                        table_capacity = pending_capacity = None
                        capture.previous_window = captured_window
                else:
                    image, captured_window = next(source), None
                sampled_at = monotonic() if on_observation is not None else None
                frame = recognize(image, viewport=viewport, profile=profile,
                                  auto_viewport=viewport is None, live=True, popup_fallback=last_viewport,
                                  window_capture=captured_window is not None,
                                  force_spectator=force_spectator, seat_capacity=seat_capacity)
            except StopIteration:
                break
            except (OSError, ValueError) as error:
                event = {"type": "waiting", "message": str(error)}
                if frames:
                    raise
            else:
                if captured_window is not None and last_viewport is None and frame.get("viewport"):
                    width, height = frame["viewport"][2:]
                    _emit({"type": "viewport", "viewport": frame["viewport"],
                           "message": f"已定位 WePoker 牌桌 {width}×{height}"}, json_lines)
                last_viewport = frame.get("viewport") or last_viewport
                event = popups.update(frame, store, session, monotonic())
                bindings = store.bindings(session) if store is not None else []
                if event is not None and event["type"] == "player_bound":
                    if (portrait := frame.get("popup_avatar")) is not None:
                        portraits[event["seat"]] = (event["player_id"], portrait)
                awaiting_layout = pending_capacity is not None
                if event is None:
                    reported_capacity = frame.get("seat_capacity")
                    capacity = reported_capacity if frame.get("seat_layout_confirmed") else None
                    awaiting_layout = (reported_capacity is not None and table_capacity is not None
                                       and reported_capacity != table_capacity
                                       and (capacity is None or capacity != pending_capacity))
                    pending_capacity = capacity if awaiting_layout else None
                    if awaiting_layout:
                        event = {"type": "waiting", "message": "座位布局发生变化，正在复核"}
                if event is None:
                    table_changed = capacity is not None and table_capacity is not None and capacity != table_capacity
                    if table_changed:
                        tracker = HandTracker()
                        popups = PopupTracker()
                        popups.update(frame, store, session, monotonic())
                        actions = ActionLog()
                        boards = BoardLog()
                        tables = TableLog()
                        portraits.clear()
                    if capacity is not None:
                        table_capacity = capacity
                    hand = tracker.update(frame)
                    if store is not None:
                        seen = frame.get("seat_avatars", {})
                        changed = [
                            binding["seat"] for binding in bindings if not binding["paused"] and (
                                table_changed or (
                                    (known := portraits.get(binding["seat"])) is not None
                                    and known[0] == binding["player_id"] and binding["seat"] in seen
                                    and avatars_conflict(known[1], seen[binding["seat"]])
                                )
                            )
                        ]
                        store.reconcile_seats(session, frame, changed_seats=changed)
                        if hand is not None:
                            store.record_frame(session, hand, frame)
                        bindings = store.bindings(session)
                        paused = tuple(binding["seat"] for binding in bindings if binding["paused"])
                        if paused and paused != last_log_status:
                            _emit({"type": "binding_paused", "seats": paused,
                                   "message": f"需重新确认玩家 ID {'、'.join(paused)}"}, json_lines)
                        last_log_status = paused
                    if show_board and (board_event := boards.update(frame, hand)) is not None:
                        if on_observation is not None:
                            on_observation(board_event, captured_at=sampled_at, sample_started_at=started)
                        _emit(board_event, json_lines)
                    if show_actions:
                        if (table_event := tables.update(frame, hand)) is not None:
                            _emit(table_event, json_lines)
                        stacks = {seat["seat"]: seat["stack"] for seat in frame["seats"]}
                        for action in actions.update(frame, hand, bindings, positions=tables.position_by_seat):
                            if (stack := stacks.get(action["seat"])) is not None:
                                action["stack"] = stack
                                action["message"] += f"｜余额 {stack:g}"
                            if on_observation is not None:
                                on_observation(action, captured_at=sampled_at, sample_started_at=started)
                            _emit(action, json_lines)
                    if observe_only:
                        event = {"type": "observing", "message": "正在采集可见动作和公共牌"}
                    else:
                        event = _decision_event(frame, model, samples, previous, started, max_age)
                if scanner is not None and captured_window is not None and not awaiting_layout:
                    scan_event = scanner.step(
                        frame, window_id=captured_window, image_size=image.size,
                        now=monotonic(), captured_at=started, bindings=bindings,
                        popup_event=event if frame.get("popup_open") else None,
                    )
                    if scan_event:
                        _emit(scan_event, json_lines)
            key = (event["type"], event.get("fingerprint", event.get("message")))
            if key != previous:
                _emit({key: value for key, value in event.items() if key != "fingerprint"}, json_lines)
                previous = key
            if source is None:
                sleep(max(0.0, interval - (monotonic() - started)))


class LiveCapture:
    def __init__(self, window_id=None, *, prefer_window=True, require_window=False):
        self.window_id = window_id
        self.prefer_window = prefer_window and sys.platform == "darwin"
        self.require_window = require_window
        self.previous_window = None

    def read(self):
        from .recognition import screen_image
        from .window_capture import WindowCaptureError, capture_window, find_wpk_window

        if self.require_window and sys.platform == "win32":
            from .windows_window import capture_window, find_wpk_window

        window_id = self.window_id
        if window_id is None and (self.prefer_window or self.require_window):
            window_id = find_wpk_window()
            if window_id is None and (self.previous_window is not None or self.require_window):
                raise WindowCaptureError("等待 WePoker-H5 窗口恢复可见")
        if window_id is not None:
            return capture_window(window_id), window_id
        return screen_image(), None


def _decision_event(frame, model, samples, previous, started, max_age):
    state, reason, seconds = _state(frame, model)
    if reason:
        return {"type": "waiting", "message": reason}
    fingerprint = json.dumps(state, ensure_ascii=False, sort_keys=True)
    event = {"type": "recommendation", "fingerprint": fingerprint}
    if previous != ("recommendation", fingerprint):
        try:
            event["analysis"] = analyze(state, samples=samples)
        except ValueError as error:
            return {"type": "waiting", "message": f"状态校验未通过：{error}"}
    if monotonic() - started >= min(max_age, seconds):
        return {"type": "waiting", "message": "本次读数已超时"}
    return event
