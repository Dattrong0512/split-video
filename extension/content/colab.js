const READY_PATTERN = /NEKO_SERVER_READY\s+(\{[^\n]+\})/g;
let lastHandshake = "";

function scanHandshake() {
  const text = document.body?.innerText || "";
  let match;
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

function findRunAllControl() {
  const candidates = [...document.querySelectorAll("[role=menuitem], button, colab-toolbar-button")];
  return candidates.find((element) => /run all|chạy tất cả/i.test(`${element.textContent} ${element.getAttribute("aria-label") || ""}`));
}

function tryRunAll() {
  const direct = findRunAllControl();
  if (direct) {
    direct.click();
    return true;
  }
  const runtime = [...document.querySelectorAll("button, [role=button]")]
    .find((element) => /runtime|thời gian chạy/i.test(`${element.textContent} ${element.getAttribute("aria-label") || ""}`));
  if (runtime) {
    runtime.click();
    setTimeout(() => findRunAllControl()?.click(), 500);
    return true;
  }
  return false;
}

function installHelper() {
  if (document.getElementById("neko-colab-helper")) return;
  const helper = document.createElement("button");
  helper.id = "neko-colab-helper";
  helper.textContent = "▶ Khởi động Douyin Dubbing";
  helper.style.cssText = "position:fixed;left:18px;bottom:18px;z-index:2147483647;background:#ff2d55;color:white;border:0;border-radius:999px;padding:12px 18px;font:600 14px system-ui;box-shadow:0 8px 30px #0005;cursor:pointer";
  helper.addEventListener("click", () => {
    helper.textContent = tryRunAll() ? "Đang khởi động Colab…" : "Hãy chọn Runtime → Run all";
  });
  document.body.appendChild(helper);
  setTimeout(() => {
    if (!lastHandshake) helper.textContent = tryRunAll() ? "Đang khởi động Colab…" : "▶ Bấm để chạy notebook";
  }, 2000);
}

new MutationObserver(() => {
  scanHandshake();
  installHelper();
}).observe(document.documentElement, { childList: true, subtree: true });
installHelper();
scanHandshake();
