const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function runDouyin(url, state = null) {
  let listener;
  const context = {
    globalThis: {}, location: { href: url }, history: { state }, innerWidth: 1920, innerHeight: 1080,
    document: { querySelectorAll: () => [], documentElement: { innerHTML: "" }, body: {} },
    chrome: { runtime: { onMessage: { addListener: (value) => { listener = value; } } } },
  };
  context.globalThis = context;
  vm.runInNewContext(fs.readFileSync("extension/content/douyin.js", "utf8"), context);
  let response;
  listener({ type: "GET_CURRENT_DOUYIN_VIDEO" }, {}, (value) => { response = value; });
  return response;
}

assert.equal(
  runDouyin("https://www.douyin.com/video/7662726211088534827").canonicalUrl,
  "https://www.douyin.com/video/7662726211088534827",
);
assert.equal(
  runDouyin("https://www.douyin.com/discover?modal_id=7674912144722875109").canonicalUrl,
  "https://www.douyin.com/video/7674912144722875109",
);
assert.equal(
  runDouyin("https://www.douyin.com/discover", { aweme_id: "7674912144722875109" }).canonicalUrl,
  "https://www.douyin.com/video/7674912144722875109",
);

function runColabWithShadowOutput(output) {
  const messages = [];
  let listener;
  let timer;
  const status = { textContent: output, shadowRoot: null };
  const shadow = {
    host: { matches: () => true }, textContent: output,
    querySelectorAll: (selector) => selector === "*" ? [] : [status],
  };
  const host = { shadowRoot: shadow };
  const body = { textContent: "", appendChild: () => {} };
  const document = {
    body,
    querySelectorAll: (selector) => selector === "*" ? [host] : [],
    getElementById: () => null,
    createElement: () => ({ id: "", textContent: "", style: {}, addEventListener: () => {} }),
  };
  const context = {
    globalThis: {}, document, location: { href: "https://colab.research.google.com/github/Dattrong0512/split-video/blob/main/OmniVoice_API.ipynb" },
    setTimeout: (callback) => { timer = callback; return 1; }, clearTimeout: () => {},
    chrome: { runtime: {
      sendMessage: (message) => { messages.push(message); return Promise.resolve(); },
      onMessage: { addListener: (value) => { listener = value; } },
    } },
  };
  context.globalThis = context;
  vm.runInNewContext(fs.readFileSync("extension/content/colab.js", "utf8"), context);
  return {
    messages, listener,
    setOutput: (value) => { status.textContent = value; shadow.textContent = value; },
    runTimer: () => { const callback = timer; timer = null; callback?.(); },
  };
}

const createdAt = Math.floor(Date.now() / 1000);
const handshake = JSON.stringify({ url: "https://test.trycloudflare.com", token: "temporary-token", createdAt });
const colab = runColabWithShadowOutput(`NEKO_PROGRESS {"stage":"tunnel","progress":82,"message":"ready soon"}\nNEKO_SERVER_READY ${handshake}`);
assert.ok(colab.messages.some((message) => message.type === "COLAB_PROGRESS" && message.payload.progress === 82));
assert.ok(colab.messages.some((message) => message.type === "COLAB_READY" && message.payload.token === "temporary-token"));
assert.equal(typeof colab.listener, "function");

const reinjected = runColabWithShadowOutput(`NEKO_PROGRESS {"stage":"tunnel","progress":82,"message":"old progress"}`);
reinjected.listener({ type: "START_COLAB_AUTOMATION", force: true }, {}, () => {});
const earlierHandshake = JSON.stringify({ url: "https://still-live.trycloudflare.com", token: "existing-token", createdAt: createdAt - 300 });
reinjected.setOutput(`NEKO_PROGRESS {"stage":"tunnel","progress":82,"message":"old progress"}\nNEKO_SERVER_READY ${earlierHandshake}`);
reinjected.runTimer();
assert.ok(reinjected.messages.some((message) => message.type === "COLAB_READY" && message.payload.token === "existing-token"));

async function testServiceWorkerReinjectsExistingColabTab() {
  let runtimeListener;
  let injectionCount = 0;
  let messageCount = 0;
  let healthOk = true;
  let refreshedUrl = "";
  let tabUpdatedListener;
  const event = () => ({ addListener: () => {} });
  const context = {
    fetch: async () => ({ ok: healthOk }),
    AbortSignal: { timeout: () => ({}) },
    setTimeout: (callback) => { callback(); return 1; },
    chrome: {
      sidePanel: { setPanelBehavior: async () => {} },
      storage: {
        local: { setAccessLevel: async () => {} },
        session: { set: async () => {}, remove: async () => {} },
      },
      runtime: {
        onInstalled: event(), onStartup: event(),
        onMessage: { addListener: (value) => { runtimeListener = value; } },
        sendMessage: async () => {},
      },
      tabs: {
        onUpdated: { addListener: (value) => { tabUpdatedListener = value; } }, onRemoved: event(),
        query: async () => [{ id: 7, windowId: 3, url: "https://colab.research.google.com/github/Dattrong0512/split-video/blob/main/OmniVoice_API.ipynb" }],
        update: async (_tabId, options) => {
          refreshedUrl = options.url || "";
          return { id: 7, windowId: 3, status: "loading", url: refreshedUrl };
        },
        create: async () => ({ id: 8 }),
        sendMessage: async () => {
          messageCount += 1;
          if (messageCount === 1) throw new Error("Receiving end does not exist");
          return { ok: true };
        },
      },
      windows: { update: async () => {} },
      scripting: { executeScript: async () => { injectionCount += 1; } },
      downloads: { download: async () => 1, search: async () => [], onChanged: event() },
    },
  };
  vm.runInNewContext(fs.readFileSync("extension/service-worker.js", "utf8"), context);
  const response = await new Promise((resolve) => {
    assert.equal(runtimeListener({ type: "OPEN_COLAB" }, {}, resolve), true);
  });
  assert.equal(response.ok, true);
  assert.equal(response.reused, true);
  assert.equal(refreshedUrl, "https://colab.research.google.com/github/Dattrong0512/split-video/blob/main/OmniVoice_API.ipynb");
  assert.equal(injectionCount, 0);
  assert.equal(messageCount, 0);
  tabUpdatedListener(7, { status: "complete" }, { url: refreshedUrl });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(injectionCount, 1);
  assert.equal(messageCount, 2);

  const notebookSender = { tab: { url: "https://colab.research.google.com/github/Dattrong0512/split-video/blob/main/OmniVoice_API.ipynb" } };
  const live = await new Promise((resolve) => {
    assert.equal(runtimeListener({
      type: "COLAB_READY", payload: { url: "https://live.trycloudflare.com", token: "token" },
    }, notebookSender, resolve), true);
  });
  assert.equal(live.ok, true);

  healthOk = false;
  const stale = await new Promise((resolve) => {
    assert.equal(runtimeListener({
      type: "COLAB_READY", payload: { url: "https://stale.trycloudflare.com", token: "token" },
    }, notebookSender, resolve), true);
  });
  assert.equal(stale.stale, true);
}

testServiceWorkerReinjectsExistingColabTab()
  .then(() => {
    const sidepanel = fs.readFileSync("extension/sidepanel.js", "utf8");
    const styles = fs.readFileSync("extension/sidepanel.css", "utf8");
    const html = fs.readFileSync("extension/sidepanel.html", "utf8");
    const canvasEditor = fs.readFileSync("extension/canvas-editor.js", "utf8");
    assert.match(sidepanel, /sidepanel\.html\?manualEditor=1/);
    assert.match(sidepanel, /chrome\.tabs\.create\(\{ url, active: true \}\)/);
    assert.match(sidepanel, /isManualEditorPage/);
    assert.match(sidepanel, /existing\.jobId === state\.jobId/);
    assert.match(sidepanel, /\{ active: true, url \}/);
    assert.match(styles, /body\.manual-editor-page #canvas-wrap/);
    assert.match(html, /id="toggle-preview"/);
    assert.match(html, /id="preview-seek"/);
    assert.match(html, /id="speech-rate"/);
    assert.match(html, /id="review-video"/);
    assert.match(sidepanel, /\/preview-token/);
    assert.match(sidepanel, /\/review-token/);
    assert.match(sidepanel, /previewOnly/);
    assert.match(sidepanel, /speechRate/);
    assert.match(sidepanel, /speechRate \/ state\.previewRate/);
    assert.match(sidepanel, /preservesPitch = true/);
    assert.match(sidepanel, /addEventListener\("change", commitAuditionedSpeechRate\)/);
    assert.match(sidepanel, /immutableReviews/);
    assert.match(sidepanel, /if \(!previewOnly \|\| !state\.immutableReviews\) \$\("#review-video"\)\.pause\(\)/);
    assert.match(sidepanel, /DOWNLOAD_PROGRESS/);
    assert.match(canvasEditor, /fitDisplay\(\)/);
    console.log("Extension automation tests passed.");
  })
  .catch((error) => { console.error(error); process.exitCode = 1; });
