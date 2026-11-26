"""Post one explicitly located click to the verified visible WePoker window."""

from __future__ import annotations

import ctypes
from math import isfinite
import plistlib
import sys

from .window_capture import _onscreen_windows, _wpk_window_id


_GRAPHICS = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_LEFT_MOUSE_DOWN = 1
_LEFT_MOUSE_UP = 2
_MOUSE_CLICK_STATE = 1
_LEFT_MOUSE_BUTTON = 0
_COMBINED_SESSION_STATE = 0


class WindowInputError(ValueError):
    """The requested window click could not be verified or posted."""


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _finite_number(value):
    try:
        return type(value) in (int, float) and isfinite(value)
    except OverflowError:
        return False


def _bounds(entry):
    bounds = entry.get("kCGWindowBounds")
    if not isinstance(bounds, dict):
        return None
    values = tuple(bounds.get(key) for key in ("X", "Y", "Width", "Height"))
    if not all(map(_finite_number, values)) or min(values[2:]) <= 0:
        return None
    return values


def _contains(bounds, point):
    left, top, width, height = bounds
    x, y = point
    return left <= x < left + width and top <= y < top + height


def _target(entries, window_id, image_size, point):
    above = []
    for entry in entries:  # CoreGraphics lists on-screen windows front to back.
        if not isinstance(entry, dict) or not entry.get("kCGWindowIsOnscreen", True):
            continue
        alpha = entry.get("kCGWindowAlpha", 1)
        if _finite_number(alpha) and alpha <= 0:
            continue
        if entry.get("kCGWindowNumber") == window_id:
            break
        above.append(entry)
    else:
        raise WindowInputError("WePoker 窗口已经关闭、最小化或移到其他桌面。")

    region = _bounds(entry)
    pid = entry.get("kCGWindowOwnerPID")
    if not region or type(pid) is not int or not 0 < pid < 2**31 or _wpk_window_id([entry]) != window_id:
        raise WindowInputError("当前窗口已不是可核实的 WePoker 页面。")

    left, top, width, height = region
    pixel_width, pixel_height = image_size
    scale = 1 if pixel_width / width < 1.5 else 2
    if abs(pixel_width - width * scale) > 2 or abs(pixel_height - height * scale) > 2:
        raise WindowInputError("截图和当前窗口尺寸不一致，请等待下一帧。")

    x, y = point
    screen_point = (left + x * width / pixel_width, top + y * height / pixel_height)
    for item in above:
        item_bounds = _bounds(item)
        if item_bounds is None or _contains(item_bounds, screen_point):
            raise WindowInputError("WePoker 的点击位置被其他窗口遮挡。")
    return pid, screen_point, region


def _window_list():
    try:
        return _onscreen_windows()
    except (OSError, AttributeError, ValueError, plistlib.InvalidFileException) as error:
        raise WindowInputError("无法核实当前窗口位置。") from error


def _bind(library, name, arguments, result):
    function = getattr(library, name)
    function.argtypes = arguments
    function.restype = result
    return function


def _native_apis():
    try:
        graphics = ctypes.CDLL(_GRAPHICS)
        foundation = ctypes.CDLL(_FOUNDATION)
        ref = ctypes.c_void_p
        return (
            _bind(graphics, "CGPreflightPostEventAccess", (), ctypes.c_bool),
            _bind(graphics, "CGEventSourceButtonState", (ctypes.c_int32, ctypes.c_uint32), ctypes.c_bool),
            _bind(graphics, "CGEventCreateMouseEvent",
                  (ref, ctypes.c_uint32, _CGPoint, ctypes.c_uint32), ref),
            _bind(graphics, "CGEventSetFlags", (ref, ctypes.c_uint64), None),
            _bind(graphics, "CGEventSetIntegerValueField", (ref, ctypes.c_uint32, ctypes.c_int64), None),
            _bind(graphics, "CGEventPostToPid", (ctypes.c_int32, ref), None),
            _bind(foundation, "CFRelease", (ref,), None),
        )
    except (OSError, AttributeError) as error:
        raise WindowInputError("macOS 的鼠标输入接口不可用。") from error


def click_window_point(
    window_id: int,
    image_size: tuple[int, int],
    point: tuple[float, float],
) -> None:
    """Post one click at a verified full-window screenshot pixel coordinate.

    The caller determines that the point is a visible avatar or its safe
    dismissal area and that the source frame is current. This primitive
    verifies the window, scale, occlusion, and native input access again.
    """
    if sys.platform == "win32":
        from .windows_window import click_window_point as windows_click

        return windows_click(window_id, image_size, point)
    if sys.platform != "darwin":
        raise WindowInputError("当前版本的自动点头像仅支持 macOS 和 Windows，仍可手动查看。")
    if type(window_id) is not int or not 0 < window_id < 2**32:
        raise WindowInputError("自动点击需要有效的 WePoker 窗口 ID。")
    if (
        not isinstance(image_size, (tuple, list))
        or len(image_size) != 2
        or any(type(size) is not int or not 0 < size < 2**31 for size in image_size)
        or not isinstance(point, (tuple, list))
        or len(point) != 2
        or not all(map(_finite_number, point))
        or not all(0 <= coord < size for coord, size in zip(point, image_size))
    ):
        raise WindowInputError("点击坐标必须位于用于识别的窗口截图内。")

    target = _target(_window_list(), window_id, image_size, point)
    preflight_access, button_state, create, set_flags, set_field, post, release = _native_apis()
    if not preflight_access():
        raise WindowInputError("自动点击需要 macOS「隐私与安全性」中的「辅助功能」授权。")
    if any(button_state(_COMBINED_SESSION_STATE, button) for button in range(3)):
        raise WindowInputError("检测到鼠标按键正在使用，等待下一帧。")

    pid, screen_point, _ = target
    events = []
    try:
        for kind in (_LEFT_MOUSE_DOWN, _LEFT_MOUSE_UP):
            event = create(None, kind, _CGPoint(*screen_point), _LEFT_MOUSE_BUTTON)
            if not event:
                raise WindowInputError("macOS 未能创建鼠标点击事件。")
            events.append(event)
            set_flags(event, 0)
            set_field(event, _MOUSE_CLICK_STATE, 1)

        if _target(_window_list(), window_id, image_size, point) != target:
            raise WindowInputError("WePoker 窗口位置或进程已变更，请等待下一帧。")
        for event in events:
            post(pid, event)
    finally:
        for event in events:
            release(event)
