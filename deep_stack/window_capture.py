"""Find an on-screen WePoker window and capture its contents."""

import ctypes
from pathlib import Path
import plistlib
import re
import subprocess
import sys
from tempfile import TemporaryDirectory


_CHROME_OWNERS = frozenset((
    "google chrome", "google chrome beta", "google chrome dev", "google chrome canary", "chromium",
))
_WPK_OWNERS = frozenset(("wepoker", "wepoker-h5", "wpk"))
_WPK_TITLE = re.compile(r"^(?:wepoker|wpk|h5\.wpk\.com)(?:$|[\s\-–—:|])", re.IGNORECASE)
_COLLECTOR_TITLE = re.compile(
    r"WPK 帧采集(?:\s*[-–—]\s*(?:Google Chrome|Chromium|Microsoft Edge)(?: (?:Beta|Dev|Canary))?)?",
    re.IGNORECASE,
)
_ON_SCREEN_WINDOWS = (1 << 0) | (1 << 4)
_PLIST_XML = 100


class WindowCaptureError(OSError):
    """The target window is ambiguous or its contents could not be captured."""


def _is_wpk_title(title):
    if not isinstance(title, str):
        return False
    title = title.strip()
    return bool(_WPK_TITLE.match(title) and not _COLLECTOR_TITLE.fullmatch(title))


def _onscreen_windows():
    graphics = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    ref = ctypes.c_void_p

    window_info = graphics.CGWindowListCopyWindowInfo
    window_info.argtypes = (ctypes.c_uint32, ctypes.c_uint32)
    window_info.restype = ref
    serialize = foundation.CFPropertyListCreateData
    serialize.argtypes = (ref, ref, ctypes.c_long, ctypes.c_ulong, ctypes.POINTER(ref))
    serialize.restype = ref
    length = foundation.CFDataGetLength
    length.argtypes = (ref,)
    length.restype = ctypes.c_long
    contents = foundation.CFDataGetBytePtr
    contents.argtypes = (ref,)
    contents.restype = ref
    release = foundation.CFRelease
    release.argtypes = (ref,)
    release.restype = None

    windows = window_info(_ON_SCREEN_WINDOWS, 0)
    if not windows:
        return []
    try:
        data = serialize(None, windows, _PLIST_XML, 0, None)
        if not data:
            return []
        try:
            entries = plistlib.loads(ctypes.string_at(contents(data), length(data)))
            return entries if isinstance(entries, list) else []
        finally:
            release(data)
    finally:
        release(windows)


def _wpk_window_id(entries):
    matches = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("kCGWindowLayer") != 0 or not entry.get("kCGWindowIsOnscreen", True):
            continue
        bounds = entry.get("kCGWindowBounds")
        if not isinstance(bounds, dict) or not all(
            type(bounds.get(axis)) in (int, float) and bounds[axis] > 0 for axis in ("Width", "Height")
        ):
            continue

        owner = entry.get("kCGWindowOwnerName")
        if not isinstance(owner, str):
            continue
        owner = owner.strip().casefold()
        if owner not in _WPK_OWNERS:
            if owner not in _CHROME_OWNERS or not _is_wpk_title(entry.get("kCGWindowName")):
                continue

        window_id = entry.get("kCGWindowNumber")
        if type(window_id) is int and window_id > 0:
            matches.add(window_id)

    if len(matches) > 1:
        raise WindowCaptureError("检测到多个 WePoker 窗口，请只保留一个可见的牌桌窗口。")
    return next(iter(matches), None)


def find_wpk_window():
    """Return the sole on-screen target if macOS supplies its window metadata."""
    if sys.platform != "darwin":
        return None
    try:
        entries = _onscreen_windows()
    except (OSError, AttributeError, ValueError, plistlib.InvalidFileException):
        return None
    return _wpk_window_id(entries)


def capture_window(window_id):
    """Read one target window into memory and release any temporary screenshot."""
    if type(window_id) is not int or window_id <= 0:
        raise WindowCaptureError("窗口 ID 必须是正整数。")
    try:
        from PIL import Image
    except ImportError as error:
        raise WindowCaptureError("窗口截图需要 Pillow，请安装 .[live] 或 .[ocr]。") from error

    try:
        if sys.platform == "darwin":
            with TemporaryDirectory() as directory:
                path = Path(directory) / "window.png"
                subprocess.run(
                    ["/usr/sbin/screencapture", "-l", str(window_id), "-o", "-a", "-x", str(path)],
                    check=True, capture_output=True, timeout=5,
                )
                with Image.open(path) as image:
                    image.load()
                    return image.copy()
        if sys.platform == "win32":
            from PIL import ImageGrab

            return ImageGrab.grab(window=window_id)
    except (OSError, subprocess.SubprocessError) as error:
        raise WindowCaptureError("读取目标窗口失败，请确认 Chrome 窗口可用且已授权录屏。") from error
    raise WindowCaptureError("当前系统不支持按窗口 ID 截图。")
