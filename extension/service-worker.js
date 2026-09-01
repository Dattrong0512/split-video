const NOTEBOOK_URL = "https://colab.research.google.com/github/Dattrong0512/split-video/blob/main/OmniVoice_API.ipynb";

chrome.runtime.onInstalled.addListener(async () => {
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
  await chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "COLAB_READY" && sender.tab?.url?.startsWith("https://colab.research.google.com/")) {
    chrome.storage.session.set({ serverSession: message.payload }).then(() => {
      chrome.runtime.sendMessage({ type: "SERVER_SESSION_UPDATED", payload: message.payload }).catch(() => {});
      sendResponse({ ok: true });
    });
    return true;
  }
  if (message?.type === "OPEN_COLAB") {
    chrome.tabs.create({ url: NOTEBOOK_URL, active: true }).then((tab) => sendResponse({ ok: true, tabId: tab.id }));
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
