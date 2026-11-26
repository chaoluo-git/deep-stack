"""Collect visible observations from timestamp-ordered images of one hand."""

from copy import deepcopy


STREETS = {0: "preflop", 3: "flop", 4: "turn", 5: "river"}
FORMAT = "deep-stack-observation-v1"


def build_observation(frames):
    """Keep raw frames and an index of distinct observed action labels."""
    if not frames:
        raise ValueError("至少需要一帧牌桌画面")

    latest_hero = None
    latest_board = []
    latest_street = -1
    labels = {}
    terminal_actions = {}
    events = []
    warnings = []
    for index, frame in enumerate(frames, start=1):
        hero = frame["hero_cards"]
        board = frame["board"]
        street = frame.get("street")
        if board is not None:
            street = STREETS.get(len(board))
            if board[:len(latest_board)] != latest_board:
                raise ValueError(f"第 {index} 帧与之前的公共牌冲突。请核对识别或拆分手牌")
            latest_board = board
        if hero is not None:
            if latest_hero is not None and set(hero) != set(latest_hero):
                raise ValueError(f"第 {index} 帧的手牌不同。请核对识别或拆分手牌")
            latest_hero = hero
        if street in STREETS.values():
            order = tuple(STREETS.values()).index(street)
            if order < latest_street:
                raise ValueError(f"第 {index} 帧的街次向前跳转。请按顺序传入同一手截图")
            latest_street = order
        else:
            street = None

        for seat in frame["seats"]:
            action = seat["action"]
            if action is None:
                continue
            seat_id = seat["seat"]
            key = (street, seat_id)
            wager = seat["wager"]
            previous = terminal_actions.get(seat_id) if action in ("fold", "all_in") else None
            if previous and previous["action"] == action and previous["street"] != street:
                continue
            previous = previous or labels.get(key)
            if previous and previous["action"] == action:
                if previous["wager"] is None and wager is not None:
                    previous["wager"] = wager
                    previous["wager_frame"] = index
                    continue
                if wager is None or previous["wager"] == wager:
                    continue
            event = {
                "frame": index,
                "street": street,
                "seat": seat_id,
                "action": action,
                "wager": wager,
                "wager_frame": index if wager is not None else None,
            }
            events.append(event)
            labels[key] = event
            if action in ("fold", "all_in"):
                terminal_actions[seat_id] = event
        warnings.extend(f"第 {index} 帧：{warning}" for warning in frame.get("warnings", []))

    warnings.append("动作记录来自可见标签。截图之间可能漏掉动作，同一帧内的先后未知。")
    return {"format": FORMAT, "frames": frames, "events": events, "warnings": warnings}


def make_draft(observation):
    """Prefill an editable snapshot without inferring live seats or legal moves."""
    if not isinstance(observation, dict) or observation.get("format") != FORMAT:
        raise ValueError("draft 需要 observe 或 capture 导出的观察记录")
    frames = observation.get("frames")
    if not isinstance(frames, list) or not frames:
        raise ValueError("观察记录缺少截图帧")

    names = {}
    hero = None
    conflicts = set()
    warnings = [
        "按最终画面核对底池、各席筹码与本街已投入，补齐各席 in_hand 并删除确认空席。",
        "确认轮到自己行动、已知手牌和公共牌，再填写合法动作及对应假设。",
    ]
    for frame in frames:
        if frame["hero_cards"] is not None:
            hero = frame["hero_cards"]
        for seat in frame["seats"]:
            name = seat["name"]
            if name is not None:
                seat_id = seat["seat"]
                if seat_id in names and names[seat_id] != name:
                    conflicts.add(seat_id)
                names[seat_id] = name

    if conflicts:
        warnings.append(f"这些座位的识别昵称出现变化，需人工核对：{', '.join(sorted(conflicts))}。")

    latest = frames[-1]
    if latest["hero_cards"] is None and hero is not None:
        warnings.append("手牌沿用了同一手较早帧的读数，请核对。")
    warnings.extend(latest.get("warnings", []))
    seats = []
    for seat in latest["seats"]:
        seat_id = seat["seat"]
        seats.append({
            "seat": seat_id,
            "name": names.get(seat_id) if seat_id not in conflicts else seat["name"],
            "stack": seat["stack"],
            "in_hand": False if seat["action"] == "fold" else None,
            "wager": seat["wager"],
        })

    return {
        "hero_cards": deepcopy(hero),
        "board": deepcopy(latest["board"]),
        "pot": latest["pot"],
        "seats": seats,
        "actions": [],
        "_review": warnings,
    }
