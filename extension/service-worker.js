const NOTEBOOK_URL = "https://colab.research.google.com/github/Dattrong0512/split-video/blob/main/OmniVoice_API.ipynb";
const NOTEBOOK_PATH = "/github/Dattrong0512/split-video/blob/main/OmniVoice_API.ipynb";
let openingColab = null;
const pendingColabTabs = new Set();

async function protectPrivateStorage() {
  await chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
}

async function startColabAutomation(tabId, force = true) {
  try {
    await chrome.tabs.sendMessage(tabId, { type: "START_COLAB_AUTOMATION", force });
    return;
  } catch (_) {
    // Reloading an unpacked extension does not inject it into tabs that were already open.
  }
  await chrome.scripting.executeScript({ target: { tabId }, files: ["content/colab.js"] });
  await chrome.tabs.sendMessage(tabId, { type: "START_COLAB_AUTOMATION", force });
}

chrome.runtime.onInstalled.addListener(async () => {
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
  await protectPrivateStorage();
});

chrome.runtime.onStartup.addListener(() => protectPrivateStorage().catch(() => {}));
protectPrivateStorage().catch(() => {});
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (!pendingColabTabs.has(tabId) || changeInfo.status !== "complete") return;
  if (!(tab.url || "").startsWith("https://colab.research.google.com/")) return;
  pendingColabTabs.delete(tabId);
  startColabAutomation(tabId, false).catch(() => {});
});

chrome.tabs.onRemoved.addListener((tabId) => pendingColabTabs.delete(tabId));

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const fromDubbingNotebook = sender.tab?.url?.startsWith("https://colab.research.google.com/") && sender.tab.url.includes(NOTEBOOK_PATH);
  if (message?.type === "COLAB_READY" && fromDubbingNotebook && /^https:\/\/[a-z0-9-]+\.trycloudflare\.com$/i.test(message.payload?.url || "")) {
    const readyProgress = { stage: "ready", progress: 100, message: "Colab T4 và máy chủ đã sẵn sàng." };
    chrome.storage.session.set({ serverSession: message.payload, colabProgress: readyProgress }).then(() => {
      chrome.runtime.sendMessage({ type: "SERVER_SESSION_UPDATED", payload: message.payload }).catch(() => {});
      chrome.runtime.sendMessage({ type: "COLAB_PROGRESS_UPDATED", payload: readyProgress }).catch(() => {});
      sendResponse({ ok: true });
    });
    return true;
  }
  if (message?.type === "COLAB_PROGRESS" && fromDubbingNotebook) {
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
          await startColabAutomation(existing.id, true);
          return { ok: true, tabId: existing.id, reused: true };
        }
        const tab = await chrome.tabs.create({ url: NOTEBOOK_URL, active: true });
        if (tab.id) {
          if (tab.status === "complete") startColabAutomation(tab.id, false).catch(() => {});
          else pendingColabTabs.add(tab.id);
        }
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
