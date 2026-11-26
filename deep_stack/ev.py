"""Conditional chip EV for a single no-limit Hold'em decision."""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from itertools import accumulate, combinations
from math import comb, fsum, isfinite, sqrt
from random import Random

RANKS = "23456789TJQKA"
SUITS = "cdhs"
DECK = tuple(range(52))
KINDS = {"fold", "check", "call", "bet", "raise", "all_in"}


def _card(value: object) -> int:
    if not isinstance(value, str) or len(value) != 2:
        raise ValueError("牌必须用两位格式表示，例如 As、Th")
    rank, suit = value[0].upper(), value[1].lower()
    if rank not in RANKS or suit not in SUITS:
        raise ValueError("牌的点数或花色无效")
    return 4 * RANKS.index(rank) + SUITS.index(suit)


def _cards(values: object, lengths: tuple[int, ...]) -> tuple[int, ...]:
    if not isinstance(values, list) or len(values) not in lengths:
        raise ValueError("手牌或公共牌数量与无限注德州扑克不符")
    cards = tuple(map(_card, values))
    if len(set(cards)) != len(cards):
        raise ValueError("同一张牌不能出现两次")
    return cards


def _number(value: object, label: str) -> float:
    if type(value) not in (int, float) or not 0 <= value <= 1e12 or not isfinite(value):
        raise ValueError(f"{label} 必须是 0 到 10^12 之间的有限数字")
    return float(value)


def _chips(value: object, label: str) -> Fraction:
    _number(value, label)
    # Use the decimal representation of JSON inputs to preserve small chip increments.
    return Fraction(str(value))


def _probability(value: object) -> float:
    probability = _number(value, "对手继续概率")
    if probability > 1:
        raise ValueError("对手继续概率必须在 0 到 1 之间")
    return probability


def _straight(ranks: set[int]) -> int:
    if 12 in ranks:
        ranks = ranks | {-1}
    for high in range(12, 2, -1):
        if all(rank in ranks for rank in range(high - 4, high + 1)):
            return high
    return -1


def _rank(cards: tuple[int, ...]) -> tuple[int, ...]:
    # Tuples are ordered by category first, then by the relevant kickers.
    ranks = [card // 4 for card in cards]
    counts = Counter(ranks)
    unique = sorted(counts, reverse=True)
    flush = ()
    for suit in range(4):
        suited = tuple(sorted((card // 4 for card in cards if card % 4 == suit), reverse=True))
        if len(suited) >= 5:
            straight_flush = _straight(set(suited))
            if straight_flush >= 0:
                return (8, straight_flush)
            flush = suited[:5]
            break
    quads = [rank for rank in unique if counts[rank] == 4]
    if quads:
        return (7, quads[0], next(rank for rank in unique if rank != quads[0]))
    trips = [rank for rank in unique if counts[rank] >= 3]
    pairs = [rank for rank in unique if counts[rank] >= 2]
    if trips and len(pairs) >= 2:
        return (6, trips[0], next(rank for rank in pairs if rank != trips[0]))
    if flush:
        return (5, *flush)
    straight = _straight(set(unique))
    if straight >= 0:
        return (4, straight)
    if trips:
        kickers = [rank for rank in unique if rank != trips[0]]
        return (3, trips[0], *kickers[:2])
    if len(pairs) >= 2:
        return (2, *pairs[:2], next(rank for rank in unique if rank not in pairs[:2]))
    if pairs:
        kickers = [rank for rank in unique if rank != pairs[0]]
        return (1, pairs[0], *kickers[:3])
    return (0, *unique[:5])


@dataclass(frozen=True)
class WeightedRange:
    hands: tuple[tuple[int, ...], ...]
    cumulative: tuple[float, ...]
    weights: tuple[float, ...]


def _range(raw: object, blocked: set[int]) -> WeightedRange | None:
    if raw == "random":
        return None
    if not isinstance(raw, list) or not raw:
        raise ValueError("对手范围必须是 random 或非空组合数组")
    seen = set()
    hands = []
    weights = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("范围组合必须提供 cards 和可选 weight")
        hand = tuple(sorted(_cards(entry.get("cards"), (2,))))
        if hand in seen:
            raise ValueError("对手范围内的同一组合只能出现一次")
        seen.add(hand)
        weight = _number(entry.get("weight", 1), "范围权重")
        if weight <= 0:
            raise ValueError("范围权重必须大于 0")
        if not blocked.intersection(hand):
            hands.append(hand)
            weights.append(weight)
    if not hands:
        raise ValueError("已知牌排除后，对手范围没有可用组合")
    scale = max(weights)
    scaled = tuple(weight / scale for weight in weights)
    return WeightedRange(tuple(hands), tuple(accumulate(scaled)), scaled)


@dataclass(frozen=True)
class Seat:
    name: str
    stack: Fraction
    live: bool
    wager: Fraction | None
    committed: Fraction | None
    range: WeightedRange | None


@dataclass(frozen=True)
class Response:
    seat: int
    amount: Fraction
    default: float
    by_hand: dict[tuple[int, ...], float]
    default_possible: bool = True

    def probability(self, hand: tuple[int, ...]) -> float:
        return self.by_hand.get(hand, self.default)

    def can_call(self) -> bool:
        return (self.default_possible and self.default > 0) or any(self.by_hand.values())

    def always_calls(self) -> bool:
        return (not self.default_possible or self.default == 1) and all(value == 1 for value in self.by_hand.values())


def _response(
    index: int, amount: Fraction, raw: object, blocked: set[int], distribution: WeightedRange | None
) -> Response:
    if not isinstance(raw, dict):
        return Response(index, amount, _probability(raw), {})
    default = _probability(raw.get("default"))
    overrides = raw.get("by_hand", [])
    if not isinstance(overrides, list):
        raise ValueError("对手 by_hand 必须是组合数组")
    by_hand = {}
    for entry in overrides:
        if not isinstance(entry, dict):
            raise ValueError("对手 by_hand 组合必须包含 cards 和 probability")
        hand = tuple(sorted(_cards(entry.get("cards"), (2,))))
        if hand in by_hand:
            raise ValueError("对手 by_hand 内的同一组合只能出现一次")
        by_hand[hand] = _probability(entry.get("probability"))
    if distribution is None:
        count = comb(len(DECK) - len(blocked), 2)
        by_hand = {hand: value for hand, value in by_hand.items() if not blocked.intersection(hand)}
    else:
        count = len(distribution.hands)
        by_hand = {hand: by_hand[hand] for hand in distribution.hands if hand in by_hand}
    return Response(index, amount, default, by_hand, len(by_hand) != count)


@dataclass(frozen=True)
class Plan:
    kind: str
    amount: Fraction
    responses: tuple[Response, ...]
    uses_default_response: bool


def _seats(raw: object, blocked: set[int], pot: Fraction, dead_money: Fraction) -> tuple[Seat, ...]:
    if not isinstance(raw, list) or not 2 <= len(raw) <= 10:
        raise ValueError("seats 必须包含 2 到 10 个座位")
    seats = []
    names = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("每个座位必须是对象")
        name, live = entry.get("seat"), entry.get("in_hand")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("座位 ID 必须是非空且唯一的字符串")
        if type(live) is not bool:
            raise ValueError("座位 in_hand 必须是布尔值")
        names.add(name)
        stack = _chips(entry.get("stack"), "座位 stack")
        wager = _chips(entry["wager"], "座位 wager") if "wager" in entry else None
        committed = _chips(entry["committed"], "座位 committed") if "committed" in entry else None
        if wager is not None and committed is not None and wager > committed:
            raise ValueError("当前街 wager 不能大于整手 committed")
        opponent_range = _range(entry.get("range", "random"), blocked) if live and name != "hero" else None
        seats.append(Seat(name, stack, live, wager, committed, opponent_range))
    for field in ("wager", "committed"):
        provided = sum(getattr(seat, field) is not None for seat in seats)
        if 0 < provided < len(seats):
            raise ValueError(f"{field} 必须对所有座位填写或全部省略")
    if seats[0].committed is not None and sum(seat.committed for seat in seats) + dead_money != pot:
        raise ValueError("所有 committed 与 dead_money 的总和必须等于 pot")
    if seats[0].wager is not None:
        wager_total = sum(seat.wager for seat in seats)
        if wager_total > pot - dead_money:
            raise ValueError("当前街 wager 的总和不能大于 pot 减去 dead_money")
    if not any(seat.name == "hero" and seat.live and seat.stack > 0 for seat in seats):
        raise ValueError("hero 必须在手牌中且仍有可操作筹码")
    if not any(seat.name != "hero" and seat.live for seat in seats):
        raise ValueError("至少需要一名仍在手牌中的对手")
    # Hero at index zero simplifies settlement and stable seat-indexed responses.
    return tuple(sorted(seats, key=lambda seat: seat.name != "hero"))


def _plan(
    entry: object, seats: tuple[Seat, ...], pot: Fraction, dead_money: Fraction, blocked: set[int]
) -> Plan:
    if not isinstance(entry, dict):
        raise ValueError("候选动作必须是对象")
    kind = entry.get("kind")
    if not isinstance(kind, str) or kind not in KINDS:
        raise ValueError("未知动作类型")
    cost = _chips(entry.get("amount"), "动作 amount")
    stack = seats[0].stack
    if cost > stack:
        raise ValueError("动作追加筹码超过 hero 剩余筹码")
    if (kind in {"fold", "check"}) != (cost == 0):
        raise ValueError("弃牌和过牌的 amount 必须为 0，其他动作必须为正数")
    responses = entry.get("responses", {})
    if not isinstance(responses, dict):
        raise ValueError("动作 responses 必须是座位到继续概率的对象")
    if kind == "fold":
        if responses:
            raise ValueError("弃牌动作不需要对手回应")
        return Plan(kind, cost, (), False)

    opponents = [(index, seat) for index, seat in enumerate(seats[1:], 1) if seat.live]
    committed = seats[0].committed is not None
    has_all_in_opponent = any(seat.stack == 0 for _, seat in opponents)
    if has_all_in_opponent and not committed:
        raise ValueError("有已全下对手时需要所有座位的 committed 才能结算分池")
    if seats[0].wager is None:
        if has_all_in_opponent:
            raise ValueError("有已全下对手时需要所有座位的 wager 才能确定待跟注金额")
        if kind == "check":
            if responses:
                raise ValueError("给出过牌时的对手回应前须提供每个座位的 wager")
            return Plan(kind, cost, (), False)
        if kind != "call" or len(opponents) != 1 or cost == stack:
            raise ValueError("需要每个座位的 wager 才能确定本街下注和剩余跟注")
        if cost > pot - dead_money:
            raise ValueError("单挑待跟注金额不能大于包含对手下注的 pot")
        if responses:
            raise ValueError("单挑跟注时对手无需再作跟注回应")
        if committed and opponents[0][1].committed < cost:
            raise ValueError("对手 committed 小于待跟注金额")
        return Plan(kind, cost, (), False)

    hero_wager = seats[0].wager
    high = max(hero_wager, *(seat.wager for _, seat in opponents))
    to_call = high - hero_wager
    target = hero_wager + cost
    above_high = target > high
    if kind == "check" and to_call != 0:
        raise ValueError("尚有筹码待跟注时不能过牌")
    if kind == "call" and cost != min(stack, to_call):
        raise ValueError("跟注 amount 与待跟注额或剩余筹码不一致")
    if kind == "bet" and (high > 0 or not above_high):
        raise ValueError("已有当前街下注时应使用 raise")
    if kind == "raise" and (high == 0 or not above_high):
        raise ValueError("加注必须在已有下注上把当前街金额提高")
    if kind == "all_in" and cost != stack:
        raise ValueError("全下 amount 必须等于 hero 剩余筹码")
    if target < high and not committed:
        raise ValueError("hero 无法全额跟注时需要 committed 才能退回对手超额筹码")
    if above_high and not any(seat.stack > 0 for _, seat in opponents):
        raise ValueError("其余玩家均已全下，不能继续下注或加注")

    model = []
    available_responses = set()
    uses_default_response = False
    for index, seat in opponents:
        gap = max(high, target) - seat.wager
        if gap == 0 or seat.stack == 0:
            continue
        available_responses.add(seat.name)
        if seat.name not in responses:
            if above_high:
                raise ValueError(f"主动动作缺少座位 {seat.name} 的继续概率")
            uses_default_response = True
        response = _response(index, min(gap, seat.stack), responses.get(seat.name, 1), blocked, seat.range)
        if response.can_call() and gap > seat.stack and not committed:
            raise ValueError("对手无法全额跟注时需要 committed 才能结算分池")
        model.append(response)
    if set(responses) - available_responses:
        raise ValueError("responses 只能包含有剩余筹码且尚需跟注的对手座位")
    return Plan(kind, cost, tuple(model), uses_default_response)


def _share(amount: Fraction, contenders: set[int], ranks: dict[int, tuple[int, ...]]) -> Fraction:
    if 0 not in contenders:
        return Fraction(0)
    best = max(ranks[player] for player in contenders)
    if ranks[0] != best:
        return Fraction(0)
    ties = sum(ranks[player] == best for player in contenders)
    return amount / ties


def _gain(
    plan: Plan,
    seats: tuple[Seat, ...],
    pot: Fraction,
    dead_money: Fraction,
    ranks: dict[int, tuple[int, ...]],
    continuing: set[int],
) -> float:
    if plan.kind == "fold":
        return 0.0
    additions = {0: plan.amount}
    eligible = set(ranks)
    for response in plan.responses:
        if response.seat in continuing:
            additions[response.seat] = response.amount
        else:
            eligible.remove(response.seat)
    if seats[0].committed is None:
        paid = _share(pot + sum(additions.values()), eligible, ranks)
        return float(paid - plan.amount)

    commitments = tuple(seat.committed + additions.get(index, 0) for index, seat in enumerate(seats))
    previous = Fraction(0)
    payout = _share(dead_money, eligible, ranks)
    for level in sorted(set(commitments)):
        increment = level - previous
        previous = level
        if increment == 0:
            continue
        contributors = {index for index, amount in enumerate(commitments) if amount >= level}
        if len(contributors) == 1:
            # A unique top contribution above every other contribution is returned.
            if 0 in contributors:
                payout += increment
            continue
        contenders = contributors & eligible
        if not contenders:
            raise ValueError("分池没有可领取的玩家，请核对已投入筹码与继续模型")
        payout += _share(len(contributors) * increment, contenders, ranks)
    return float(payout - plan.amount)


def _exact_river(
    plans: tuple[Plan, ...],
    seats: tuple[Seat, ...],
    pot: Fraction,
    dead_money: Fraction,
    hero: tuple[int, ...],
    board: tuple[int, ...],
) -> tuple[list[tuple[float, float]], int]:
    index, opponent = next((index, seat) for index, seat in enumerate(seats[1:], 1) if seat.live)
    available = [card for card in DECK if card not in hero + board]
    if opponent.range is None:
        hands = tuple(combinations(available, 2))
        weights = (1.0,) * len(hands)
    else:
        hands, weights = opponent.range.hands, opponent.range.weights
    total_weight = fsum(weights)
    hero_rank = _rank(hero + board)
    payoffs = [[] for _ in plans]
    for hand, weight in zip(hands, weights):
        ranks = {0: hero_rank, index: _rank(hand + board)}
        for values, plan in zip(payoffs, plans):
            if not plan.responses:
                payoffs_value = _gain(plan, seats, pot, dead_money, ranks, set())
            else:
                probability = plan.responses[0].probability(hand)
                payoffs_value = 0.0
                if probability:
                    payoffs_value += probability * _gain(plan, seats, pot, dead_money, ranks, {index})
                if probability < 1:
                    payoffs_value += (1 - probability) * _gain(plan, seats, pot, dead_money, ranks, set())
            values.append(weight * payoffs_value / total_weight)
    return [(fsum(values), 0.0) for values in payoffs], len(hands)


@dataclass
class Estimate:
    mean: float = 0.0
    sum_squares: float = 0.0

    def update(self, result: float, count: int) -> None:
        delta = result - self.mean
        self.mean += delta / count
        self.sum_squares += delta * (result - self.mean)


def _simulate(
    plans: tuple[Plan, ...],
    seats: tuple[Seat, ...],
    pot: Fraction,
    dead_money: Fraction,
    hero: tuple[int, ...],
    board: tuple[int, ...],
    samples: int,
    seed: int,
) -> list[tuple[float, float]]:
    fixed = set(hero + board)
    weighted = [(index, seat.range) for index, seat in enumerate(seats[1:], 1) if seat.live and seat.range is not None]
    random_seats = [index for index, seat in enumerate(seats[1:], 1) if seat.live and seat.range is None]
    live_indices = [index for index, seat in enumerate(seats[1:], 1) if seat.live]
    estimates = [Estimate() for _ in plans]
    rng = Random(seed)
    accepted = 0
    attempts = 0
    while accepted < samples:
        attempts += 1
        if attempts > max(4096, samples * 20):
            raise ValueError("这些对手范围之间有过多重复牌，无法在有限抽样次数内取得有效牌局")
        blocked = fixed.copy()
        holdings = {}
        for index, distribution in weighted:
            draw = rng.random() * distribution.cumulative[-1]
            hand = distribution.hands[min(bisect_right(distribution.cumulative, draw), len(distribution.hands) - 1)]
            if blocked.intersection(hand):
                break
            holdings[index] = hand
            blocked.update(hand)
        else:
            remaining = [card for card in DECK if card not in blocked]
            needed_board = 5 - len(board)
            dealt = rng.sample(remaining, needed_board + 2 * len(random_seats))
            full_board = board + tuple(dealt[:needed_board])
            for number, index in enumerate(random_seats):
                offset = needed_board + 2 * number
                holdings[index] = tuple(sorted(dealt[offset:offset + 2]))
            ranks = {index: _rank(hand + full_board) for index, hand in holdings.items()}
            ranks[0] = _rank(hero + full_board)
            draws = {index: rng.random() for index in live_indices}
            accepted += 1
            for estimate, plan in zip(estimates, plans):
                continuing = {
                    response.seat for response in plan.responses
                    if draws[response.seat] < response.probability(holdings[response.seat])
                }
                result = _gain(plan, seats, pot, dead_money, ranks, continuing)
                estimate.update(result, accepted)
    return [(estimate.mean, sqrt(max(estimate.sum_squares, 0) / (samples * (samples - 1)))) for estimate in estimates]


def analyze(state: dict, *, samples: int = 4000, seed: int = 1) -> dict:
    """Compare explicitly supplied candidate actions from the decision point.

    Amounts are new chips contributed now. The pot includes all contributions
    already made, including uncollected bets from the current street.
    Raises behind and later-street betting are outside the rollout.
    """
    if not isinstance(state, dict):
        raise ValueError("牌局快照必须是对象")
    if type(samples) is not int or not 100 <= samples <= 200000:
        raise ValueError("samples 必须是 100 到 200000 之间的整数")
    if type(seed) is not int:
        raise ValueError("seed 必须是整数")
    hero = _cards(state.get("hero_cards"), (2,))
    board = _cards(state.get("board"), (0, 3, 4, 5))
    blocked = set(hero + board)
    if len(blocked) != len(hero) + len(board):
        raise ValueError("hero 底牌与公共牌不能重复")
    pot = _chips(state.get("pot"), "底池 pot")
    dead_money = _chips(state.get("dead_money", 0), "主池死注 dead_money")
    if dead_money > pot:
        raise ValueError("dead_money 不能超过底池 pot")
    seats = _seats(state.get("seats"), blocked, pot, dead_money)
    candidates = state.get("actions")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("actions 必须提供至少一个合法候选动作")
    plans = tuple(_plan(candidate, seats, pot, dead_money, blocked) for candidate in candidates)
    if len({(plan.kind, plan.amount) for plan in plans}) != len(plans):
        raise ValueError("候选动作的类型和金额不得重复")
    if seats[0].committed is not None:
        # The most restrictive eligible set validates that no reachable side pot is orphaned.
        neutral_ranks = {index: (0,) for index, seat in enumerate(seats) if seat.live}
        for plan in plans:
            continuing = {response.seat for response in plan.responses if response.always_calls()}
            _gain(plan, seats, pot, dead_money, neutral_ranks, continuing)
    exact = len(board) == 5 and sum(seat.live for seat in seats) == 2
    if exact:
        estimates, count = _exact_river(plans, seats, pot, dead_money, hero, board)
    else:
        estimates = _simulate(plans, seats, pot, dead_money, hero, board, samples, seed)
        count = samples
    actions = [
        {
            "kind": plan.kind,
            "amount": float(plan.amount),
            "ev": mean,
            "std_error": stdev,
            "ci95": [mean - 1.96 * stdev, mean + 1.96 * stdev],
        }
        for plan, (mean, stdev) in zip(plans, estimates)
    ]
    ranked = sorted(actions, key=lambda action: action["ev"], reverse=True)
    assumptions = [
        "从当前决策点计算净筹码收益，弃牌为 0，动作按给定候选集合比较。",
        "在给定底牌后按座位独立抽取继续概率；继续方只补足跟注，随后发牌摊牌。",
        "候选动作应事先核对行动顺序、最小下注和能否重新加注。",
        "未建模后续加注、后续街下注、抽水、奖金结构和保险。平手按比例分筹码。",
        "区间仅反映抽样误差，条件在给定对手范围和继续模型上。",
    ]
    random_opponents = [seat.name for seat in seats[1:] if seat.live and seat.range is None]
    if random_opponents:
        assumptions.append(f"以下座位采用均匀随机的可用对手底牌：{'、'.join(random_opponents)}。")
    if any(plan.uses_default_response for plan in plans):
        assumptions.append("被动动作中未指定回应的待跟注对手默认补筹码至摊牌。")
    if seats[0].committed is None:
        assumptions.append("当前底池按单一主池结算。")
    if seats[0].wager is None:
        assumptions.append("未提供当前街投入；过牌按无待跟注计算，单挑跟注金额按输入使用。")
    return {
        "method": "exact_river" if exact else "monte_carlo",
        "samples": count,
        "seed": None if exact else seed,
        "assumptions": assumptions,
        "actions": actions,
        "recommendation": ranked[0].copy(),
        "ev_gap": ranked[0]["ev"] - ranked[1]["ev"] if len(ranked) > 1 else None,
        "intervals_overlap": None if len(ranked) == 1 else ranked[1]["ci95"][1] >= ranked[0]["ci95"][0],
    }
