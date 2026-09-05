const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

function runDouyin(url, state = null, message = { type: "GET_CURRENT_DOUYIN_VIDEO" }, videos = []) {
  let listener;
  const context = {
    globalThis: {}, location: { href: url }, history: { state }, innerWidth: 1920, innerHeight: 1080,
    document: { querySelectorAll: (selector) => selector === "video" ? videos : [], documentElement: { innerHTML: "" }, body: {} },
    chrome: { runtime: { onMessage: { addListener: (value) => { listener = value; } } } },
  };
  context.globalThis = context;
  vm.runInNewContext(fs.readFileSync("extension/content/douyin.js", "utf8"), context);
  let response;
  listener(message, {}, (value) => { response = value; });
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
let sourcePaused = false;
const sourceVideo = { paused: false, pause: () => { sourcePaused = true; sourceVideo.paused = true; } };
assert.equal(
  runDouyin(
    "https://www.douyin.com/video/7662726211088534827", null,
    { type: "PAUSE_DOUYIN_VIDEO", canonicalUrl: "https://www.douyin.com/video/7662726211088534827" },
    [sourceVideo],
  ).paused,
  1,
);
assert.equal(sourcePaused, true);

const visibleMedia = (src, x = 0) => ({
  currentSrc: src, paused: true,
  getBoundingClientRect: () => ({ left: x, right: x + 400, top: 0, bottom: 500 }),
});
const detectedMedia = runDouyin(
  "https://www.douyin.com/search/test?modal_id=7649625894688269809",
  { aweme_id: "7634064818458152218" }, { type: "GET_CURRENT_DOUYIN_VIDEO" },
  [visibleMedia("https://v3.douyinvod.com/hidden.mp4", 3000), visibleMedia("https://v3.douyinvod.com/current.mp4")],
);
assert.equal(detectedMedia.canonicalUrl, "https://www.douyin.com/video/7649625894688269809");
assert.equal(detectedMedia.mediaUrl, "https://v3.douyinvod.com/current.mp4");
assert.equal(runDouyin("https://www.douyin.com/video/7649625894688269809", null,
  { type: "GET_CURRENT_DOUYIN_VIDEO" }, [visibleMedia("blob:https://www.douyin.com/local")]).mediaUrl, undefined);

function testBlobMediaUsesOnlyTheRequestedVideoRecord() {
  const source = fs.readFileSync("extension/page-media.js", "utf8").replace("export function", "function");
  const record = { aweme_id: "7649625894688269809", video: { play_addr: { url_list: ["https://v3.douyinvod.com/correct.mp4"] } } };
  const data = { memoizedProps: { item: record }, sibling: { memoizedProps: {
    item: { aweme_id: "7634064818458152218", video: { play_addr: { url_list: ["https://v3.douyinvod.com/wrong.mp4"] } } },
  } } };
  data.return = data;
  const video = { ...visibleMedia("blob:local"), __reactFiber$test: data };
  const context = { Node: class {}, innerWidth: 1920, innerHeight: 1080,
    document: { querySelectorAll: (selector) => selector === "video" ? [video] : [] } };
  vm.runInNewContext(source, context);
  assert.equal(context.readPageMedia("7649625894688269809").mediaUrl, "https://v3.douyinvod.com/correct.mp4");
  assert.equal(context.readPageMedia("9999999999999999999"), null);
  record.video = { playAddr: [{ src: "https://v3.douyinvod.com/camel-case.mp4" }] };
  assert.equal(context.readPageMedia("7649625894688269809").mediaUrl, "https://v3.douyinvod.com/camel-case.mp4");
}
testBlobMediaUsesOnlyTheRequestedVideoRecord();

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
  let tabUpdateOptions = null;
  let tabUpdatedListener;
  let cookieQuery = null;
  let updateAvailableListener;
  const sessionWrites = [];
  const event = () => ({ addListener: () => {} });
  const context = {
    fetch: async () => ({ ok: healthOk, json: async () => ({ apiVersion: "1.5.9" }) }),
    AbortSignal: { timeout: () => ({}) },
    setTimeout: (callback) => { callback(); return 1; },
    chrome: {
      sidePanel: { setPanelBehavior: async () => {} },
      storage: {
        local: { setAccessLevel: async () => {} },
        session: { set: async (value) => { sessionWrites.push(value); }, remove: async () => {} },
      },
      runtime: {
        onInstalled: event(), onStartup: event(),
        onUpdateAvailable: { addListener: (value) => { updateAvailableListener = value; } },
        onMessage: { addListener: (value) => { runtimeListener = value; } },
        sendMessage: async () => {},
      },
      cookies: {
        getAll: async (query) => {
          cookieQuery = query;
          return [{
            domain: ".douyin.com", path: "/", secure: true, httpOnly: true,
            expirationDate: 2_000_000_000, name: "sessionid", value: "fresh-session",
          }];
        },
      },
      tabs: {
        onUpdated: { addListener: (value) => { tabUpdatedListener = value; } }, onRemoved: event(),
        query: async () => [{ id: 7, windowId: 3, url: "https://colab.research.google.com/github/Dattrong0512/split-video/blob/main/OmniVoice_API.ipynb" }],
        update: async (_tabId, options) => {
          tabUpdateOptions = options;
          return { id: 7, windowId: 3, status: "complete", url: "https://colab.research.google.com/github/Dattrong0512/split-video/blob/main/OmniVoice_API.ipynb" };
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
  assert.equal(typeof updateAvailableListener, "function");
  await updateAvailableListener({ version: "1.5.9" });
  assert.ok(sessionWrites.some((value) => value.extensionUpdate?.version === "1.5.9"));
  const response = await new Promise((resolve) => {
    assert.equal(runtimeListener({ type: "OPEN_COLAB" }, {}, resolve), true);
  });
  assert.equal(response.ok, true);
  assert.equal(response.reused, true);
  assert.equal(tabUpdateOptions.active, true);
  assert.equal(Object.hasOwn(tabUpdateOptions, "url"), false);
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

  const exportedCookies = await new Promise((resolve) => {
    assert.equal(runtimeListener({ type: "EXPORT_DOUYIN_COOKIES" }, {}, resolve), true);
  });
  assert.equal(cookieQuery.domain, "douyin.com");
  assert.equal(exportedCookies.ok, true);
  assert.equal(exportedCookies.count, 1);
  assert.match(exportedCookies.cookieText, /^# Netscape HTTP Cookie File/m);
  assert.match(
    exportedCookies.cookieText,
    /#HttpOnly_\.douyin\.com\tTRUE\t\/\tTRUE\t2000000000\tsessionid\tfresh-session/,
  );
}

async function testCanvasEditorWaitsForDecodedVideoFrame() {
  const context = {
    Image: class { constructor() { this.complete = true; } },
    window: { addEventListener: () => {} },
  };
  context.globalThis = context;
  const source = fs.readFileSync("extension/canvas-editor.js", "utf8")
    .replace("export class CanvasEditor", "class CanvasEditor") + "\nglobalThis.CanvasEditor = CanvasEditor;";
  vm.runInNewContext(source, context);

  const noop = () => {};
  const canvas = {
    width: 1280, height: 720, style: {}, addEventListener: noop,
    parentElement: {
      clientWidth: 1280, clientHeight: 720,
      style: { setProperty: noop },
    },
    getContext: () => ({
      clearRect: noop, drawImage: noop, fillRect: noop, strokeRect: noop, fillText: noop,
    }),
  };
  const video = {
    hidden: true, style: {}, videoWidth: 1280, videoHeight: 720,
    load() { this.onloadedmetadata?.(); },
  };
  const editor = new context.CanvasEditor(canvas);
  let resolved = false;
  const loading = editor.setVideo(video, "https://preview.example/video.mp4").then(() => { resolved = true; });
  await Promise.resolve();
  assert.equal(resolved, false, "metadata alone must not replace the decoded JPEG fallback");
  assert.equal(editor.video, null);
  video.onloadeddata();
  await loading;
  assert.equal(editor.video, video);
  assert.equal(video.hidden, false);
}

function sidepanelFunction(name, followingName) {
  const source = fs.readFileSync("extension/sidepanel.js", "utf8");
  return source.slice(source.indexOf(`async function ${name}(`), source.indexOf(`\n${followingName}`));
}

async function testReviewLoadingHandlesErrorsAndResetsPlaybackSpeed() {
  for (const event of ["loadedmetadata", "error", "timeout"]) {
    const listeners = new Map();
    let timeoutCallback;
    let cleared = false;
    const video = {
      hidden: true, currentTime: 0, duration: 30, playbackRate: 1.4,
      addEventListener: (name, fn) => listeners.set(name, fn),
      removeEventListener: (name) => listeners.delete(name),
      load: () => event === "timeout" ? timeoutCallback() : listeners.get(event)(),
    };
    const state = { speechRate: 1.4, reviewResume: null };
    const context = {
      state, $: (selector) => selector === "#review-video" ? video : {},
      serverFetch: async () => ({ url: "https://preview.example", speechRate: 1, seconds: 30, timingMode: "fit_audio" }),
      setTimeout: (fn) => { timeoutCallback = fn; return 42; },
      clearTimeout: (id) => { assert.equal(id, 42); cleared = true; },
      showSpeedCard() {}, setBusy() {}, setStatus() {}, persistJob: async () => {},
    };
    vm.runInNewContext(sidepanelFunction("showDubbingReview", "function invalidateDubbingReview"), context);
    if (event === "loadedmetadata") {
      await context.showDubbingReview();
      assert.equal(state.stage, "preview_ready");
      assert.equal(state.speechRate, 1);
      assert.equal(state.timingMode, "fit_audio");
    } else {
      await assert.rejects(context.showDubbingReview(), /Không tải được preview/);
    }
    assert.equal(video.playbackRate, 1);
    assert.equal(listeners.size, 0);
    assert.ok(cleared);
  }
}

async function testCancelledPollCannotRestartTheOldJob() {
  let respond;
  const state = { jobId: "old-job" };
  const context = {
    state, clearTimeout() {},
    serverFetch: () => new Promise((resolve) => { respond = resolve; }),
    setStatus: () => { throw new Error("Stale poll must not update UI"); },
  };
  vm.runInNewContext(sidepanelFunction("pollJob", "async function uploadClone"), context);
  const polling = context.pollJob();
  state.jobId = null;
  respond({ status: "analysis_ready" });
  await polling;
}

async function testCurrentVideoRefreshesStaleSelectionAndRetainsSourceTabAcrossColab() {
  let activeTab = { id: 7, url: "https://www.douyin.com/search/test?modal_id=7649625894688269809" };
  const sourceTab = activeTab;
  const state = { canonicalUrl: "https://www.douyin.com/video/7634064818458152218" };
  let mediaRevision = 0;
  const context = {
    state, $: () => ({}), setStatus() {},
    chrome: { tabs: {
      query: async () => [activeTab],
      get: async (id) => { assert.equal(id, 7); return sourceTab; },
      sendMessage: async (id) => {
        assert.equal(id, 7);
        return { ok: true, canonicalUrl: "https://www.douyin.com/video/7649625894688269809",
          mediaUrl: `https://v3.douyinvod.com/video?revision=${++mediaRevision}`, userAgent: "Chrome-Test" };
      },
    } },
  };
  vm.runInNewContext(sidepanelFunction("findCurrentVideo", "function voiceCatalog"), context);
  assert.equal(await context.findCurrentVideo(), true);
  assert.equal(state.canonicalUrl, "https://www.douyin.com/video/7649625894688269809");
  assert.equal(state.sourceTabId, 7);
  activeTab = { id: 8, url: "https://colab.research.google.com/" };
  assert.equal(await context.findCurrentVideo(false, true), true);
  assert.equal(state.mediaUrl, "https://v3.douyinvod.com/video?revision=2");
  activeTab = { id: 9, url: "https://www.tiktok.com/@test/video/123" };
  assert.equal(await context.findCurrentVideo(), false);
  assert.equal(state.canonicalUrl, "");
}

function testTranslationErrorKeepsTheActualValidationReason() {
  const source = fs.readFileSync("extension/sidepanel.js", "utf8");
  const context = { state: { voiceCount: 1 } };
  vm.runInNewContext(source.slice(source.indexOf("function errorCode("), source.indexOf("async function serverFetch(")), context);
  const message = "Gemini trả kết quả không đúng cấu trúc: thiếu source id 7";
  assert.equal(context.friendlyError({ code: "GEMINI_RESPONSE_INVALID", message }), message);
  assert.equal(context.friendlyError({ detail: { code: "GEMINI_RESPONSE_INVALID", message } }), message);
}
testTranslationErrorKeepsTheActualValidationReason();

Promise.all([
  testServiceWorkerReinjectsExistingColabTab(),
  testCanvasEditorWaitsForDecodedVideoFrame(),
  testReviewLoadingHandlesErrorsAndResetsPlaybackSpeed(),
  testCancelledPollCannotRestartTheOldJob(),
  testCurrentVideoRefreshesStaleSelectionAndRetainsSourceTabAcrossColab(),
])
  .then(() => {
    const sidepanel = fs.readFileSync("extension/sidepanel.js", "utf8");
    const styles = fs.readFileSync("extension/sidepanel.css", "utf8");
    const html = fs.readFileSync("extension/sidepanel.html", "utf8");
    const manifest = JSON.parse(fs.readFileSync("extension/manifest.json", "utf8"));
    const canvasEditor = fs.readFileSync("extension/canvas-editor.js", "utf8");
    assert.match(sidepanel, /sidepanel\.html\?manualEditor=1/);
    assert.match(sidepanel, /chrome\.tabs\.create\(\{ url, active: true \}\)/);
    assert.match(sidepanel, /isManualEditorPage/);
    assert.match(sidepanel, /existing\.jobId === state\.jobId/);
    assert.match(sidepanel, /\{ active: true, url \}/);
    assert.match(fs.readFileSync("extension\/service-worker.js", "utf8"), /tabs\.update\(existing\.id, \{ active: true \}\)/);
    assert.match(styles, /body\.manual-editor-page #canvas-wrap/);
    assert.match(html, /id="toggle-preview"/);
    assert.match(html, /id="preview-seek"/);
    assert.match(html, /id="speech-rate"/);
    assert.match(html, /id="voice-count"/);
    assert.match(html, /<option value="1" selected>1<\/option>/);
    assert.match(sidepanel, /voiceCount: 1/);
    assert.match(sidepanel, /voiceCount: state\.voiceCount/);
    assert.match(sidepanel, /return \{ "\*": \$\("#default-voice"\)\.value \}/);
    assert.match(sidepanel, /selections\[`S\$\{index\}`\]/);
    assert.match(sidepanel, /new Set\(Object\.values\(selections\)\)\.size !== state\.voiceCount/);
    assert.match(sidepanel, /INVALID_VOICE_MAP/);
    assert.match(html, /id="review-video"/);
    assert.match(html, /id="open-large-review"/);
    assert.match(sidepanel, /\/preview-token/);
    assert.match(sidepanel, /\/review-token/);
    assert.match(sidepanel, /previewOnly/);
    assert.match(sidepanel, /speechRate/);
    assert.match(sidepanel, /speechRate \/ state\.previewRate/);
    assert.match(sidepanel, /preservesPitch = true/);
    assert.match(sidepanel, /addEventListener\("change", commitAuditionedSpeechRate\)/);
    assert.match(sidepanel, /immutableReviews/);
    assert.match(sidepanel, /if \(!previewOnly \|\| !state\.immutableReviews\) \$\("#review-video"\)\.pause\(\)/);
    assert.match(sidepanel, /sidepanel\.html\?reviewPlayer=1/);
    assert.match(sidepanel, /reviewPlayerTab/);
    assert.match(sidepanel, /new BroadcastChannel\("douyin-dubbing-review-playback"\)/);
    assert.match(sidepanel, /health\.apiVersion !== EXPECTED_API_VERSION/);
    assert.ok(manifest.permissions.includes("cookies"));
    assert.match(sidepanel, /type: "EXPORT_DOUYIN_COOKIES"/);
    assert.match(sidepanel, /await refreshDouyinCookies\(\)/);
    assert.match(html, /id="reload-extension"/);
    assert.match(sidepanel, /chrome\.runtime\.reload\(\)/);
    assert.match(fs.readFileSync("extension\/content\/colab.js", "utf8"), /newestHandshake === rejectedHandshake/);
    assert.match(sidepanel, /type: "PAUSE_DOUYIN_VIDEO"/);
    assert.match(sidepanel, /reviewHandoff: handoff/);
    assert.match(sidepanel, /state\.reviewResume = reviewVideo\.hidden/);
    assert.match(sidepanel, /video\.currentTime = Math\.min\(Math\.max\(0, resume\.currentTime\)/);
    assert.match(sidepanel, /renderConfig/);
    assert.match(styles, /body\.review-player-page #review-video/);
    assert.match(styles, /height: calc\(100vh - 230px\)/);
    assert.match(sidepanel, /DOWNLOAD_PROGRESS/);
    assert.match(canvasEditor, /fitDisplay\(\)/);
    console.log("Extension automation tests passed.");
  })
  .catch((error) => { console.error(error); process.exitCode = 1; });
