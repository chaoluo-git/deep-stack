import assert from "node:assert/strict";
import {test} from "node:test";
import {CaptureController, install} from "../background.mjs";
import {frameMetadata, isWpkUrl, validatePairing} from "../protocol.mjs";

const TAB_ID = 17;
const source = {tabId: TAB_ID};
const received = "Network.webSocketFrameReceived";
const sent = "Network.webSocketFrameSent";
const pairing = {version: 1, url: "http://127.0.0.1:8899/v1/frames", token: "X".repeat(43)};
const wpk = "https://h5.wpk.com/table?private=not-for-export";
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u;

function event(timestamp, opcode, payloadData, requestId = "private-protocol-request-id") {
  return {requestId, timestamp, response: {opcode, payloadData, mask: true}};
}

function controller() {
  const calls = [];
  const requests = [];
  const api = {
    tabs: {get: async id => ({id, active: true, url: wpk})},
    debugger: {
      getTargets: async () => [{tabId: TAB_ID, attached: false}],
      attach: async (...args) => { calls.push(["attach", ...args]); },
      sendCommand: async (...args) => { calls.push(["command", ...args]); },
      detach: async (...args) => { calls.push(["detach", ...args]); },
    },
  };
  const capture = new CaptureController(api, async (url, options) => {
    requests.push({url, options});
    return new Response(null, {status: 204});
  });
  return {capture, api, calls, requests};
}

function navigate(tabUrl, frameId = 0) {
  return {tabId: TAB_ID, frameId, url: tabUrl};
}

test("accepts only the target HTTPS origin and canonical loopback pairing", () => {
  assert.equal(isWpkUrl(wpk), true);
  for (const url of ["http://h5.wpk.com/table", "https://wpk.com", "https://h5.wpk.com.fake.example",
    "https://u@h5.wpk.com/", "https://h5.wpk.com:444/", "blob:https://h5.wpk.com/a", "about:blank"]) {
    assert.equal(isWpkUrl(url), false, url);
  }
  assert.deepEqual(validatePairing(pairing), {url: pairing.url, token: pairing.token});
  for (const url of ["https://127.0.0.1:8899/v1/frames", "http://localhost:8899/v1/frames",
    "http://127.1:8899/v1/frames", "http://127.0.0.1:8899/v1/frames?secret=1",
    "http://127.0.0.1:65536/v1/frames", "http://127.0.0.1:0/v1/frames", "http://127.0.0.1:8899/v1/frames\n",
    "http://127.0.0.1:8899@third.party/v1/frames"]) {
    assert.equal(validatePairing({...pairing, url}), null, url);
  }
  assert.equal(validatePairing({...pairing, token: "A".repeat(45) + "\n"}), null);
  assert.equal(validatePairing({...pairing, version: 2}), null);
});

test("recognizes only byte size and encoding, including UTF-8 and gzip without decompression", () => {
  assert.equal(frameMetadata(received, event(1, 1, "牌😀")).size_bytes, 7);
  const json = frameMetadata(received, event(1, 1, ' {"s":"private"}'));
  assert.equal(json.encoding, "json");
  assert.equal(Object.hasOwn(json, "payloadData"), false);
  assert.equal(frameMetadata(received, event(1, 1, "[x]")).encoding, "utf8");
  assert.equal(frameMetadata(received, event(1, 2, "AQIDBA==")).size_bytes, 4);
  const gzipHeader = Buffer.from([31, 139, 8, 0, 0, 0, 0, 0, 0, 255]);
  assert.equal(frameMetadata(received, event(1, 2, gzipHeader.toString("base64"))).encoding, "gzip");
  assert.equal(frameMetadata(received, event(1, 9, "")).encoding, "control");
  assert.equal(frameMetadata(received, event(1, 2, "a===")), null);
});

test("attachment is user-initiated, fail-closed for non-WPK or DevTools contention", async () => {
  const {capture, api, calls} = controller();
  assert.deepEqual(calls, []);
  api.tabs.get = async () => ({id: TAB_ID, active: true, url: wpk, pendingUrl: "https://other.example/"});
  assert.equal((await capture.start(TAB_ID, pairing)).phase, "idle");
  api.tabs.get = async () => ({id: TAB_ID, active: true, url: wpk});
  api.debugger.getTargets = async () => [{tabId: TAB_ID, attached: true}];
  const occupied = await capture.start(TAB_ID, pairing);
  assert.equal(occupied.phase, "idle");
  assert.match(occupied.message, /开发者工具/u);
  assert.deepEqual(calls, []);
});

test("delivers the strict wire schema, ignores unrelated networks, and detaches on Stop", async () => {
  const {capture, requests, calls} = controller();
  assert.equal((await capture.start(TAB_ID, pairing)).phase, "capturing");
  const ignored = {
    get requestId() { assert.fail("unrelated event was read"); },
    get cookies() { assert.fail("cookie info was read"); },
  };
  capture.onDebugEvent({tabId: 99}, received, ignored);
  capture.onDebugEvent({tabId: TAB_ID, sessionId: "other-target"}, received, ignored);
  capture.onDebugEvent(source, "Network.requestWillBeSentExtraInfo", ignored);
  capture.onDebugEvent(source, received, event(1, 1, '{"secret":"first"}'));
  capture.onDebugEvent(source, sent, event(1.012, 2, "YWJjZA=="));
  capture.onDebugEvent(source, received, event(1.021, 9, "YQ==", "second-private-id"));
  capture.onDebugEvent(source, received, event(1.023, 99, "drop"));
  const result = await capture.stop();
  assert.equal(result.sent, 3);
  assert.equal(result.dropped, 1);
  assert.deepEqual(calls, [["attach", source, "1.3"], ["command", source, "Network.enable", {}], ["detach", source]]);
  assert.equal(requests.length, 1);
  const request = requests[0];
  assert.equal(request.url, pairing.url);
  const {headers, method, redirect, credentials, body} = request.options;
  assert.equal(headers.Authorization, `Bearer ${pairing.token}`);
  assert.equal(headers["Content-Type"], "application/json");
  assert.equal(method, "POST");
  assert.equal(redirect, "error");
  assert.equal(credentials, "omit");
  assert.doesNotMatch(body, /secret|private|YWJjZA|not-for-export/u);
  const data = JSON.parse(body);
  assert.match(data.capture_id, uuid);
  assert.match(data.batch_id, uuid);
  assert.deepEqual(data, {
    capture_id: data.capture_id, batch_id: data.batch_id, frames: [
      {connection: "ws-1", offset_ms: 0, direction: "received", opcode: 1, size_bytes: 18, encoding: "json"},
      {connection: "ws-1", offset_ms: 12, direction: "sent", opcode: 2, size_bytes: 4, encoding: "binary"},
      {connection: "ws-2", offset_ms: 21, direction: "received", opcode: 9, size_bytes: 1, encoding: "control"},
    ],
  });
});

test("continues same-origin navigation, stops on main-frame departure, and does not auto-attach", async () => {
  const {capture, calls, requests} = controller();
  await capture.start(TAB_ID, pairing);
  capture.onNavigation(navigate("https://irrelevant.example", 1), "before");
  capture.onNavigation(navigate(wpk), "before");
  capture.onDebugEvent(source, received, event(1, 1, "blocked-during-navigation"));
  capture.onNavigation(navigate(wpk), "committed");
  capture.onDebugEvent(source, received, event(2, 1, "queued"));
  capture.onNavigation(navigate("https://other.example/"), "before");
  capture.onDebugEvent(source, received, event(3, 1, "blocked-after-navigation"));
  await capture.control;
  assert.equal(capture.status().phase, "idle");
  assert.equal(capture.status().dropped, 1);
  assert.equal(requests.length, 0);
  assert.equal(calls.filter(([call]) => call === "attach").length, 1);
  assert.deepEqual(calls.at(-1), ["detach", source]);
});

test("invokes the default native fetch with the worker scope as its receiver", async () => {
  const originalFetch = globalThis.fetch;
  const {api} = controller();
  globalThis.fetch = async function () {
    if (this !== globalThis) throw new TypeError("Illegal invocation");
    return new Response(null, {status: 204});
  };
  try {
    const capture = new CaptureController(api);
    await capture.start(TAB_ID, pairing);
    capture.onDebugEvent(source, received, event(1, 1, "one"));
    assert.equal((await capture.stop()).sent, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("retries failed acknowledgments with the identical body and batch ID", async () => {
  const {capture, requests} = controller();
  capture.fetch = async (url, options) => {
    requests.push({url, options});
    return new Response(null, {status: requests.length === 1 ? 503 : 204});
  };
  await capture.start(TAB_ID, pairing);
  capture.onDebugEvent(source, received, event(1, 1, "once"));
  const result = await capture.stop();
  assert.equal(result.sent, 1);
  assert.equal(requests.length, 2);
  assert.equal(requests[0].options.body, requests[1].options.body);
});

test("when Chrome ends debugging, does not detach a replacement tool or auto-attach", async () => {
  const {capture, calls} = controller();
  await capture.start(TAB_ID, pairing);
  capture.onDetach(source, "canceled_by_user");
  await capture.control;
  assert.match(capture.status().message, /Chrome 已断开/u);
  assert.deepEqual(calls.map(([call]) => call), ["attach", "command"]);
});

test("rejects injected start messages from regular tabs", () => {
  const {api, calls} = controller();
  const stub = {addListener() {}};
  api.debugger.onEvent = api.debugger.onDetach = api.tabs.onRemoved = api.tabs.onReplaced = stub;
  api.webNavigation = {onBeforeNavigate: stub, onCommitted: stub, onHistoryStateUpdated: stub, onErrorOccurred: stub};
  let message;
  api.runtime = {id: "extension", getURL: page => `chrome-extension://extension/${page}`,
    onMessage: {addListener(listener) { message = listener; }}};
  install(api);
  assert.equal(message({action: "start", tabId: TAB_ID, pairing}, {id: "extension", url: wpk}, () => assert.fail()), false);
  assert.deepEqual(calls, []);
});
