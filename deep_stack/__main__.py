"""Command-line entry points for observation, review and EV analysis."""

import argparse
from contextlib import contextmanager
import json
import math
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
from uuid import uuid4

from .observations import build_observation, make_draft


DEMO = {
    "hero_cards": ["As", "Ad"],
    "board": ["2c", "3d", "7h", "9s", "Jc"],
    "pot": 100,
    "seats": [
        {"seat": "hero", "stack": 200, "in_hand": True, "wager": 0},
        {
            "seat": "right_upper", "stack": 180, "in_hand": True, "wager": 20,
            "range": [
                {"cards": ["Js", "Jd"], "weight": 1},
                {"cards": ["Kc", "Kd"], "weight": 1},
                {"cards": ["Qs", "Qd"], "weight": 1},
                {"cards": ["Ac", "Kh"], "weight": 1},
            ],
        },
    ],
    "actions": [{"kind": "fold", "amount": 0}, {"kind": "call", "amount": 20}],
}


def _viewport(value):
    try:
        x, y, width, height = map(int, value.split(","))
        if x >= 0 and y >= 0 and width > 0 and height > 0:
            return x, y, width, height
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("需要 X,Y,W,H，原点非负，宽和高为正整数")


def _add_output(parser):
    parser.add_argument("-o", "--output", type=Path, help="保存 JSON 到文件，默认输出到终端")


def _add_layout(parser, *, auto=False):
    parser.add_argument("--layout", type=Path, help="使用 JSON 布局配置")
    parser.add_argument("--viewport", type=_viewport, metavar="X,Y,W,H", help="指定牌桌截图像素坐标")
    if auto:
        parser.add_argument("--auto-viewport", action="store_true", help="自动定位横向绿色牌桌")


def _add_window(parser):
    parser.add_argument("--window-id", type=int, metavar="ID",
                        help="指定 macOS 窗口 ID，默认优先捕捉 WePoker-H5")


def _add_h5_listener(parser, default_port):
    parser.add_argument("--port", type=int, default=default_port, help="本机监听端口，0 表示自动选择")
    parser.add_argument("--pair-file", type=Path, help="指定配对文件位置，默认在本机临时目录创建")
    parser.add_argument("--json", action="store_true", help="逐行输出 JSON 事件")


def _parser():
    parser = argparse.ArgumentParser(description="从本地牌桌截图提取观察记录并复盘筹码 EV。")
    commands = parser.add_subparsers(dest="command", required=True)

    observe = commands.add_parser("observe", help="读取同一手牌按先后排列的截图")
    observe.add_argument("images", metavar="IMAGE", type=Path, nargs="+")
    _add_layout(observe, auto=True)
    _add_output(observe)

    capture = commands.add_parser("capture", help="从当前屏幕采集一次牌桌截图")
    _add_layout(capture, auto=True)
    _add_window(capture)
    _add_output(capture)

    h5 = commands.add_parser("h5-inspect", help="只读分析本地 Chrome WebSocket 抓包")
    h5.add_argument("trace", type=Path, metavar="TRACE", help="Chrome HAR 或 DevTools Protocol Monitor JSON")
    h5.add_argument("--patterns", action="store_true", help="检测可能的周期性往返消息")
    _add_output(h5)

    packet_profile = commands.add_parser("h5-profile", help="对本机抓包做二进制封包结构统计")
    packet_profile.add_argument("trace", type=Path, metavar="TRACE", help="Chrome HAR 或 CDP JSON 事件列表")
    _add_output(packet_profile)

    h5_live = commands.add_parser("h5-live", help="接收 Chrome 扩展的实时 WebSocket 消息摘要")
    _add_h5_listener(h5_live, 8767)
    h5_live.add_argument("--journal", type=Path, metavar="JOURNAL", help="新建网络帧元数据 JSONL 时间轴")

    calibration = commands.add_parser("h5-calibrate", help="在同一时间轴上采集 H5 帧和可见牌局动作")
    calibration.add_argument("timeline", type=Path, metavar="TIMELINE", help="创建新的匿名校准 JSONL 文件")
    _add_h5_listener(calibration, 0)
    _add_layout(calibration)
    _add_window(calibration)
    calibration.add_argument("--spectate", action="store_true", help="观战模式，底部座位视为普通玩家")
    calibration.add_argument("--table-seats", type=int, choices=(8, 9), help="指定实体座位数量，默认自动判断")
    calibration.add_argument("--interval", type=float, default=0.75, help="屏幕采样间隔秒数")
    calibration.add_argument("--seconds", type=float, help="采集多少秒后停止，默认运行到 Ctrl+C")
    calibration.add_argument("--db", type=Path, help="可选的本机已核验玩家动作数据库")
    calibration.add_argument("--session", help="可选的数据库牌桌会话 ID")

    correlation = commands.add_parser("h5-correlate", help="为可见行动查找时间相近的 H5 消息候选")
    correlation.add_argument("timeline", type=Path, metavar="TIMELINE", help="h5-calibrate 的 JSONL 文件")
    correlation.add_argument("--window-ms", type=int, default=1500, help="事件前后的候选窗口毫秒数")
    _add_output(correlation)

    layout = commands.add_parser("layout-template", help="导出按位置比例配置的布局模板")
    layout.add_argument("--live", action="store_true", help="包含实时座位标记和按钮区域")
    _add_output(layout)

    live = commands.add_parser("live", help="连续识别画面并在确认轮到自己时输出条件 EV")
    _add_layout(live)
    _add_window(live)
    live.add_argument("--model", type=Path, help="对手范围、应答及牌桌金额口径配置")
    live.add_argument("--frames", type=Path, nargs="+", metavar="IMAGE", help="回放本地截图序列")
    live.add_argument("--samples", type=int, default=1500, help="每次蒙特卡洛计算的样本数")
    live.add_argument("--interval", type=float, default=0.75, help="屏幕采样间隔秒数")
    live.add_argument("--max-age", type=float, default=3.0, help="最大可用读数时长（秒）")
    live.add_argument("--json", action="store_true", help="逐行输出 JSON")
    live.add_argument("--session", help="牌桌会话 ID，默认生成新 ID")
    live.add_argument("--spectate", action="store_true", help="观战模式，底部座位视为普通玩家")
    live.add_argument("--table-seats", type=int, choices=(8, 9), help="指定实体座位数量，默认自动判断")
    live.add_argument("--scan-avatars", action="store_true", help="macOS 或 Windows 观战时依次打开未绑定玩家的头像")
    live.add_argument("--quiet-actions", action="store_true", help="隐藏动作和牌桌摘要，仍显示公共牌")
    live_log = live.add_mutually_exclusive_group()
    live_log.add_argument("--db", type=Path, help="本地玩家 SQLite 路径")
    live_log.add_argument("--no-player-log", action="store_true", help="关闭玩家动作归档")

    players = commands.add_parser("players", help="通过头像公开 ID 登记玩家和查询动作统计")
    players.add_argument("--db", type=Path, default=Path("players.sqlite3"))
    player_commands = players.add_subparsers(dest="player_command", required=True)
    bind = player_commands.add_parser("bind", help="将当前座位与核对过的固定玩家 ID 绑定")
    bind.add_argument("session")
    bind.add_argument("seat")
    bind.add_argument("id")
    bind.add_argument("--name", help="截图中可见的昵称，用来发现座位变化")
    unbind = player_commands.add_parser("unbind", help="解除会话内的座位绑定")
    unbind.add_argument("session")
    unbind.add_argument("seat")
    bindings = player_commands.add_parser("bindings", help="列出会话内的座位绑定")
    bindings.add_argument("session")
    show = player_commands.add_parser("show", help="查询玩家统计")
    show.add_argument("id")
    show.add_argument("--events", type=int, metavar="N", help="附带最近 N 条动作")
    show.add_argument("--profiles", type=int, metavar="N", help="附带最近 N 条俱乐部公开画像")
    read_id = player_commands.add_parser("read-id", help="从已裁到 ID 行的头像弹窗截图读取 ID")
    read_id.add_argument("image", type=Path)
    read_id.add_argument("--viewport", type=_viewport, metavar="X,Y,W,H")

    draft = commands.add_parser("draft", help="将观察记录转换为需核对的分析输入")
    draft.add_argument("observation", type=Path)
    _add_output(draft)

    analysis = commands.add_parser("analyze", help="计算已核对输入的各候选动作筹码 EV")
    analysis.add_argument("state", type=Path)
    analysis.add_argument("--samples", type=int, default=4000)
    analysis.add_argument("--seed", type=int, default=1)
    _add_output(analysis)

    demo = commands.add_parser("demo", help="运行一个合成的单挑河牌场景")
    demo.add_argument("--state", action="store_true", help="输出示例牌局输入")
    _add_output(demo)
    return parser


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} 第 {exc.lineno} 行第 {exc.colno} 列的 JSON 无效：{exc.msg}") from exc


def _write_json(result, path):
    content = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if path is None:
        sys.stdout.write(content)
        return
    # Replace only once the complete result has been written.
    tmp = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
            tmp = Path(file.name)
            file.write(content)
        tmp.replace(path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _observe(images, viewport=None, *, profile=None, auto_viewport=False, window_capture=False):
    from .recognition import recognize

    frames = []
    for image in images:
        try:
            frame = recognize(image, viewport=viewport, profile=profile, auto_viewport=auto_viewport,
                              window_capture=window_capture)
        except (OSError, ValueError) as exc:
            raise ValueError(f"{getattr(image, 'name', '屏幕')}：{exc}") from exc
        frames.append({**frame, "source": getattr(image, "name", "screen")})
    return build_observation(frames)


def _capture(args, profile):
    if (sys.platform != "darwin" or getattr(sys, "frozen", False)
            or args.viewport or args.auto_viewport or args.window_id is not None):
        from .live import LiveCapture

        image, captured_window = LiveCapture(args.window_id, prefer_window=args.viewport is None).read()
        return _observe([image], args.viewport, profile=profile, auto_viewport=args.viewport is None,
                        window_capture=captured_window is not None)
    with TemporaryDirectory(prefix="deep-stack-") as folder:
        path = Path(folder) / "capture.png"
        print("请框选网页内完整牌桌，按 Esc 取消。", file=sys.stderr)
        result = subprocess.run(
            ["/usr/sbin/screencapture", "-i", "-s", "-x", str(path)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode or not path.is_file():
            detail = result.stderr.strip()
            raise ValueError(f"截图取消或失败{f'：{detail}' if detail else ''}")
        return _observe([path], profile=profile)


def _players(args):
    if args.player_command == "read-id":
        from .recognition import read_player_id

        return {"player_id": read_player_id(args.image, viewport=args.viewport)}

    from .player_store import PlayerStore

    with PlayerStore(args.db) as store:
        if args.player_command == "bind":
            store.bind(args.session, args.seat, args.id, name=args.name)
            return {"bindings": store.bindings(args.session)}
        if args.player_command == "unbind":
            store.unbind(args.session, args.seat)
            return {"bindings": store.bindings(args.session)}
        if args.player_command == "bindings":
            return {"bindings": store.bindings(args.session)}
        result = store.stats(args.id)
        if args.events is not None:
            result["events"] = store.events(args.id, limit=args.events)
        if args.profiles is not None:
            result["profiles"] = store.profiles(args.id, limit=args.profiles)
        return result


@contextmanager
def _h5_pairing_file(path):
    if path is not None:
        yield path.expanduser()
    else:
        with TemporaryDirectory(prefix="deep-stack-h5-") as folder:
            yield Path(folder) / "h5-pairing.json"


def _run_h5_live(args):
    from .h5_live import live_server, serve_live
    from .h5_traffic import TrafficAnalyzer

    traffic = TrafficAnalyzer()
    journal_path = args.journal.expanduser() if args.journal is not None else None
    journal = None
    journal_failure = None

    def emit(event):
        nonlocal journal_failure
        if journal is not None and event["type"] == "frames":
            try:
                journal.h5(event)
            except (OSError, ValueError) as error:
                journal_failure = error
                raise
        if journal_path is not None and event["type"] == "listening":
            event = {**event, "journal": str(journal_path)}
        if args.json:
            print(json.dumps(event, ensure_ascii=False, allow_nan=False), flush=True)
        elif event["type"] == "listening":
            print(f"H5 本机监听 {event['url']}\n配对文件 {event['pairing_file']}", flush=True)
            if journal_path is not None:
                print(f"网络帧时间轴 {journal_path}", flush=True)
        elif event["type"] == "frames":
            for frame in event["frames"]:
                direction = "下行" if frame["direction"] == "received" else "上行"
                print(f"H5 +{frame['offset_ms'] / 1000:.3f}s {frame['connection']} {direction} "
                      f"{frame['size_bytes']} B｜{frame['encoding']}", flush=True)
        for pattern in traffic.update(event):
            if args.json:
                print(json.dumps(pattern, ensure_ascii=False, allow_nan=False), flush=True)
            else:
                print(pattern["message"], flush=True)

    with _h5_pairing_file(args.pair_file) as pairing:
        if journal_path is None:
            return serve_live(args.port, pairing, emit)
        from .h5_calibration import CalibrationRecorder

        if journal_path.resolve() == pairing.resolve():
            raise ValueError("网络帧时间轴与配对文件需要不同路径")
        with CalibrationRecorder(journal_path) as journal, live_server(args.port, pairing, emit) as server:
            while journal_failure is None:
                server.handle_request()
            raise journal_failure


def _run_h5_calibrate(args):
    from threading import Thread
    from time import monotonic

    from .h5_calibration import CalibrationRecorder
    from .h5_live import live_server
    from .layout import load_profile
    from .live import scan_live

    if not math.isfinite(args.interval) or args.interval <= 0:
        raise ValueError("采样间隔须为正数")
    if args.seconds is not None and (not math.isfinite(args.seconds) or args.seconds <= 0):
        raise ValueError("采集持续时间须为正数")
    if args.window_id is not None and (sys.platform != "darwin" or not 1 <= args.window_id <= 2**32 - 1):
        raise ValueError("--window-id 需要 macOS 的正整数窗口 ID")

    timeline = args.timeline.expanduser()
    database = args.db.expanduser() if args.db is not None else None
    profile = load_profile(args.layout.expanduser()) if args.layout else None
    listening = None
    failure = None

    def receive(event):
        nonlocal listening, failure
        if event["type"] == "listening":
            listening = event
        else:
            try:
                journal.h5(event)
            except Exception as error:
                failure = error
                raise

    def should_stop():
        if failure is not None:
            raise failure
        return deadline is not None and monotonic() >= deadline

    with _h5_pairing_file(args.pair_file) as pairing:
        paths = [path.resolve() for path in (timeline, pairing, database) if path is not None]
        if len(set(paths)) != len(paths):
            raise ValueError("校准时间轴、配对文件和数据库需要不同路径")
        with live_server(args.port, pairing, receive) as server, CalibrationRecorder(timeline) as journal:
            if args.json:
                print(json.dumps({**listening, "timeline": str(timeline)}, ensure_ascii=False), flush=True)
            else:
                print(f"H5 本机监听 {listening['url']}\n配对文件 {listening['pairing_file']}\n校准时间轴 {timeline}",
                      flush=True)
            deadline = monotonic() + args.seconds if args.seconds is not None else None
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                scan_live(frames=None, viewport=args.viewport, profile=profile, model={}, samples=1500,
                          interval=args.interval, max_age=3, json_lines=args.json, database=database,
                          session=args.session or uuid4().hex[:12], window_id=args.window_id,
                          force_spectator=args.spectate, seat_capacity=args.table_seats,
                          show_actions=True, show_board=True, observe_only=True,
                          on_observation=journal.screen, should_stop=should_stop)
            finally:
                server.shutdown()
                thread.join()
            if failure is not None:
                raise failure


def _run(args):
    if args.command == "h5-live":
        return _run_h5_live(args)
    if args.command == "h5-profile":
        from .h5_packet_profile import profile_packets

        trace = args.trace.expanduser()
        if args.output is not None:
            args.output = args.output.expanduser()
            if args.output.resolve() == trace.resolve():
                raise ValueError("H5 封包报告需要与原始抓包使用不同文件")
        return profile_packets(trace)
    if args.command == "h5-calibrate":
        return _run_h5_calibrate(args)
    if args.command == "h5-correlate":
        from .h5_correlate import correlate_timeline

        timeline = args.timeline.expanduser()
        if args.output is not None:
            args.output = args.output.expanduser()
            if args.output.resolve() == timeline.resolve():
                raise ValueError("H5 对照报告需要与原始时间轴使用不同文件")
        return correlate_timeline(timeline, window_ms=args.window_ms)
    if args.command == "h5-inspect":
        from .h5_trace import inspect_trace

        trace = args.trace.expanduser()
        if args.output is not None:
            args.output = args.output.expanduser()
            if args.output.resolve() == trace.resolve():
                raise ValueError("H5 分析报告需要与原始抓包使用不同文件")
        report = inspect_trace(trace)
        if args.patterns:
            from .h5_traffic import summarize_traffic

            report["traffic_patterns"] = summarize_traffic(report["frames"])
        return report
    if args.command in {"observe", "capture", "live"}:
        from .layout import load_profile

        profile = load_profile(args.layout) if args.layout else None
        if args.command == "observe":
            return _observe(args.images, args.viewport, profile=profile, auto_viewport=args.auto_viewport)
        if args.window_id is not None:
            if sys.platform != "darwin" or not 1 <= args.window_id <= 2**32 - 1:
                raise ValueError("--window-id 需要 macOS 的正整数窗口 ID")
            if getattr(args, "frames", None):
                raise ValueError("--window-id 不能与 --frames 同时使用")
        if args.command == "capture":
            return _capture(args, profile)
        from .live import scan_live

        if not all(math.isfinite(value) and value > 0 for value in (args.interval, args.max_age)):
            raise ValueError("采样间隔和读数时限须为正数")
        if not 100 <= args.samples <= 200000:
            raise ValueError("samples 必须是 100 到 200000 之间的整数")
        if args.scan_avatars and (
            sys.platform not in {"darwin", "win32"} or args.frames or args.no_player_log or not args.spectate
        ):
            raise ValueError("--scan-avatars 需要 macOS 或 Windows 实时窗口、--spectate 和本机玩家记录")
        model = _read_json(args.model) if args.model else {"pot_includes_wagers": profile is None}
        db = args.db if args.frames else (None if args.no_player_log else args.db or Path("players.sqlite3"))
        scan_live(frames=args.frames, viewport=args.viewport, profile=profile, model=model,
                  samples=args.samples, interval=args.interval, max_age=args.max_age,
                  json_lines=args.json, database=db, session=args.session or uuid4().hex[:12],
                  window_id=args.window_id, force_spectator=args.spectate, seat_capacity=args.table_seats,
                  show_actions=not args.quiet_actions, show_board=True, scan_avatars=args.scan_avatars)
        return None
    if args.command == "layout-template":
        from .layout import profile_template
        from .recognition import live_profile

        return profile_template(live_profile() if args.live else None)
    if args.command == "players":
        return _players(args)
    if args.command == "draft":
        try:
            return make_draft(_read_json(args.observation))
        except (KeyError, TypeError) as exc:
            raise ValueError(f"观察记录字段无效：{exc}") from exc

    from .ev import analyze

    if args.command == "demo":
        return DEMO if args.state else analyze(DEMO)
    return analyze(_read_json(args.state), samples=args.samples, seed=args.seed)


def main(argv=None):
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    try:
        result = _run(args)
        if result is not None:
            _write_json(result, getattr(args, "output", None))
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, subprocess.TimeoutExpired):
        if args.command in {"live", "h5-live", "h5-calibrate"}:
            return 0
        print("操作已中断或超时。", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
