import {frameMetadata, isFrameEvent, isWpkUrl, validatePairing} from "./protocol.mjs";

const BATCH_SIZE = 64;
const BUFFER_SIZE = 1024;
const CONNECTION_LIMIT = 256;
const FLUSH_INTERVAL_MS = 250;
const FETCH_TIMEOUT_MS = 2500;
const STOP_TIMEOUT_MS = 1600;
const RETRY_MS = [0, 350, 700, 1400];
const INACTIVE = "请选择本机配对文件，打开 WPK 标签页并点击开始。";

function delay(ms, signal) {
  if (!ms || signal.aborted) return Promise.resolve();
  return new Promise(resolve => {
    const done = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    signal.addEventListener("abort", done, {once: true});
  });
}

function canObserve(tab) {
  return isWpkUrl(tab?.url) && (!tab.pendingUrl || isWpkUrl(tab.pendingUrl));
}

export class CaptureController {
  constructor(api, fetcher = globalThis.fetch.bind(globalThis)) {
    this.api = api;
    this.fetch = fetcher;
    this.run = null;
    this.last = {phase: "idle", message: INACTIVE, sent: 0, dropped: 0};
    this.control = Promise.resolve();
  }

  enqueue(operation) {
    const result = this.control.then(operation);
    this.control = result.catch(() => {});
    return result;
  }

  status() {
    const run = this.run;
    if (!run) return this.last;
    const {phase, message, sent, dropped} = run;
    return {phase, message, sent, dropped};
  }

  async start(tabId, pairing) {
    if (this.run) return this.status();
    const endpoint = validatePairing(pairing);
    if (!endpoint || !Number.isInteger(tabId) || tabId < 0) {
      return {...this.last, message: "请在 WPK 标签页使用有效的本机配对文件。"};
    }
    const run = {
      target: {tabId}, endpoint, id: crypto.randomUUID(), shutdown: new AbortController(),
      phase: "starting", message: "正在连接 WPK 标签页。", attached: false, detached: false, navigation: false,
      queue: [], connections: new Map(), nextConnection: 0, startTime: null,
      sent: 0, dropped: 0, timer: null, inflight: null, failure: null, stopReason: null,
    };
    this.run = run;
    try {
      const tab = await this.api.tabs.get(tabId);
      if (!tab.active || !canObserve(tab) || run.stopReason) {
        return await this.stop(run.stopReason || "请切换到 https://h5.wpk.com 标签页后重试。", false);
      }
      const targets = await this.api.debugger.getTargets();
      if (run.stopReason) return await this.stop(run.stopReason, false);
      if (targets.some(target => target.tabId === tabId && target.attached)) {
        return await this.stop("当前标签页已连接开发者工具或其他调试器。请先关闭后再点击开始。", false);
      }
      try {
        await this.api.debugger.attach(run.target, "1.3");
        run.attached = !run.detached;
      } catch (error) {
        const occupied = /already attached|another debugger/iu.test(error?.message ?? "");
        return await this.stop(occupied ? "当前标签页已有调试器连接。请关闭开发者工具后重试。" :
          "Chrome 无法连接当前标签页。请核对调试权限和浏览器策略。", false);
      }
      if (run.stopReason || !canObserve(await this.api.tabs.get(tabId))) {
        return await this.stop(run.stopReason || "标签页已离开 WPK，采集停止。", false);
      }
      await this.api.debugger.sendCommand(run.target, "Network.enable", {});
      if (run.stopReason) return await this.stop(run.stopReason, false);
      run.phase = "capturing";
      run.message = "正在等待 WPK 的 WebSocket 帧。";
      return this.status();
    } catch {
      return await this.stop(run.stopReason || "无法启用 WPK 网络采集。请确认标签页仍然打开后重试。", false);
    }
  }

  stopSoon(run, message) {
    if (this.run !== run || run.stopReason) return;
    run.stopReason = message;
    run.shutdown.abort();
    void this.enqueue(() => this.run === run ? this.stop(message, false) : this.status());
  }

  async stop(message = "采集已停止。", drain = true) {
    const run = this.run;
    if (!run) return this.last;
    run.phase = "stopping";
    run.message = "正在停止采集。";
    clearTimeout(run.timer);
    if (!drain) run.shutdown.abort();
    const timer = setTimeout(() => run.shutdown.abort(), STOP_TIMEOUT_MS);
    const detach = async () => {
      if (!run.attached) return;
      try {
        await this.api.debugger.detach(run.target);
      } catch {
        if (run.attached) return "Chrome 未确认断开调试器，请关闭对应标签页。";
      } finally {
        run.attached = false;
      }
    };
    const finish = async () => {
      if (run.inflight) await run.inflight;
      while (drain && !run.shutdown.signal.aborted && !run.failure && run.queue.length) {
        await this.deliver(run);
      }
    };
    const [detachMessage] = await Promise.all([detach(), finish()]);
    clearTimeout(timer);
    run.shutdown.abort();
    this.run = null;
    this.last = {
      phase: "idle", message: [message, detachMessage, drain && run.failure].filter(Boolean).join(" "),
      sent: run.sent, dropped: run.dropped + run.queue.length,
    };
    return this.last;
  }

  onNavigation(details, phase) {
    const run = this.run;
    if (!run || details.tabId !== run.target.tabId || details.frameId !== 0 || run.phase === "stopping") return;
    if (!isWpkUrl(details.url)) {
      this.stopSoon(run, "标签页已离开 WPK，采集停止。");
    } else if (phase === "error") {
      this.stopSoon(run, "WPK 标签页导航失败，采集已停止。请在页面恢复后重新开始。");
    } else if (phase === "before") {
      run.navigation = true;
    } else if (phase === "committed") {
      run.navigation = false;
      run.connections.clear();
    }
  }

  onDetach(source, reason) {
    const run = this.run;
    if (!run || source.tabId !== run.target.tabId || source.sessionId) return;
    run.detached = true;
    run.attached = false;
    if (run.phase === "stopping") return;
    this.stopSoon(run, reason === "target_closed" ? "WPK 标签页已关闭，采集停止。" :
      "Chrome 已断开调试器。若打开了开发者工具，请关闭后重新开始。");
  }

  onTabRemoved(tabId) {
    if (this.run?.target.tabId === tabId) this.stopSoon(this.run, "WPK 标签页已关闭或被替换，采集停止。");
  }

  onDebugEvent(source, method, params) {
    const run = this.run;
    if (!run || run.phase !== "capturing" || run.stopReason || run.navigation ||
        source.tabId !== run.target.tabId || source.sessionId) return;
    if (method === "Network.webSocketClosed") {
      run.connections.delete(params?.requestId);
      return;
    }
    if (!isFrameEvent(method)) return;
    if (run.queue.length >= BUFFER_SIZE) {
      run.dropped += 1;
      return;
    }
    const meta = frameMetadata(method, params);
    if (!meta) {
      run.dropped += 1;
      return;
    }
    const {requestId, timestamp, ...properties} = meta;
    run.startTime ??= timestamp;
    const offset_ms = Math.max(0, Math.round((timestamp - run.startTime) * 1000));
    if (!Number.isSafeInteger(offset_ms)) {
      run.dropped += 1;
      return;
    }
    let connection = run.connections.get(requestId);
    if (!connection) {
      if (run.connections.size >= CONNECTION_LIMIT || run.nextConnection >= Number.MAX_SAFE_INTEGER) {
        run.dropped += 1;
        return;
      }
      connection = `ws-${++run.nextConnection}`;
      run.connections.set(requestId, connection);
    }
    run.queue.push({connection, offset_ms, ...properties});
    this.schedule(run);
  }

  schedule(run) {
    if (run.phase !== "capturing" || run.stopReason || run.inflight || run.timer !== null || !run.queue.length) return;
    run.timer = setTimeout(() => {
      run.timer = null;
      if (run.phase !== "capturing" || run.stopReason) return;
      void this.deliver(run).then(() => {
        if (run.failure) this.stopSoon(run, run.failure);
        else this.schedule(run);
      });
    }, run.queue.length >= BATCH_SIZE ? 0 : FLUSH_INTERVAL_MS);
  }

  deliver(run) {
    if (run.inflight) return run.inflight;
    const frames = run.queue.splice(0, BATCH_SIZE);
    const batch = {capture_id: run.id, batch_id: crypto.randomUUID(), frames};
    run.inflight = this.send(run, batch).then(failure => {
      run.inflight = null;
      if (failure) {
        run.dropped += frames.length;
        if (!run.shutdown.signal.aborted) run.failure = failure;
      } else {
        run.sent += frames.length;
        if (run.phase === "capturing") run.message = "正在实时向本机发送帧元数据。";
      }
    });
    return run.inflight;
  }

  async send(run, batch) {
    const body = JSON.stringify(batch);
    for (const backoff of RETRY_MS) {
      await delay(backoff, run.shutdown.signal);
      if (run.shutdown.signal.aborted) break;
      const abort = new AbortController();
      const cancel = () => abort.abort();
      run.shutdown.signal.addEventListener("abort", cancel, {once: true});
      const timer = setTimeout(cancel, FETCH_TIMEOUT_MS);
      try {
        const response = await this.fetch(run.endpoint.url, {
          method: "POST", body,
          headers: {"Authorization": `Bearer ${run.endpoint.token}`, "Content-Type": "application/json"},
          redirect: "error", credentials: "omit", referrerPolicy: "no-referrer", cache: "no-store",
          signal: abort.signal,
        });
        void response.body?.cancel().catch(() => {});
        if (response.ok) return null;
        if ([401, 403].includes(response.status)) return "本机配对已失效，请选择接收程序当前生成的配对文件。";
        if (response.status < 500 && ![408, 429].includes(response.status)) {
          return "本机程序拒绝了帧元数据，请核对接收程序版本。";
        }
      } catch {
        // Retry the same batch ID so a lost acknowledgment cannot duplicate it.
      } finally {
        clearTimeout(timer);
        run.shutdown.signal.removeEventListener("abort", cancel);
      }
      if (!run.shutdown.signal.aborted && run.phase === "capturing") run.message = "本机接收暂未响应，正在重试。";
    }
    return "无法向本机接收程序送达帧元数据，请确认程序仍在运行后重新开始。";
  }
}

export function install(api) {
  const controller = new CaptureController(api);
  api.debugger.onEvent.addListener((...args) => controller.onDebugEvent(...args));
  api.debugger.onDetach.addListener((...args) => controller.onDetach(...args));
  api.tabs.onRemoved.addListener(tabId => controller.onTabRemoved(tabId));
  api.tabs.onReplaced.addListener((_added, removed) => controller.onTabRemoved(removed));
  api.webNavigation.onBeforeNavigate.addListener(details => controller.onNavigation(details, "before"));
  api.webNavigation.onCommitted.addListener(details => controller.onNavigation(details, "committed"));
  api.webNavigation.onHistoryStateUpdated.addListener(details => controller.onNavigation(details, "history"));
  api.webNavigation.onErrorOccurred.addListener(details => controller.onNavigation(details, "error"));
  api.runtime.onMessage.addListener((message, sender, respond) => {
    const [page, fragment, extra] = typeof sender.url === "string" ? sender.url.split("#") : [];
    if (sender.id !== api.runtime.id || page !== api.runtime.getURL("popup.html") || extra !== undefined ||
        (fragment !== undefined && !/^tab=[0-9]+$/u.test(fragment))) return false;
    if (message?.action === "start" && fragment !== undefined && fragment !== `tab=${message.tabId}`) return false;
    if (message?.action === "status") {
      respond(controller.status());
      return false;
    }
    if (message?.action !== "start" && message?.action !== "stop") return false;
    const operation = message.action === "start" ? () => controller.start(message.tabId, message.pairing) : () => controller.stop();
    void controller.enqueue(operation).then(respond, () => respond({
      phase: "idle", message: "无法完成操作，请关闭弹窗后重试。", sent: 0, dropped: 0,
    }));
    return true;
  });
}

if (typeof chrome !== "undefined" && chrome.debugger) install(chrome);
