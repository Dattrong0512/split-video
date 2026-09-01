if (location.href.includes("/github/Dattrong0512/split-video/blob/main/OmniVoice_API.ipynb") && !globalThis.__DOUYIN_DUBBING_COLAB_AUTOMATION__) {
  globalThis.__DOUYIN_DUBBING_COLAB_AUTOMATION__ = true;

  const READY_PATTERN = /NEKO_SERVER_READY\s+(\{[^\n]+\})/g;
  const PROGRESS_PATTERN = /NEKO_PROGRESS\s+(\{[^\n]+\})/g;
  const ERROR_PATTERN = /(GPU_UNAVAILABLE|TUNNEL_FAILED|API_FAILED|INSTALL_FAILED):?\s*([^\n]*)/g;
  const OUTPUT_SELECTOR = "colab-output, colab-static-output-renderer, colab-stream-output, .output_text, .stream, [role=status]";
  let lastHandshake = "";
  let lastProgress = "";
  let lastError = "";
  let runAllRequested = false;
  let lastRunAllAt = 0;
  let lastNotebookProgressAt = 0;
  let forceRunPending = false;
  let lastControlClickAt = 0;
  let automationTimer = null;
  let cachedRoots = [document];
  let rootsExpireAt = 0;

  function roots() {
    if (Date.now() < rootsExpireAt) return cachedRoots;
    const found = [document];
    for (let index = 0; index < found.length; index += 1) {
      for (const element of found[index].querySelectorAll("*")) {
        if (element.shadowRoot && !found.includes(element.shadowRoot)) found.push(element.shadowRoot);
      }
    }
    cachedRoots = found;
    rootsExpireAt = Date.now() + 10000;
    return cachedRoots;
  }

  function allElements(selector) {
    return roots().flatMap((root) => [...root.querySelectorAll(selector)]);
  }

  function elementLabel(element) {
    return `${element.textContent || ""} ${element.getAttribute?.("aria-label") || ""} ${element.getAttribute?.("title") || ""}`.replace(/\s+/g, " ").trim();
  }

  function findControl(pattern, selectors = "button, [role=button], [role=menuitem], colab-toolbar-button") {
    const eligible = (element) => element.id !== "neko-colab-helper" && pattern.test(elementLabel(element)) && !element.disabled;
    const direct = [...document.querySelectorAll(selectors)].find(eligible);
    if (direct) return direct;
    return allElements(selectors).find(eligible);
  }

  function sendProgress(payload) {
    const serialized = JSON.stringify(payload);
    if (serialized === lastProgress) return;
    lastProgress = serialized;
    chrome.runtime.sendMessage({ type: "COLAB_PROGRESS", payload }).catch(() => {});
    const helper = document.getElementById("neko-colab-helper");
    if (helper && payload.message) helper.textContent = payload.message;
  }

  function pageText() {
    const fragments = [];
    for (const root of roots()) {
      if (root !== document && root.host?.matches?.(OUTPUT_SELECTOR)) fragments.push(root.textContent || "");
      for (const element of root.querySelectorAll(OUTPUT_SELECTOR)) {
        fragments.push(element.textContent || "");
        if (element.shadowRoot) fragments.push(element.shadowRoot.textContent || "");
      }
    }
    return fragments.join("\n");
  }

  function scanNotebookOutput(text) {
    let match;
    let newestProgress = null;
    PROGRESS_PATTERN.lastIndex = 0;
    while ((match = PROGRESS_PATTERN.exec(text))) newestProgress = match[1];
    if (newestProgress) {
      try {
        lastNotebookProgressAt = Date.now();
        sendProgress(JSON.parse(newestProgress));
      } catch (_) {}
    }

    let newestError = null;
    ERROR_PATTERN.lastIndex = 0;
    while ((match = ERROR_PATTERN.exec(text))) newestError = `${match[1]}: ${match[2]}`.trim();
    if (newestError && newestError !== lastError) {
      lastError = newestError;
      sendProgress({ stage: "error", progress: 0, message: newestError, error: true });
    }

    let newestHandshake = null;
    READY_PATTERN.lastIndex = 0;
    while ((match = READY_PATTERN.exec(text))) newestHandshake = match[1];
    if (!newestHandshake || newestHandshake === lastHandshake) return;
    try {
      const payload = JSON.parse(newestHandshake);
      if (/^https:\/\/[a-z0-9-]+\.trycloudflare\.com$/i.test(payload.url || "") && payload.token) {
        lastHandshake = newestHandshake;
        document.getElementById("neko-colab-helper")?.remove();
        chrome.runtime.sendMessage({ type: "COLAB_READY", payload }).then((response) => {
          if (!response?.stale || lastHandshake !== newestHandshake) return;
          lastHandshake = "";
          forceRunPending = true;
          sendProgress({ stage: "restart", progress: 12, message: "Kết nối cũ đã hết hạn. Đang tạo máy chủ mới…" });
          scheduleTick(50);
        }).catch(() => {});
      }
    } catch (_) {}
  }

  function clickControl(control) {
    if (!control || Date.now() - lastControlClickAt < 350) return false;
    lastControlClickAt = Date.now();
    control.click();
    return true;
  }

  function clickConfirmation() {
    return clickControl(findControl(/\brun anyway\b|chạy vẫn|^yes\b|^có\b|connect anyway|kết nối vẫn/i));
  }

  function clickRunAll() {
    const direct = findControl(/\brun all\b|chạy tất cả/i);
    if (direct && clickControl(direct)) {
      runAllRequested = true;
      lastRunAllAt = Date.now();
      return true;
    }
    const runtime = findControl(/(^|\s)runtime(\s|$)|thời gian chạy/i);
    if (!clickControl(runtime)) return false;
    setTimeout(() => {
      const item = findControl(/run all|chạy tất cả/i);
      if (clickControl(item)) {
        runAllRequested = true;
        lastRunAllAt = Date.now();
      }
      setTimeout(clickConfirmation, 700);
    }, 500);
    return true;
  }

  function automateColab(text) {
    if (lastHandshake) return;
    if (/allocating|connecting|initializing|đang kết nối|đang phân bổ/i.test(text)) {
      sendProgress({ stage: "connecting", progress: 10, message: "Đang kết nối Colab và yêu cầu GPU T4…" });
      return;
    }
    if (clickConfirmation()) {
      sendProgress({ stage: "confirming", progress: 12, message: "Đang xác nhận phiên Colab…" });
      return;
    }
    const connect = findControl(/(^|\s)(connect|reconnect|kết nối|kết nối lại)(\s|$)/i);
    if (connect && clickControl(connect)) {
      sendProgress({ stage: "connecting", progress: 8, message: "Đang tự kết nối Colab và yêu cầu GPU T4…" });
      return;
    }
    if (forceRunPending) {
      if (clickRunAll()) {
        forceRunPending = false;
        sendProgress({ stage: "run-all", progress: 15, message: "Đã gửi lệnh Run all · lần đầu có thể mất 3–10 phút…" });
      }
      return;
    }
    if (runAllRequested && lastNotebookProgressAt >= lastRunAllAt - 2000) {
      return;
    }
    if (runAllRequested && Date.now() - lastRunAllAt < 30000) {
      sendProgress({ stage: "executing", progress: 18, message: "Colab đang chạy notebook · lần đầu có thể mất 3–10 phút…" });
      return;
    }
    runAllRequested = false;
    if (clickRunAll()) {
      sendProgress({ stage: "run-all", progress: 15, message: "Đã gửi lệnh Run all · lần đầu có thể mất 3–10 phút…" });
      return;
    }
    sendProgress({ stage: "manual", progress: 5, message: "Đang chờ giao diện Colab sẵn sàng…" });
  }

  function installHelper() {
    if (document.getElementById("neko-colab-helper") || !document.body) return;
    const helper = document.createElement("button");
    helper.id = "neko-colab-helper";
    helper.textContent = "Đang chuẩn bị Colab…";
    helper.style.cssText = "position:fixed;left:18px;bottom:18px;z-index:2147483647;background:#ff2d55;color:white;border:0;border-radius:999px;padding:12px 18px;font:600 14px system-ui;box-shadow:0 8px 30px #0005;cursor:pointer";
    helper.addEventListener("click", () => resetAutomation(true));
    document.body.appendChild(helper);
  }

  function scheduleTick(delay = 0) {
    clearTimeout(automationTimer);
    automationTimer = setTimeout(tick, delay);
  }

  function resetAutomation(force = false) {
    if (force) {
      lastHandshake = "";
      lastProgress = "";
      lastError = "";
      runAllRequested = false;
      lastRunAllAt = 0;
      lastNotebookProgressAt = 0;
      lastControlClickAt = 0;
      forceRunPending = true;
      rootsExpireAt = 0;
    }
    scheduleTick(50);
  }

  function tick() {
    installHelper();
    const text = pageText();
    scanNotebookOutput(text);
    if (lastHandshake) {
      automationTimer = null;
      return;
    }
    automateColab(text);
    scheduleTick(2000);
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "START_COLAB_AUTOMATION") return false;
    resetAutomation(Boolean(message.force));
    sendResponse({ ok: true });
    return false;
  });

  tick();
}
