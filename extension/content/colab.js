const READY_PATTERN = /NEKO_SERVER_READY\s+(\{[^\n]+\})/g;
const PROGRESS_PATTERN = /NEKO_PROGRESS\s+(\{[^\n]+\})/g;
let lastHandshake = "";
let lastProgress = "";
let automationAttempts = 0;
let automationTimer = null;
let runAllRequested = false;

function allElements(selector) {
  const output = [];
  const visit = (root) => {
    output.push(...root.querySelectorAll(selector));
    for (const element of root.querySelectorAll("*")) if (element.shadowRoot) visit(element.shadowRoot);
  };
  visit(document);
  return output;
}

function elementLabel(element) {
  return `${element.textContent || ""} ${element.getAttribute?.("aria-label") || ""} ${element.getAttribute?.("title") || ""}`.replace(/\s+/g, " ").trim();
}

function findControl(pattern, selectors = "button, [role=button], [role=menuitem], colab-toolbar-button") {
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
  return [document.body?.innerText || "", ...allElements("colab-output, [role=status]").map(elementLabel)].join("\n");
}

function scanNotebookOutput() {
  const text = pageText();
  let match;
  PROGRESS_PATTERN.lastIndex = 0;
  while ((match = PROGRESS_PATTERN.exec(text))) {
    try { sendProgress(JSON.parse(match[1])); } catch (_) {}
  }
  READY_PATTERN.lastIndex = 0;
  while ((match = READY_PATTERN.exec(text))) {
    if (match[1] === lastHandshake) continue;
    try {
      const payload = JSON.parse(match[1]);
      if (payload.url?.startsWith("https://") && payload.token) {
        lastHandshake = match[1];
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

function automateColab() {
  if (lastHandshake || automationAttempts >= 100) return;
  automationAttempts += 1;
  const text = pageText();
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
    const clicked = clickConfirmation() || clickRunAll();
    sendProgress({ stage: clicked ? "run-all" : "manual", progress: clicked ? 15 : 5, message: clicked ? "Đã gửi lệnh Run all…" : "Chọn Runtime → Run all một lần." });
  });
  document.body.appendChild(helper);
}

function tick() {
  installHelper();
  scanNotebookOutput();
  automateColab();
  clearTimeout(automationTimer);
  automationTimer = setTimeout(tick, lastHandshake ? 5000 : 1800);
}

new MutationObserver(() => scanNotebookOutput()).observe(document.documentElement, { childList: true, subtree: true });
tick();
