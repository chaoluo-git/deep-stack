import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";

import { PUBLIC_STATE_EXPRESSION, readPublicState } from "../public-state.mjs";

const node = (properties = {}) => ({ activeInHierarchy: true, opacity: 255, ...properties });
const label = (string, properties) => ({ node: node(properties), enabled: true, string });

function tableFixture() {
  const board = {
    node: node(),
    _spriteCards: [313, 203, 411, 110, 213],
    spCardList: [313, 203, 411, 110, 213].map((name) => ({ node: node(), spriteFrame: { name: String(name) } })),
    spSeeCardBordList: Array.from({ length: 5 }, () => node({ activeInHierarchy: false })),
  };
  const room = { lblBetStep: label("2/4(2)") };
  const table = {
    node: node(),
    comCards: { getComponent: () => board },
    roomInfo: { getComponent: (name) => name === "roomInfo" ? room : null },
  };
  const controller = { node: node(), _gameUI: table, _roundData: { round: "RIVER" } };
  const info = {
    node: node(),
    AvatarNode: node(),
    _data: { seatNum: 3, userId: "9007199254740999" },
    _isSelf: false,
    lblActionState: label("跟注"),
    ndStateBg: node(),
    lblNickname: label("", { activeInHierarchy: false }),
    ndD: node(),
    ndDAni: node({ activeInHierarchy: false }),
    lblFold: label("弃牌", { opacity: 0 }),
    isFold: false,
    ndAllin: node({ activeInHierarchy: false }),
    allinAction0: node({ activeInHierarchy: false }),
    allinAction1: node({ activeInHierarchy: false }),
    lblBet: label("1.5K"),
    ndBetBg: node(),
    spBetAni: { node: node({ activeInHierarchy: false }) },
    lblScore: label("19.5K"),
    currentBet: 1500,
    currentScore: 19500,
  };
  const seat = {
    node: node(),
    _gameUIJs: table,
    ndContentSeat: node(),
    _data: { seatNum: 3, userId: "9007199254740999" },
    useInfoJs: info,
  };
  const modules = { gameUI: [table], gameContr: [controller], seat: [seat] };
  const scene = { name: "gamePadTable", getComponentsInChildren: (name) => modules[name] || [] };
  const model = { currentBoutNum: 75, currentUserSeatNum: 0 };
  const portal = { roomId: "9007199254740998" };
  const root = {
    location: { origin: "https://h5.wpk.com" },
    document: { visibilityState: "visible" },
    cc: { director: { getScene: () => scene } },
    CurrentUserInfo: { roomId: portal.roomId },
    WPUserModel: { seatNum: -1 },
    HMFUtils: { convertScoreUnit: (amount) => `${amount / 1000}K` },
    __require(name) {
      if (name === "WPGamePortal") return { WPGamePortal: { Instance: portal } };
      assert.equal(name, "WPGameData");
      return { WPGameData: { Instance: model } };
    },
  };
  root.top = root;
  return { root, scene, board, seat, info, controller, modules, model, portal, room, table };
}

test("reads an exact bounded snapshot from the active public scene", () => {
  const { root } = tableFixture();
  const expected = {
    table_ref: "9007199254740998",
    hand_ref: "75",
    street: "river",
    table_details: { small_blind: 2, big_blind: 4, ante: 2, third_blind: null, dealer_slot: 3 },
    board: ["Kc", "3h", "Jd", "Ts", "Kh"],
    seats: [{ slot: 3, player_id: "9007199254740999", action: "call", wager: 1500, stack: 19500 }],
  };
  assert.deepEqual(readPublicState(root), expected);

  const context = vm.createContext({
    location: root.location, document: root.document, cc: root.cc, __require: root.__require,
    CurrentUserInfo: root.CurrentUserInfo, HMFUtils: root.HMFUtils, WPUserModel: root.WPUserModel,
  });
  vm.runInContext("globalThis.top = globalThis", context);
  const evaluated = vm.runInContext(PUBLIC_STATE_EXPRESSION, context);
  assert.deepEqual(JSON.parse(JSON.stringify(evaluated)), expected);
});

test("only emits the visible prefix with matching card frames", () => {
  const { root, board, controller } = tableFixture();
  board.spCardList[2].spriteFrame.name = "309";
  assert.deepEqual(readPublicState(root).board, ["Kc", "3h"]);
  board.spCardList[2].spriteFrame.name = "411";
  board.spSeeCardBordList[3].activeInHierarchy = true;
  Object.defineProperty(board._spriteCards, 3, {
    get() { throw new Error("unshown board state must be ignored"); },
  });
  assert.deepEqual(readPublicState(root).board, ["Kc", "3h", "Jd"]);
  controller._roundData.round = "PRE_FLOP";
  assert.deepEqual(readPublicState(root).board, []);
});

test("omits unconfirmed data and rejects other pages and replay overlays", () => {
  const { root, scene, info, modules, model } = tableFixture();
  info._data.userId = "77";
  info.lblBet.node.activeInHierarchy = false;
  info.lblFold.node.opacity = 255;
  info.isFold = true;
  const [seat] = readPublicState(root).seats;
  assert.deepEqual(seat, { slot: 3, player_id: null, action: null, wager: null, stack: 19500 });

  info._isSelf = true;
  assert.equal(readPublicState(root), null);
  info._isSelf = false;
  model.currentUserSeatNum = 1;
  assert.equal(readPublicState(root), null);
  model.currentUserSeatNum = undefined;
  assert.equal(readPublicState(root), null);
  model.currentUserSeatNum = "0";
  model.currentBoutNum = 0;
  assert.equal(readPublicState(root).hand_ref, null);
  root.WPUserModel.seatNum = 2;
  assert.equal(readPublicState(root), null);
  root.WPUserModel.seatNum = undefined;
  assert.equal(readPublicState(root), null);
  root.WPUserModel.seatNum = "0";
  const requireModel = root.__require;
  root.__require = () => { throw new Error("module unavailable"); };
  assert.equal(readPublicState(root), null);
  root.__require = requireModel;
  modules.historyReviewLayer = [{ node: node() }];
  assert.equal(readPublicState(root), null);
  modules.historyReviewLayer = [];
  root.document.visibilityState = "hidden";
  assert.equal(readPublicState(root), null);
  root.document.visibilityState = "visible";
  scene.name = "hallCommon";
  assert.equal(readPublicState(root), null);
  scene.name = "gameTable";
  root.location.origin = "https://wpk.com";
  assert.equal(readPublicState(root), null);
});

test("requires the visible action widget and a displayed non-animating wager", () => {
  const { root, info } = tableFixture();
  info.lblActionState.string = "加注";
  info.ndStateBg.activeInHierarchy = false;
  info.ndBetBg.activeInHierarchy = false;
  assert.equal(readPublicState(root).seats[0].action, null);
  assert.equal(readPublicState(root).seats[0].wager, null);

  info.ndStateBg.activeInHierarchy = true;
  info.lblNickname.node.activeInHierarchy = true;
  assert.equal(readPublicState(root).seats[0].action, null);
  info.lblNickname.node.activeInHierarchy = false;
  info.ndBetBg.activeInHierarchy = true;
  info.spBetAni.node.activeInHierarchy = true;
  assert.equal(readPublicState(root).seats[0].action, "raise");
  assert.equal(readPublicState(root).seats[0].wager, null);

  info.ndStateBg.activeInHierarchy = false;
  info.lblFold.node.opacity = 255;
  assert.equal(readPublicState(root).seats[0].action, null);
  info.isFold = true;
  assert.equal(readPublicState(root).seats[0].action, "fold");
  info.lblFold.node.opacity = 0;
  info.ndAllin.activeInHierarchy = true;
  info.allinAction1.activeInHierarchy = true;
  assert.equal(readPublicState(root).seats[0].action, "all_in");
});

test("does not bind one player to multiple seats or emit out-of-range amounts", () => {
  const { root, seat, info, modules } = tableFixture();
  const secondData = { ...seat._data, seatNum: 4 };
  modules.seat.push({ ...seat, _data: secondData, useInfoJs: { ...info, _data: secondData } });
  info.currentScore = 1e12 + 1;
  assert.deepEqual(readPublicState(root).seats.map(({ slot, player_id, stack }) => ({ slot, player_id, stack })), [
    { slot: 3, player_id: null, stack: null },
    { slot: 4, player_id: null, stack: 19500 },
  ]);
});

test("an unresolved visible seat claim blocks attribution in a different seat", () => {
  const { root, seat, info, modules } = tableFixture();
  const secondData = { ...seat._data, seatNum: 4 };
  const secondAvatar = { seatNum: 4, userId: "123456" };
  modules.seat.push({ ...seat, _data: secondData, useInfoJs: { ...info, _data: secondAvatar } });
  assert.deepEqual(readPublicState(root).seats.map(({ player_id }) => player_id), [null, null]);

  secondAvatar.seatNum = 5;
  assert.deepEqual(readPublicState(root).seats.map(({ player_id }) => player_id), [null]);
});

test("requires matching precise table references and trusts a plain visible amount", () => {
  const { root, info, portal } = tableFixture();
  info.lblBet.string = "1,450.5";
  assert.equal(readPublicState(root).seats[0].wager, 1450.5);
  info.lblScore.string = "18.1K";
  assert.equal(readPublicState(root).seats[0].stack, null);

  portal.roomId = "9007199254740999";
  assert.equal(readPublicState(root).table_ref, null);
  portal.roomId = root.CurrentUserInfo.roomId = Number("9007199254740998");
  assert.equal(readPublicState(root).table_ref, null);
});

test("reads only a valid visible table-stakes caption and leaves omitted values unknown", () => {
  const { root, room, table } = tableFixture();
  room.lblBetStep.string = "0.25/0.5/1(0.1)";
  assert.deepEqual(readPublicState(root).table_details, {
    small_blind: 0.25, big_blind: 0.5, ante: 0.1, third_blind: 1, dealer_slot: 3,
  });
  room.lblBetStep.string = "2/4";
  assert.deepEqual(readPublicState(root).table_details, {
    small_blind: 2, big_blind: 4, ante: null, third_blind: null, dealer_slot: 3,
  });
  const missing = { small_blind: null, big_blind: null, ante: null, third_blind: null, dealer_slot: 3 };
  for (const malformed of ["", "4/2", "2/4/3", "2/4(0)", "2/1000000000001", "2/4 chips", "2(Dx2)"]) {
    room.lblBetStep.string = malformed;
    assert.deepEqual(readPublicState(root).table_details, missing, malformed);
  }
  room.lblBetStep.string = "2/4(2)";
  room.lblBetStep.node.opacity = 0;
  assert.deepEqual(readPublicState(root).table_details, missing);
  delete table.roomInfo;
  assert.deepEqual(readPublicState(root).table_details, missing);
});

test("requires a single static dealer marker bound to an occupied table seat", () => {
  const { root, info, modules, seat } = tableFixture();
  assert.equal(readPublicState(root).table_details.dealer_slot, 3);
  info.ndDAni.activeInHierarchy = true;
  assert.equal(readPublicState(root).table_details.dealer_slot, null);
  info.ndDAni.activeInHierarchy = false;
  const secondData = { seatNum: 4, userId: "123456" };
  const second = { ...seat, _data: secondData, useInfoJs: { ...info, _data: secondData, ndD: node() } };
  modules.seat.push(second);
  assert.equal(readPublicState(root).table_details.dealer_slot, null);
  second.useInfoJs.ndD.activeInHierarchy = false;
  assert.equal(readPublicState(root).table_details.dealer_slot, 3);
  second.useInfoJs.ndDAni = node();
  assert.equal(readPublicState(root).table_details.dealer_slot, null);
  second.useInfoJs.ndDAni.activeInHierarchy = false;
  info._data = null;
  assert.equal(readPublicState(root).table_details.dealer_slot, null);
});
