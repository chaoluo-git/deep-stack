"""Read and click a foreground Chromium WePoker window on Windows."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import ctypes
from dataclasses import dataclass
from functools import lru_cache
import ntpath
import sys

from .window_capture import WindowCaptureError, _WPK_TITLE
from .window_input import WindowInputError, _bind, _finite_number


_CHROMIUM_EXECUTABLES = frozenset(("chrome.exe", "msedge.exe"))
_BUSY_GUI_FLAGS = 0x001E  # Moving/resizing, menu mode, system menu, or popup menu.
_INPUT_KEYS = (0x01, 0x02, 0x04, 0x05, 0x06, 0x10, 0x11, 0x12, 0x5B, 0x5C)
_LEFT_DOWN = 0x0002
_LEFT_UP = 0x0004
_HANDLE = ctypes.c_void_p
_DWORD = ctypes.c_uint32
_INT = ctypes.c_int32


class _Point(ctypes.Structure):
    _fields_ = [("x", _INT), ("y", _INT)]


class _Rect(ctypes.Structure):
    _fields_ = [("left", _INT), ("top", _INT), ("right", _INT), ("bottom", _INT)]

    def contains(self, point):
        x, y = point
        return self.left <= x < self.right and self.top <= y < self.bottom


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = [
        ("size", _DWORD), ("flags", _DWORD), ("active", _HANDLE), ("focus", _HANDLE),
        ("capture", _HANDLE), ("menu_owner", _HANDLE), ("move_size", _HANDLE),
        ("caret", _HANDLE), ("caret_rect", _Rect),
    ]


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", _INT), ("dy", _INT), ("mouse_data", _DWORD), ("flags", _DWORD),
        ("time", _DWORD), ("extra_info", ctypes.c_size_t),
    ]


class _Input(ctypes.Structure):
    # MOUSEINPUT is the largest member of the Win32 INPUT union.
    _fields_ = [("kind", _DWORD), ("mouse", _MouseInput)]


@dataclass(frozen=True)
class _Target:
    hwnd: int
    pid: int
    title: str
    dpi: int
    client: tuple[int, int, int, int]

    @property
    def size(self):
        left, top, right, bottom = self.client
        return right - left, bottom - top


_last_capture: ContextVar[_Target | None] = ContextVar("windows_wpk_capture", default=None)


class _Win32:
    def __init__(self):
        user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        pid_pointer = ctypes.POINTER(_DWORD)
        point_pointer = ctypes.POINTER(_Point)
        rect_pointer = ctypes.POINTER(_Rect)
        text_buffer = ctypes.POINTER(ctypes.c_wchar)

        self.foreground = _bind(user, "GetForegroundWindow", (), _HANDLE)
        self.is_visible = _bind(user, "IsWindowVisible", (_HANDLE,), _INT)
        self.is_enabled = _bind(user, "IsWindowEnabled", (_HANDLE,), _INT)
        self.is_minimized = _bind(user, "IsIconic", (_HANDLE,), _INT)
        self.ancestor = _bind(user, "GetAncestor", (_HANDLE, _DWORD), _HANDLE)
        self.window_above = _bind(user, "GetWindow", (_HANDLE, _DWORD), _HANDLE)
        self.window_rect = _bind(user, "GetWindowRect", (_HANDLE, rect_pointer), _INT)
        self.window_text = _bind(user, "GetWindowTextW", (_HANDLE, text_buffer, _INT), _INT)
        self.window_class = _bind(user, "GetClassNameW", (_HANDLE, text_buffer, _INT), _INT)
        self.window_process = _bind(user, "GetWindowThreadProcessId", (_HANDLE, pid_pointer), _DWORD)
        self.window_dpi = _bind(user, "GetDpiForWindow", (_HANDLE,), _DWORD)
        self.client_rect = _bind(user, "GetClientRect", (_HANDLE, rect_pointer), _INT)
        self.client_to_screen = _bind(user, "ClientToScreen", (_HANDLE, point_pointer), _INT)
        self.point_monitor = _bind(user, "MonitorFromPoint", (_Point, _DWORD), _HANDLE)
        self.point_window = _bind(user, "WindowFromPoint", (_Point,), _HANDLE)
        self.system_metric = _bind(user, "GetSystemMetrics", (_INT,), _INT)
        self.set_dpi_context = _bind(user, "SetThreadDpiAwarenessContext", (_HANDLE,), _HANDLE)
        self.gui_thread_info = _bind(user, "GetGUIThreadInfo", (_DWORD, ctypes.POINTER(_GuiThreadInfo)), _INT)
        self.async_key = _bind(user, "GetAsyncKeyState", (_INT,), ctypes.c_int16)
        self.move_cursor = _bind(user, "SetCursorPos", (_INT, _INT), _INT)
        self.get_cursor = _bind(user, "GetCursorPos", (point_pointer,), _INT)
        self.send_input = _bind(user, "SendInput", (_DWORD, ctypes.POINTER(_Input), _INT), _DWORD)
        self.open_process = _bind(kernel, "OpenProcess", (_DWORD, _INT, _DWORD), _HANDLE)
        self.process_image = _bind(kernel, "QueryFullProcessImageNameW", (_HANDLE, _DWORD, text_buffer, pid_pointer), _INT)
        self.close_handle = _bind(kernel, "CloseHandle", (_HANDLE,), _INT)
        self.dwm_attribute = _bind(dwm, "DwmGetWindowAttribute", (_HANDLE, _DWORD, _HANDLE, _DWORD), _INT)

    @contextmanager
    def physical_pixels(self):
        previous = self.set_dpi_context(_HANDLE(-4))  # Per-monitor-v2 DPI awareness.
        if not previous:
            raise WindowCaptureError("Windows 无法切换到精确的屏幕像素坐标。")
        try:
            yield
        finally:
            if not self.set_dpi_context(previous):
                raise WindowCaptureError("Windows 无法恢复线程的 DPI 设置。")

    def pid(self, hwnd):
        pid = _DWORD()
        if self.window_process(hwnd, ctypes.byref(pid)):
            return pid.value
        return None

    def process_executable(self, pid):
        process = self.open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION.
        if not process:
            return None
        try:
            path = ctypes.create_unicode_buffer(32768)
            length = _DWORD(len(path))
            if self.process_image(process, 0, path, ctypes.byref(length)):
                return ntpath.basename(path.value).casefold()
            return None
        finally:
            self.close_handle(process)

    def current_target(self, hwnd):
        if (
            self.foreground() != hwnd or self.ancestor(hwnd, 2) != hwnd
            or not self.is_visible(hwnd) or not self.is_enabled(hwnd) or self.is_minimized(hwnd)
        ):
            return None
        title = ctypes.create_unicode_buffer(512)
        window_class = ctypes.create_unicode_buffer(256)
        length = self.window_text(hwnd, title, len(title))
        if (
            not 0 < length < len(title) - 1 or not _WPK_TITLE.match(title.value.strip())
            or not self.window_class(hwnd, window_class, len(window_class))
            or window_class.value != "Chrome_WidgetWin_1"
        ):
            return None
        pid = self.pid(hwnd)
        dpi = self.window_dpi(hwnd)
        if not pid or self.process_executable(pid) not in _CHROMIUM_EXECUTABLES or not dpi:
            return None

        rect = _Rect()
        if not self.client_rect(hwnd, ctypes.byref(rect)) or (rect.left, rect.top) != (0, 0):
            return None
        origin = _Point(0, 0)
        end = _Point(rect.right, rect.bottom)
        if not self.client_to_screen(hwnd, ctypes.byref(origin)) or not self.client_to_screen(hwnd, ctypes.byref(end)):
            return None
        x = self.system_metric(76)  # Virtual desktop includes monitors with negative origins.
        y = self.system_metric(77)
        width = self.system_metric(78)
        height = self.system_metric(79)
        if not (x <= origin.x < end.x <= x + width and y <= origin.y < end.y <= y + height):
            return None
        return _Target(hwnd, pid, title.value, dpi, (origin.x, origin.y, end.x, end.y))

    def unobscured(self, target, point):
        native_point = _Point(*point)
        if not self.point_monitor(native_point, 0):  # MONITOR_DEFAULTTONULL.
            return False
        hit = self.point_window(native_point)
        if not hit or self.ancestor(hit, 2) != target.hwnd or self.pid(hit) != target.pid:
            return False

        # WindowFromPoint can skip overlays which still obscure the screenshot.
        above = target.hwnd
        seen = {above}
        for _ in range(128):
            above = self.window_above(above, 3)  # GW_HWNDPREV.
            if not above:
                return True
            if above in seen:
                return False
            seen.add(above)
            if not self.is_visible(above) or self.is_minimized(above):
                continue
            cloaked = _DWORD()
            if self.dwm_attribute(above, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 and cloaked.value:
                continue  # DWMWA_CLOAKED, including windows on other virtual desktops.
            rect = _Rect()
            if not self.window_rect(above, ctypes.byref(rect)) or rect.contains(point):
                return False
        return False

    def input_idle(self, hwnd):
        state = _GuiThreadInfo(size=ctypes.sizeof(_GuiThreadInfo))
        if (
            not self.gui_thread_info(0, ctypes.byref(state)) or state.active != hwnd
            or state.capture or state.flags & _BUSY_GUI_FLAGS
        ):
            return False
        return all(not (self.async_key(key) & 0x8000) for key in _INPUT_KEYS)

    def cursor_is_at(self, point):
        cursor = _Point()
        return bool(self.get_cursor(ctypes.byref(cursor))) and (cursor.x, cursor.y) == point

    def send_mouse(self, *flags):
        events = (_Input * len(flags))(*(_Input(0, _MouseInput(flags=value)) for value in flags))
        return self.send_input(len(events), events, ctypes.sizeof(_Input))


@lru_cache(maxsize=1)
def _native():
    if sys.platform != "win32":
        raise WindowCaptureError("当前系统不提供 Windows 窗口接口。")
    try:
        return _Win32()
    except (AttributeError, OSError) as error:
        raise WindowCaptureError("自动点头像需要支持每显示器 DPI 的 Windows 10 或更新版本。") from error


def _valid_hwnd(hwnd):
    return type(hwnd) is int and 0 < hwnd < 1 << (ctypes.sizeof(_HANDLE) * 8)


def find_wpk_window() -> int | None:
    """Find WePoker in the foreground Chrome or Edge window, if present."""
    native = _native()
    hwnd = native.foreground()
    if not _valid_hwnd(hwnd):
        return None
    with native.physical_pixels():
        return hwnd if native.current_target(hwnd) else None


def capture_window(hwnd: int):
    """Capture the verified foreground client area and remember its geometry."""
    _last_capture.set(None)
    if not _valid_hwnd(hwnd):
        raise WindowCaptureError("自动点头像需要有效的 Windows 浏览器窗口句柄。")
    native = _native()
    image = None
    try:
        from PIL import ImageGrab

        with native.physical_pixels():
            target = native.current_target(hwnd)
            if target is None:
                raise WindowCaptureError("请将 Chrome 或 Edge 的 WePoker 牌桌放到前台。")
            image = ImageGrab.grab(window=hwnd)
            if image.size != target.size or native.current_target(hwnd) != target:
                raise WindowCaptureError("Windows 窗口或 DPI 已改变，等待下一帧。")
        _last_capture.set(target)
        return image
    except WindowCaptureError:
        raise
    except (ImportError, OSError) as error:
        raise WindowCaptureError("无法读取前台 WePoker 牌桌窗口。") from error
    finally:
        if image is not None and _last_capture.get() is None:
            image.close()


def click_window_point(hwnd: int, image_size: tuple[int, int], point: tuple[float, float]) -> None:
    """Send one left click to a current Windows client-area screenshot point."""
    if (
        not _valid_hwnd(hwnd)
        or not isinstance(image_size, (tuple, list)) or len(image_size) != 2
        or any(type(size) is not int or not 0 < size < (1 << 31) for size in image_size)
        or not isinstance(point, (tuple, list)) or len(point) != 2 or not all(map(_finite_number, point))
        or not all(0 <= coord < size for coord, size in zip(point, image_size))
    ):
        raise WindowInputError("点击坐标必须位于用于识别的 Windows 窗口截图内。")
    target = _last_capture.get()
    _last_capture.set(None)
    if target is None or target.hwnd != hwnd or target.size != tuple(image_size):
        raise WindowInputError("点击需要当前 Windows WePoker 窗口的最新截图。")

    screen_point = (target.client[0] + int(point[0]), target.client[1] + int(point[1]))
    try:
        native = _native()
        with native.physical_pixels():
            _perform_click(native, target, screen_point)
    except WindowCaptureError as error:
        raise WindowInputError(str(error)) from error


def _perform_click(native, target, screen_point):
    hwnd = target.hwnd
    if native.current_target(hwnd) != target or not native.unobscured(target, screen_point):
        raise WindowInputError("WePoker 窗口已变更或点击位置被遮挡。")
    if not native.input_idle(hwnd):
        raise WindowInputError("检测到鼠标、菜单或修饰键正在使用。")
    if not native.move_cursor(*screen_point):
        raise WindowInputError("Windows 无法将鼠标移动到目标窗口。")
    if (
        native.current_target(hwnd) != target or not native.unobscured(target, screen_point)
        or not native.input_idle(hwnd) or not native.cursor_is_at(screen_point) or native.foreground() != hwnd
    ):
        raise WindowInputError("移动指针后窗口、遮挡或鼠标状态发生变更。")
    inserted = native.send_mouse(_LEFT_DOWN, _LEFT_UP)
    if inserted == 2:
        return
    if inserted == 1 and native.send_mouse(_LEFT_UP) != 1:
        raise WindowInputError("Windows 只送出按下事件且未能补发释放，请检查鼠标状态。")
    raise WindowInputError("Windows 未能完整发送点击，请确认应用与浏览器的权限级别一致。")
