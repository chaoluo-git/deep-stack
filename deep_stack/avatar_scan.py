"""Schedule optional profile lookups from positively verified spectator frames."""

from dataclasses import dataclass

from .window_input import WindowInputError, click_window_point


_MAX_FRAME_AGE = 2.5
_CLICK_INTERVAL = 1.5
_SEAT_COOLDOWN = 15.0
_OPEN_TIMEOUT = 8.0
_CLOSE_TIMEOUT = 3.0
_CONFIRMED_POPUP_EVENTS = {"player_bound", "player_profile"}


@dataclass
class _Attempt:
    seat: str
    deadline: float
    phase: str = "opening"


def _event(kind, message, *, seat=None, stage=None):
    return {"type": kind, "message": message, "seat": seat, "stage": stage}


class AvatarScanner:
    """Open one profile at a time, verify ownership, and wait for its close."""

    def __init__(self):
        self._window = None
        self._last_capture = float("-inf")
        self._next_click = float("-inf")
        self._retry_after = {}
        self._attempt = None
        self._disabled = False

    def _click(self, window_id, image_size, point, now):
        try:
            click_window_point(window_id, image_size, point)
        except WindowInputError as error:
            self._disabled = True
            self._attempt = None
            return _event("waiting", f"已暂停自动头像扫描：{error}", stage="disabled")
        self._next_click = now + _CLICK_INTERVAL
        return None

    def step(self, frame, *, window_id, image_size, now, captured_at, bindings, popup_event):
        """Advance one step using a fresh frame of the same visible spectator window.

        Verified avatar and dismissal points are full-window screenshot pixels.
        A popup is closed only after the profile tracker confirms the requested
        seat. Unknown or manually opened popups remain available to the user.
        """
        if self._disabled:
            return None
        if type(window_id) is not int or window_id <= 0 or not image_size:
            self._window = self._attempt = None
            return None

        window = (window_id, tuple(image_size))
        if self._window is not None and window != self._window:
            self._window = window
            self._attempt = None
            self._retry_after.clear()
            return None
        self._window = window

        if (
            frame.get("spectating") is not True
            or frame.get("layout_mismatch")
            or frame.get("hero_evidence")
            or frame.get("hero_cards")
            or any(row.get("seat") == "hero" for row in frame.get("seats", ()))
        ):
            self._attempt = None
            return None
        if not 0 <= now - captured_at <= _MAX_FRAME_AGE or captured_at <= self._last_capture:
            return None
        self._last_capture = captured_at

        popup_open = bool(frame.get("popup_open"))
        if self._attempt is not None:
            return self._advance(frame, popup_open, popup_event, window_id, image_size, now)
        if popup_open or now < self._next_click:
            return None

        occupied = frame.get("avatar_points", {})
        skipped = set(frame.get("vacant_seats", ())) | {"hero"}
        skipped.update(row["seat"] for row in bindings if row.get("player_id") and not row.get("paused"))
        for seat, point in occupied.items():
            if seat in skipped or now < self._retry_after.get(seat, float("-inf")):
                continue
            error = self._click(window_id, image_size, point, now)
            if error:
                return error
            self._attempt = _Attempt(seat, now + _OPEN_TIMEOUT)
            self._retry_after[seat] = now + _SEAT_COOLDOWN
            return _event("avatar_scan", f"已点击 {seat} 的头像，等待资料弹窗。",
                          seat=seat, stage="opening")
        return None

    def _advance(self, frame, popup_open, popup_event, window_id, image_size, now):
        attempt = self._attempt
        if attempt.phase == "closing":
            if not popup_open:
                self._attempt = None
                return None
            if now >= attempt.deadline:
                self._attempt = None
                return _event("waiting", "头像资料仍在显示，请手动关闭以继续扫描。",
                              seat=attempt.seat, stage="close_timeout")
            return None

        if now >= attempt.deadline:
            self._attempt = None
            if popup_open:
                message = "资料弹窗未能与所点座位核对，请手动处理。"
            else:
                message = f"未检测到 {attempt.seat} 的资料弹窗，稍后重试。"
            return _event("waiting", message, seat=attempt.seat, stage="open_timeout")
        if not popup_open:
            if attempt.phase == "popup":
                self._attempt = None
            return None

        attempt.phase = "popup"
        if not popup_event or popup_event.get("type") not in _CONFIRMED_POPUP_EVENTS:
            return None
        confirmed_seat = popup_event.get("seat")
        if confirmed_seat is not None and confirmed_seat != attempt.seat:
            self._attempt = None
            return _event("waiting", "弹窗座位与刚点击的头像不一致，请手动处理。",
                          seat=attempt.seat, stage="different_seat")
        point = frame.get("dismiss_popup_point")
        if confirmed_seat != attempt.seat or point is None or now < self._next_click:
            return None

        error = self._click(window_id, image_size, point, now)
        if error:
            return error
        attempt.phase = "closing"
        attempt.deadline = now + _CLOSE_TIMEOUT
        return _event("avatar_scan", f"已核对并点击关闭 {attempt.seat} 的资料弹窗。",
                      seat=attempt.seat, stage="closing")
