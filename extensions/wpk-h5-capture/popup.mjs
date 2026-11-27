import {MAX_PAIRING_BYTES, isWpkUrl, validatePairing} from "./protocol.mjs";

const field = document.getElementById("pairing-field");
const fileInput = document.getElementById("pairing-file");
const form = document.getElementById("capture-form");
const start = document.getElementById("start-capture");
const stop = document.getElementById("stop-capture");
const statusLine = document.getElementById("capture-status");
const stats = document.getElementById("capture-stats");
const pairingLine = document.getElementById("pairing-status");
const pairingWindow = document.getElementById("open-pairing-window");
const windowTab = /^#tab=([0-9]+)$/u.exec(location.hash);
const requestedTabId = windowTab ? Number(windowTab[1]) : undefined;
let pairing;
let busy = false;
let phase = "loading";
let selectedTabId;
let fileRevision = 0;
let refreshing = false;

function refreshControls() {
  field.hidden = phase !== "idle";
  start.disabled = busy || phase !== "idle" || !pairing || selectedTabId === undefined;
  stop.disabled = busy || phase !== "capturing";
  pairingWindow.hidden = requestedTabId !== undefined || selectedTabId === undefined || phase !== "idle";
}

function display(next) {
  phase = next.phase;
  const tabNotice = phase === "idle" && selectedTabId === undefined ? " 请切换到 WPK 网页打开扩展。" : "";
  statusLine.textContent = next.message + tabNotice;
  stats.hidden = phase !== "capturing" && !next.sent && !next.dropped;
  const unit = next.mode === "public" ? " 个快照" : " 帧";
  stats.textContent = `已确认 ${next.sent}${unit} · 未确认或丢弃 ${next.dropped}${unit}`;
  refreshControls();
}

async function refresh() {
  if (busy || refreshing) return;
  refreshing = true;
  try {
    const tab = requestedTabId === undefined ? (await chrome.tabs.query({active: true, currentWindow: true}))[0] :
      await chrome.tabs.get(requestedTabId).catch(() => undefined);
    selectedTabId = Number.isInteger(tab?.id) && tab.active && isWpkUrl(tab.url) && (!tab.pendingUrl || isWpkUrl(tab.pendingUrl)) ? tab.id : undefined;
    const status = await chrome.runtime.sendMessage({action: "status"});
    if (!busy) display(status);
  } catch {
    if (!busy) display({phase: "idle", message: "无法读取采集状态，请重新打开扩展。", sent: 0, dropped: 0});
  } finally {
    refreshing = false;
  }
}

fileInput.addEventListener("change", async () => {
  const revision = ++fileRevision;
  const file = fileInput.files?.[0];
  fileInput.value = "";
  pairing = undefined;
  pairingLine.textContent = "";
  refreshControls();
  if (!file) return;
  try {
    if (file.size < 1 || file.size > MAX_PAIRING_BYTES) throw new Error();
    const candidate = JSON.parse(await file.text());
    if (revision !== fileRevision) return;
    const endpoint = validatePairing(candidate);
    if (!endpoint) throw new Error();
    pairing = candidate;
    pairingLine.textContent = endpoint.mode === "public" ? "配对成功，准备采集公开牌桌快照。" : "配对成功，准备采集帧元数据。";
  } catch {
    if (revision === fileRevision) pairingLine.textContent = "配对文件无效，请使用本机接收程序生成的 JSON 文件。";
  }
  refreshControls();
});

async function changeCapture(message) {
  if (busy) return;
  busy = true;
  refreshControls();
  try {
    const next = await chrome.runtime.sendMessage(message);
    if (message.action === "start" && next.phase === "capturing") {
      pairing = undefined;
      pairingLine.textContent = "";
    }
    display(next);
  } catch {
    display({phase: "idle", message: "连接采集组件失败，请重新打开扩展。", sent: 0, dropped: 0});
  } finally {
    busy = false;
    refreshControls();
  }
}

pairingWindow.addEventListener("click", async () => {
  if (selectedTabId === undefined || pairingWindow.hidden) return;
  try {
    await chrome.windows.create({
      url: `${chrome.runtime.getURL("popup.html")}#tab=${selectedTabId}`, type: "popup", width: 400, height: 285,
    });
  } catch {
    pairingLine.textContent = "无法打开独立配对窗口。";
  }
});

form.addEventListener("submit", event => {
  event.preventDefault();
  if (!start.disabled) void changeCapture({action: "start", tabId: selectedTabId, pairing});
});

stop.addEventListener("click", () => {
  if (!stop.disabled) void changeCapture({action: "stop"});
});

void refresh();
setInterval(() => { void refresh(); }, 500);
