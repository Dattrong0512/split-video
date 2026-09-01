const READY_PATTERN = /NEKO_SERVER_READY\s+(\{[^\n]+\})/g;
const PROGRESS_PATTERN = /NEKO_PROGRESS\s+(\{[^\n]+\})/g;
let lastHandshake = "";
let lastProgress = "";
let automationAttempts = 0;
let automationTimer = null;
let runAllRequested = false;
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
  rootsExpireAt = Date.now() + 60000;
  return cachedRoots;
}

function allElements(selector) {
  return roots().flatMap((root) => [...root.querySelectorAll(selector)]);
}

function elementLabel(element) {
  return `${element.textContent || ""} ${element.getAttribute?.("aria-label") || ""} ${element.getAttribute?.("title") || ""}`.replace(/\s+/g, " ").trim();
}

function findControl(pattern, selectors = "button, [role=button], [role=menuitem], colab-toolbar-button") {
  const direct = [...document.querySelectorAll(selectors)].find((element) => pattern.test(elementLabel(element)) && !element.disabled);
  if (direct) return direct;
  return allElements(selectors).find((element) => pattern.test(elementLabel(element)) && !element.disabled);
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
  return document.body?.textContent || "";
}

function scanNotebookOutput(text) {
  let match;
  let newestProgress = null;
  PROGRESS_PATTERN.lastIndex = 0;
  while ((match = PROGRESS_PATTERN.exec(text))) {
    newestProgress = match[1];
  }
  if (newestProgress) try { sendProgress(JSON.parse(newestProgress)); } catch (_) {}
  let newestHandshake = null;
  READY_PATTERN.lastIndex = 0;
  while ((match = READY_PATTERN.exec(text))) {
    newestHandshake = match[1];
  }
  if (newestHandshake && newestHandshake !== lastHandshake) {
    try {
      const payload = JSON.parse(newestHandshake);
      if (payload.url?.startsWith("https://") && payload.token) {
        lastHandshake = newestHandshake;
        chrome.runtime.sendMessage({ type: "COLAB_READY", payload });
      }
    } catch (_) {}
  }
}

function clickConfirmation() {
  const confirm = findControl(/\brun anyway\b|chạy vẫn|^yes\b|^có\b/i);
  if (confirm) { confirm.click(); return true; }
  return false;
}

function clickRunAll() {
  const direct = findControl(/\brun all\b|chạy tất cả/i);
  if (direct) { direct.click(); runAllRequested = true; return true; }
  const runtime = findControl(/(^|\s)runtime(\s|$)|thời gian chạy/i);
  if (!runtime) return false;
  runtime.click();
  setTimeout(() => {
    const item = findControl(/run all|chạy tất cả/i);
    if (item) { item.click(); runAllRequested = true; }
    setTimeout(clickConfirmation, 700);
  }, 500);
  return true;
}

function automateColab(text) {
  if (lastHandshake || automationAttempts >= 100) return;
  automationAttempts += 1;
  if (/allocating|connecting|initializing|đang kết nối|đang phân bổ/i.test(text)) {
    sendProgress({ stage: "connecting", progress: 10, message: "Đang kết nối Colab và yêu cầu GPU T4…" });
    return;
  }
  if (clickConfirmation()) {
    sendProgress({ stage: "confirming", progress: 12, message: "Đang xác nhận phiên Colab…" });
    return;
  }
  if (runAllRequested) {
    sendProgress({ stage: "executing", progress: 18, message: "Colab đang chạy notebook · lần đầu có thể mất 3–10 phút…" });
    return;
  }
  const connect = findControl(/(^|\s)(connect|kết nối)(\s|$)/i);
  if (connect) {
    connect.click();
    sendProgress({ stage: "connecting", progress: 8, message: "Đang tự kết nối Colab và yêu cầu GPU T4…" });
    return;
  }
  if (clickRunAll()) {
    sendProgress({ stage: "run-all", progress: 15, message: "Đã gửi lệnh Run all · lần đầu có thể mất 3–10 phút…" });
    return;
  }
  sendProgress({ stage: "manual", progress: 5, message: "Nếu Colab chưa chạy, bấm nút này một lần." });
}

function installHelper() {
  if (document.getElementById("neko-colab-helper")) return;
  const helper = document.createElement("button");
  helper.id = "neko-colab-helper";
  helper.textContent = "Đang chuẩn bị Colab…";
  helper.style.cssText = "position:fixed;left:18px;bottom:18px;z-index:2147483647;background:#ff2d55;color:white;border:0;border-radius:999px;padding:12px 18px;font:600 14px system-ui;box-shadow:0 8px 30px #0005;cursor:pointer";
  helper.addEventListener("click", () => {
    automationAttempts = 0;
    runAllRequested = false;
    rootsExpireAt = 0;
    const clicked = clickConfirmation() || clickRunAll();
    sendProgress({ stage: clicked ? "run-all" : "manual", progress: clicked ? 15 : 5, message: clicked ? "Đã gửi lệnh Run all…" : "Chọn Runtime → Run all một lần." });
  });
  document.body.appendChild(helper);
}

function tick() {
  installHelper();
  const text = pageText();
  scanNotebookOutput(text);
  automateColab(text);
  clearTimeout(automationTimer);
  automationTimer = setTimeout(tick, lastHandshake ? 5000 : 2000);
}

tick();
