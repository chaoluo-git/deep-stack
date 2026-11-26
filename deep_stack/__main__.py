"""Command-line entry points for observation, review and EV analysis."""

import argparse
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


def _parser():
    parser = argparse.ArgumentParser(description="从本地牌桌截图提取观察记录并复盘筹码 EV。")
    commands = parser.add_subparsers(dest="command", required=True)

    observe = commands.add_parser("observe", help="读取同一手牌按先后排列的截图")
    observe.add_argument("images", metavar="IMAGE", type=Path, nargs="+")
    _add_layout(observe, auto=True)
    _add_output(observe)

    capture = commands.add_parser("capture", help="从当前屏幕采集一次牌桌截图")
    _add_layout(capture, auto=True)
    _add_output(capture)

    layout = commands.add_parser("layout-template", help="导出按位置比例配置的布局模板")
    layout.add_argument("--live", action="store_true", help="包含实时座位标记和按钮区域")
    _add_output(layout)

    live = commands.add_parser("live", help="连续识别画面并在确认轮到自己时输出条件 EV")
    _add_layout(live)
    live.add_argument("--model", type=Path, help="对手范围、应答及牌桌金额口径配置")
    live.add_argument("--frames", type=Path, nargs="+", metavar="IMAGE", help="回放本地截图序列")
    live.add_argument("--samples", type=int, default=1500, help="每次蒙特卡洛计算的样本数")
    live.add_argument("--interval", type=float, default=0.75, help="屏幕采样间隔秒数")
    live.add_argument("--max-age", type=float, default=3.0, help="最大可用读数时长（秒）")
    live.add_argument("--json", action="store_true", help="逐行输出 JSON")
    live.add_argument("--session", help="牌桌会话 ID，默认生成新 ID")
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


def _observe(images, viewport=None, *, profile=None, auto_viewport=False):
    from .recognition import recognize

    frames = []
    for image in images:
        try:
            frame = recognize(image, viewport=viewport, profile=profile, auto_viewport=auto_viewport)
        except (OSError, ValueError) as exc:
            raise ValueError(f"{getattr(image, 'name', '屏幕')}：{exc}") from exc
        frames.append({**frame, "source": getattr(image, "name", "screen")})
    return build_observation(frames)


def _capture(args, profile):
    if sys.platform != "darwin" or getattr(sys, "frozen", False) or args.viewport or args.auto_viewport:
        from .recognition import screen_image

        return _observe([screen_image()], args.viewport, profile=profile, auto_viewport=args.viewport is None)
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


def _run(args):
    if args.command in {"observe", "capture", "live"}:
        from .layout import load_profile

        profile = load_profile(args.layout) if args.layout else None
        if args.command == "observe":
            return _observe(args.images, args.viewport, profile=profile, auto_viewport=args.auto_viewport)
        if args.command == "capture":
            return _capture(args, profile)
        from .live import scan_live

        if not all(math.isfinite(value) and value > 0 for value in (args.interval, args.max_age)):
            raise ValueError("采样间隔和读数时限须为正数")
        if not 100 <= args.samples <= 200000:
            raise ValueError("samples 必须是 100 到 200000 之间的整数")
        model = _read_json(args.model) if args.model else {"pot_includes_wagers": profile is None}
        db = args.db if args.frames else (None if args.no_player_log else args.db or Path("players.sqlite3"))
        scan_live(frames=args.frames, viewport=args.viewport, profile=profile, model=model,
                  samples=args.samples, interval=args.interval, max_age=args.max_age,
                  json_lines=args.json, database=db, session=args.session or uuid4().hex[:12])
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
        if args.command == "live":
            return 0
        print("操作已中断或超时。", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
