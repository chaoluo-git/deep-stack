"""Stream local, read-only observations and evaluate verified decision snapshots."""

from contextlib import nullcontext
from decimal import Decimal
import json
from pathlib import Path
import re
from time import monotonic, sleep
import unicodedata
from uuid import uuid4

from .ev import analyze


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
    cardbacks = set(frame.get("cardbacks", []))
    occupied = []
    for seat in seats:
        key = seat["seat"]
        action = seat["action"]
        active = key == "hero" or key in cardbacks or action in _LIVE_ACTIONS
        visibly_empty = key in frame.get("vacant_seats", [])
        visibly_empty |= any(text in _OUT for text in _texts(frame, f"{key}.status"))
        visibly_empty |= any(text in _OUT for text in _texts(frame, f"{key}.label"))
        if key in empty or visibly_empty:
            if active or action == "fold":
                return None, f"{key} 的空席设置与画面冲突", deadline
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
    from .layout import SEAT_IDS

    allowed = {"empty_seats", "pot_includes_wagers", "ranges", "responses", "aggressive_amount"}
    if model.keys() - allowed:
        raise ValueError("实时模型包含未知配置字段")
    empty = model.get("empty_seats", [])
    if not isinstance(empty, list) or any(seat not in SEAT_IDS or seat == "hero" for seat in empty):
        raise ValueError("empty_seats 应为除 hero 外的空座位 ID 列表")
    if type(model.get("pot_includes_wagers", False)) is not bool:
        raise ValueError("pot_includes_wagers 应为布尔值")
    if model.get("aggressive_amount") not in (None, "street_total", "additional"):
        raise ValueError("aggressive_amount 应为 street_total 或 additional")
    ranges = model.get("ranges", {})
    if not isinstance(ranges, dict) or any(seat not in SEAT_IDS or seat == "hero" for seat in ranges):
        raise ValueError("ranges 应为对手座位 ID 到范围的对象")
    responses = model.get("responses", {})
    if not isinstance(responses, dict) or any(
        kind not in _LIVE_ACTIONS or not isinstance(values, dict)
        or any(seat not in SEAT_IDS or seat == "hero" for seat in values)
        for kind, values in responses.items()
    ):
        raise ValueError("responses 应为动作到对手座位应答模型的对象")
    return model


class HandTracker:
    """Tag coherent same-hand frames without inventing boundaries from unknown cards."""

    def __init__(self):
        self.hand = None
        self.hero = None
        self.board = []
        self.street = None
        self.candidate = None

    def update(self, frame):
        street = frame["street"]
        cards = frame["hero_cards"]
        board = frame["board"]
        if street not in _STREETS:
            self.candidate = None
            return None
        if not cards:
            self.candidate = None
            if self.board and board is not None and board[:len(self.board)] == self.board:
                self.board = board
                self.street = street
                return self.hand
            return None
        hero = tuple(sorted(cards))
        if self.hand is None:
            self.hand = uuid4().hex
        elif hero != self.hero or _STREETS.index(street) < _STREETS.index(self.street):
            fingerprint = (hero, street, tuple(board or ()))
            if self.candidate != fingerprint:
                self.candidate = fingerprint
                return None
            self.hand = uuid4().hex
            self.board = []
        elif board is not None and board[:len(self.board)] != self.board:
            self.candidate = None
            return None
        self.candidate = None
        self.hero = hero
        self.street = street
        if board is not None:
            self.board = board
        return self.hand


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
              json_lines, database=None, session=None):
    from .recognition import recognize, screen_image

    model = validate_model(model)
    tracker = HandTracker()
    context = nullcontext()
    if database is not None:
        from .player_store import PlayerStore

        context = PlayerStore(database)
    source = iter(frames) if frames else iter(screen_image, None)
    previous = None
    last_log_status = None
    with context as store:
        if store is not None:
            _emit({"type": "session", "session": session,
                   "message": f"玩家记录会话 {session}，数据库 {Path(database)}"}, json_lines)
        for image in source:
            started = monotonic()
            try:
                frame = recognize(image, viewport=viewport, profile=profile,
                                  auto_viewport=viewport is None, live=True)
            except (OSError, ValueError) as error:
                event = {"type": "waiting", "message": str(error)}
                if frames:
                    raise
            else:
                if store is not None and not frame.get("layout_mismatch") and (hand := tracker.update(frame)) is not None:
                    registered = store.record_frame(session, hand, frame)
                    paused = tuple(registered.get("paused", []))
                    if paused and paused != last_log_status:
                        _emit({"type": "binding_paused", "seats": paused,
                               "message": f"需重新确认玩家 ID {'、'.join(paused)}"}, json_lines)
                    last_log_status = paused
                state, reason, seconds = _state(frame, model)
                if reason:
                    event = {"type": "waiting", "message": reason}
                else:
                    fingerprint = json.dumps(state, ensure_ascii=False, sort_keys=True)
                    if previous != ("recommendation", fingerprint):
                        try:
                            analysis = analyze(state, samples=samples)
                        except ValueError as error:
                            event = {"type": "waiting", "message": f"状态校验未通过：{error}"}
                        else:
                            event = {"type": "recommendation", "analysis": analysis}
                    else:
                        event = {"type": "recommendation"}
                    event["fingerprint"] = fingerprint
                    if monotonic() - started >= min(max_age, seconds):
                        event = {"type": "waiting", "message": "本次读数已超时"}
            key = (event["type"], event.get("fingerprint", event.get("message")))
            if key != previous:
                event.pop("fingerprint", None)
                _emit(event, json_lines)
                previous = key
            if not frames:
                sleep(max(0.0, interval - (monotonic() - started)))
