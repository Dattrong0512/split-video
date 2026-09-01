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
    setTimeout: () => 1, clearTimeout: () => {},
    chrome: { runtime: {
      sendMessage: (message) => { messages.push(message); return Promise.resolve(); },
      onMessage: { addListener: (value) => { listener = value; } },
    } },
  };
  context.globalThis = context;
  vm.runInNewContext(fs.readFileSync("extension/content/colab.js", "utf8"), context);
  return { messages, listener };
}

const createdAt = Math.floor(Date.now() / 1000);
const handshake = JSON.stringify({ url: "https://test.trycloudflare.com", token: "temporary-token", createdAt });
const colab = runColabWithShadowOutput(`NEKO_PROGRESS {"stage":"tunnel","progress":82,"message":"ready soon"}\nNEKO_SERVER_READY ${handshake}`);
assert.ok(colab.messages.some((message) => message.type === "COLAB_PROGRESS" && message.payload.progress === 82));
assert.ok(colab.messages.some((message) => message.type === "COLAB_READY" && message.payload.token === "temporary-token"));
assert.equal(typeof colab.listener, "function");

async function testServiceWorkerReinjectsExistingColabTab() {
  let runtimeListener;
  let injectionCount = 0;
  let messageCount = 0;
  const event = () => ({ addListener: () => {} });
  const context = {
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
        onUpdated: event(), onRemoved: event(),
        query: async () => [{ id: 7, windowId: 3, url: "https://colab.research.google.com/github/Dattrong0512/split-video/blob/main/OmniVoice_API.ipynb" }],
        update: async () => {},
        create: async () => ({ id: 8 }),
        sendMessage: async () => {
          messageCount += 1;
          if (messageCount === 1) throw new Error("Receiving end does not exist");
          return { ok: true };
        },
      },
      windows: { update: async () => {} },
      scripting: { executeScript: async () => { injectionCount += 1; } },
      downloads: { download: async () => 1 },
    },
  };
  vm.runInNewContext(fs.readFileSync("extension/service-worker.js", "utf8"), context);
  const response = await new Promise((resolve) => {
    assert.equal(runtimeListener({ type: "OPEN_COLAB" }, {}, resolve), true);
  });
  assert.equal(response.ok, true);
  assert.equal(response.reused, true);
  assert.equal(injectionCount, 1);
  assert.equal(messageCount, 2);
}

testServiceWorkerReinjectsExistingColabTab()
  .then(() => {
    const sidepanel = fs.readFileSync("extension/sidepanel.js", "utf8");
    const styles = fs.readFileSync("extension/sidepanel.css", "utf8");
    assert.match(sidepanel, /sidepanel\.html\?manualEditor=1/);
    assert.match(sidepanel, /chrome\.tabs\.create\(\{ url, active: true \}\)/);
    assert.match(sidepanel, /isManualEditorPage/);
    assert.match(sidepanel, /existing\.jobId === state\.jobId/);
    assert.match(sidepanel, /\{ active: true, url \}/);
    assert.match(styles, /body\.manual-editor-page #canvas-wrap/);
    console.log("Extension automation tests passed.");
  })
  .catch((error) => { console.error(error); process.exitCode = 1; });
