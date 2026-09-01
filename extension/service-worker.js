const NOTEBOOK_URL = "https://colab.research.google.com/github/Dattrong0512/split-video/blob/main/OmniVoice_API.ipynb";
const NOTEBOOK_PATH = "/github/Dattrong0512/split-video/blob/main/OmniVoice_API.ipynb";
let openingColab = null;

chrome.runtime.onInstalled.addListener(async () => {
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
  await chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "COLAB_READY" && sender.tab?.url?.startsWith("https://colab.research.google.com/")) {
    const readyProgress = { stage: "ready", progress: 100, message: "Colab T4 và máy chủ đã sẵn sàng." };
    chrome.storage.session.set({ serverSession: message.payload, colabProgress: readyProgress }).then(() => {
      chrome.runtime.sendMessage({ type: "SERVER_SESSION_UPDATED", payload: message.payload }).catch(() => {});
      chrome.runtime.sendMessage({ type: "COLAB_PROGRESS_UPDATED", payload: readyProgress }).catch(() => {});
      sendResponse({ ok: true });
    });
    return true;
  }
  if (message?.type === "COLAB_PROGRESS" && sender.tab?.url?.startsWith("https://colab.research.google.com/")) {
    chrome.storage.session.set({ colabProgress: message.payload }).then(() => {
      chrome.runtime.sendMessage({ type: "COLAB_PROGRESS_UPDATED", payload: message.payload }).catch(() => {});
      sendResponse({ ok: true });
    });
    return true;
  }
  if (message?.type === "OPEN_COLAB") {
    if (!openingColab) {
      openingColab = (async () => {
        const tabs = await chrome.tabs.query({ url: "https://colab.research.google.com/*" });
        const existing = tabs.find((tab) => (tab.url || "").includes(NOTEBOOK_PATH));
        if (existing?.id) {
          await chrome.tabs.update(existing.id, { active: true });
          if (existing.windowId) await chrome.windows.update(existing.windowId, { focused: true });
          return { ok: true, tabId: existing.id, reused: true };
        }
        const tab = await chrome.tabs.create({ url: NOTEBOOK_URL, active: true });
        return { ok: true, tabId: tab.id, reused: false };
      })().finally(() => { openingColab = null; });
    }
    openingColab.then(sendResponse).catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  if (message?.type === "DOWNLOAD_RESULT") {
    chrome.downloads.download({ url: message.url, filename: message.filename, saveAs: false })
      .then((downloadId) => sendResponse({ ok: true, downloadId }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }
  return false;
});
