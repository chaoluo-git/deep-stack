export function readPublicState(root) {
  const scenes = new Set(["gameTable", "gameTableL", "gamePadTable"]);
  const streets = new Map([
    ["PRE_FLOP", "preflop"], ["FLOP", "flop"], ["TURN", "turn"],
    ["RIVER", "river"], ["THAN", "settled"],
  ]);
  const actions = new Map([
    ["fold", "fold"], ["弃牌", "fold"], ["棄牌", "fold"], ["蓋牌", "fold"],
    ["check", "check"], ["让牌", "check"], ["讓牌", "check"],
    ["过牌", "check"], ["過牌", "check"], ["call", "call"], ["跟注", "call"],
    ["bet", "bet"], ["下注", "bet"], ["raise", "raise"], ["加注", "raise"],
    ["all in", "all_in"], ["allin", "all_in"], ["all-in", "all_in"],
  ]);

  function visibleNode(node) {
    for (let depth = 0; node && depth < 64; depth += 1, node = node.parent) {
      if (node.activeInHierarchy !== true || node.opacity <= 0) return false;
      if (!node.parent) return true;
    }
    return false;
  }

  function visible(component) {
    return component?.enabled !== false && visibleNode(component?.node);
  }

  function label(component) {
    return visible(component) && typeof component.string === "string"
      ? component.string.trim()
      : "";
  }

  function decimalReference(value) {
    if (typeof value === "number" && !Number.isSafeInteger(value)) return null;
    if (typeof value !== "number" && typeof value !== "string") return null;
    const text = String(value).trim();
    return /^[0-9]{1,24}$/.test(text) && /[1-9]/.test(text) ? text : null;
  }

  function amount(value, displayedLabel, isWager = false) {
    const text = label(displayedLabel);
    if (!text) return null;
    const numeric = /^(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?$/;
    if (numeric.test(text)) {
      const shown = Number(text.replaceAll(",", ""));
      return shown <= 1e12 ? shown : null;
    }
    const raw = typeof value === "string" && numeric.test(value) ? Number(value.replaceAll(",", "")) : value;
    if (!Number.isFinite(raw) || raw < 0 || raw > 1e12) return null;
    try {
      // K, BB and ante displays use the official unit formatter. A matching
      // formatted value ties this amount to the actual visible label.
      const formatted = root.HMFUtils?.convertScoreUnit(raw, true, false, isWager);
      return typeof formatted === "string" && formatted.trim() === text ? raw : null;
    } catch {
      return null;
    }
  }

  function cardName(code) {
    if (!Number.isInteger(code)) return null;
    const suit = [null, "s", "h", "c", "d"][Math.floor(code / 100)];
    const rank = "A23456789TJQK"[(code % 100) - 1];
    return suit && rank ? rank + suit : null;
  }

  function shownAction(info) {
    const candidates = new Set();
    // The action label can remain active after its separate panel is hidden.
    if (visibleNode(info.ndStateBg) && !visible(info.lblNickname)) {
      const action = actions.get(label(info.lblActionState).toLowerCase());
      if (action && action !== "fold" && action !== "all_in") candidates.add(action);
    }
    if (info.isFold === true && actions.get(label(info.lblFold).toLowerCase()) === "fold") {
      candidates.add("fold");
    }
    // The all-in renderer hides the text banner and uses dedicated animations.
    if (visibleNode(info.ndAllin) && (visibleNode(info.allinAction0) || visibleNode(info.allinAction1))) {
      candidates.add("all_in");
    }
    return candidates.size === 1 ? candidates.values().next().value : null;
  }

  function liveHand() {
    try {
      // The active gameUI already loads this model. Its currentBoutNum is also
      // the hand number used by the official review list.
      const model = root.__require("WPGameData").WPGameData.Instance;
      const seat = model.currentUserSeatNum;
      const local = root.WPUserModel?.seatNum;
      const localSeat = typeof local === "string" && /^-?(?:0|[1-9][0-9]*)$/.test(local) ? Number(local) : local;
      return {
        hand: decimalReference(model.currentBoutNum),
        spectating: (seat === 0 || seat === "0") && Number.isSafeInteger(localSeat) && localSeat <= 0,
      };
    } catch {
      return null;
    }
  }

  function liveTableReference() {
    try {
      // Both client models use the session ID shown on the official review.
      const current = decimalReference(root.CurrentUserInfo?.roomId);
      const portal = decimalReference(root.__require("WPGamePortal").WPGamePortal.Instance.roomId);
      return current === portal ? current : null;
    } catch {
      return null;
    }
  }

  function publicBoard(component, street) {
    const result = [];
    if (!visible(component)) return result;
    const limit = { preflop: 0, flop: 3, turn: 4 }[street] ?? 5;

    for (let slot = 0; slot < limit; slot += 1) {
      const sprite = component.spCardList?.[slot];
      // The official renderer marks separately revealed cards with these overlays.
      if (!visible(sprite) || component.spSeeCardBordList?.[slot]?.activeInHierarchy) break;
      const code = component._spriteCards?.[slot];
      const card = cardName(code);
      if (!card || String(sprite.spriteFrame?.name) !== String(code) || result.includes(card)) break;
      result.push(card);
    }
    return result;
  }

  try {
    if (root?.location?.origin !== "https://h5.wpk.com" || root.top !== root ||
        root.document?.visibilityState !== "visible") return null;
    const scene = root.cc?.director?.getScene?.();
    if (!scenes.has(scene?.name)) return null;
    const active = (name) => scene.getComponentsInChildren(name).filter(visible);
    const [table, secondTable] = active("gameUI");
    if (!table || secondTable) return null;
    if (active("historyReviewLayer").length || active("GameReplay").length ||
        active("GameReviewFavorReplay").length) return null;
    const hand = liveHand();
    if (!hand?.spectating) return null;

    const controllers = active("gameContr").filter((component) => component._gameUI === table);
    const street = controllers.length === 1 ? streets.get(controllers[0]._roundData?.round) ?? null : null;
    const seats = [];
    const usedSlots = new Set();
    const playerClaims = new Map();

    for (const seat of active("seat")) {
      const info = seat.useInfoJs;
      if (seat._gameUIJs !== table || !visibleNode(seat.ndContentSeat) || !visible(info) ||
          !visibleNode(info.AvatarNode)) continue;
      if (info._isSelf === true) return null;

      // The seat and avatar bind the same player. Opening that avatar's public
      // userInfoLayer displays the same userId, and seatNum identifies table roles.
      const seatId = decimalReference(seat._data?.userId);
      const avatarId = decimalReference(info._data?.userId);
      for (const id of new Set([seatId, avatarId])) {
        if (id === null) continue;
        if (!playerClaims.has(id)) playerClaims.set(id, new Set());
        playerClaims.get(id).add(seat);
      }
      const slot = Number(decimalReference(seat._data?.seatNum));
      if (slot < 1 || slot > 9 || Number(decimalReference(info._data?.seatNum)) !== slot) continue;
      if (usedSlots.has(slot)) return null;
      usedSlots.add(slot);
      const playerId = seatId === avatarId ? seatId : null;
      seats.push({
        slot,
        player_id: playerId,
        action: shownAction(info),
        wager: visibleNode(info.ndBetBg) && !visible(info.spBetAni)
          ? amount(info.currentBet, info.lblBet, true) : null,
        stack: amount(info.currentScore, info.lblScore),
      });
    }

    for (const seat of seats) {
      if (playerClaims.get(seat.player_id)?.size > 1) seat.player_id = null;
    }

    return {
      table_ref: liveTableReference(),
      hand_ref: hand?.hand ?? null,
      street,
      board: publicBoard(table.comCards?.getComponent("comCards"), street),
      seats: seats.sort((a, b) => a.slot - b.slot),
    };
  } catch {
    // Unsupported or changing presentation components should produce no sample.
    return null;
  }
}

export const PUBLIC_STATE_EXPRESSION = `(${readPublicState.toString()})(globalThis)`;
