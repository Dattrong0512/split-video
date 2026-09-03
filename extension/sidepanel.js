import { CanvasEditor } from "./canvas-editor.js";
import { cookieSummary, deleteClone, listClones, saveClone } from "./storage.js";

const $ = (selector) => document.querySelector(selector);
const EXPECTED_API_VERSION = "1.5.2";
const pageParams = new URLSearchParams(location.search);
const isManualEditorPage = pageParams.get("manualEditor") === "1";
const isReviewPlayerPage = pageParams.get("reviewPlayer") === "1";
const pageInstanceId = crypto.randomUUID();
const reviewChannel = new BroadcastChannel("douyin-dubbing-review-playback");
if (isManualEditorPage) document.body.classList.add("manual-editor-page");
if (isReviewPlayerPage) document.body.classList.add("review-player-page");
const editor = new CanvasEditor($("#preview-canvas"));
const state = {
  canonicalUrl: "", server: null, jobId: null, stage: "idle", blurMode: "auto",
  clones: [], voices: [], analysis: null, pending: null, pollTimer: null, recoveryCount: 0, downloadId: null,
  speechRate: 1, previewRate: null, immutableReviews: false, renderConfig: null, uploadedClones: {},
  reviewResume: null, voiceCount: 1, voiceSelections: {},
};

function reviewSnapshot(shouldPlay = null) {
  const video = $("#review-video");
  return {
    jobId: state.jobId,
    currentTime: Number.isFinite(video.currentTime) ? video.currentTime : 0,
    shouldPlay: shouldPlay ?? (!video.paused && !video.ended),
    speechRate: state.speechRate,
    createdAt: Date.now(),
  };
}

async function pauseSourceVideo() {
  const tabs = await chrome.tabs.query({ url: ["https://*.douyin.com/*"] });
  await Promise.allSettled(tabs.map((tab) => chrome.tabs.sendMessage(tab.id, {
    type: "PAUSE_DOUYIN_VIDEO", canonicalUrl: state.canonicalUrl,
  })));
}

function pauseOtherReviewPlayers() {
  reviewChannel.postMessage({ type: "claim", sender: pageInstanceId, jobId: state.jobId });
  pauseSourceVideo().catch(() => {});
}

reviewChannel.addEventListener("message", (event) => {
  const message = event.data;
  if (message?.type !== "claim" || message.sender === pageInstanceId || message.jobId !== state.jobId) return;
  $("#review-video").pause();
});

function setStatus(message, progress = null) {
  $("#status").textContent = message;
  if (progress !== null) $("#progress-bar").style.width = `${Math.max(0, Math.min(100, progress))}%`;
}

function setBusy(busy) {
  $("#primary").disabled = busy;
  $("#cancel").hidden = !busy || !state.jobId;
  $("#speech-rate").disabled = busy;
  $("#voice-count").disabled = busy || Boolean(state.jobId);
  document.querySelectorAll("[data-blur-mode]").forEach((button) => { button.disabled = busy; });
}

function mediaTime(value) {
  const seconds = Math.max(0, Number.isFinite(value) ? Math.floor(value) : 0);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function updatePreviewControls() {
  const video = $("#preview-video");
  $("#preview-seek").value = String(video.currentTime || 0);
  $("#preview-time").textContent = `${mediaTime(video.currentTime)} / ${mediaTime(video.duration)}`;
  $("#toggle-preview").textContent = video.paused ? "▶ Phát video" : "❚❚ Tạm dừng";
}

async function setupVideoPreview() {
  const preview = await serverFetch(`/api/jobs/${state.jobId}/preview-token`, { method: "POST" });
  const video = $("#preview-video");
  await editor.setVideo(video, preview.url);
  $("#preview-seek").max = String(video.duration || 0);
  $("#preview-controls").hidden = false;
  updatePreviewControls();
}

function showSpeedCard() {
  $("#speed-card").hidden = false;
  $("#speech-rate").value = String(state.speechRate);
  $("#speech-rate-value").textContent = `${state.speechRate.toFixed(2)}×`;
}

async function showDubbingReview() {
  const review = await serverFetch(`/api/jobs/${state.jobId}/review-token`, { method: "POST" });
  const video = $("#review-video");
  const resume = state.reviewResume ? { ...state.reviewResume } : null;
  if (resume?.shouldPlay && !video.hidden && Number.isFinite(video.currentTime)) {
    resume.currentTime = video.currentTime;
  }
  video.defaultPlaybackRate = 1;
  video.playbackRate = 1;
  video.preservesPitch = true;
  video.src = review.url;
  video.hidden = false;
  $("#open-large-review").hidden = false;
  video.load();
  await new Promise((resolve) => {
    if (video.readyState >= HTMLMediaElement.HAVE_METADATA) resolve();
    else video.addEventListener("loadedmetadata", resolve, { once: true });
  });
  if (resume && Number.isFinite(resume.currentTime)) {
    video.currentTime = Math.min(Math.max(0, resume.currentTime), Math.max(0, video.duration - .05));
  }
  state.previewRate = Number(review.speechRate);
  state.speechRate = state.previewRate;
  state.reviewResume = null;
  state.stage = "preview_ready";
  showSpeedCard();
  setBusy(false);
  setStatus(`Đã tạo preview ${review.seconds.toFixed(0)} giây ở tốc độ ${state.previewRate.toFixed(2)}×. Hãy nghe thử.`, 100);
  $("#primary").textContent = "Dùng tốc độ này & tải toàn bộ";
  await persistJob();
  if (resume?.shouldPlay) {
    pauseOtherReviewPlayers();
    video.play().catch(() => {});
  }
}

function invalidateDubbingReview() {
  if (!state.jobId || !["ready", "preview_ready"].includes(state.stage)) return;
  state.previewRate = null;
  state.stage = "ready";
  const video = $("#review-video");
  video.pause();
  video.defaultPlaybackRate = 1;
  video.playbackRate = 1;
  video.hidden = true;
  $("#open-large-review").hidden = true;
  $("#primary").textContent = "Tạo lại preview 30 giây";
  setStatus(`Tốc độ ${state.speechRate.toFixed(2)}× chưa được preview. Hãy tạo lại 30 giây để nghe thử.`, 55);
  persistJob().catch(() => {});
}

function auditionSpeechRate() {
  if (!state.jobId || !["ready", "preview_ready"].includes(state.stage)) return;
  const video = $("#review-video");
  if (video.hidden || !Number.isFinite(state.previewRate) || state.previewRate <= 0) {
    invalidateDubbingReview();
    return;
  }
  const liveRate = Math.max(.25, Math.min(4, state.speechRate / state.previewRate));
  video.defaultPlaybackRate = liveRate;
  video.playbackRate = liveRate;
  video.preservesPitch = true;
  if (video.ended) video.currentTime = 0;
  pauseOtherReviewPlayers();
  video.play().catch(() => {});
  if (Math.abs(state.speechRate - state.previewRate) <= .001) {
    state.stage = "preview_ready";
    $("#primary").textContent = "Dùng tốc độ này & tải toàn bộ";
    setStatus(`Preview chính xác ở tốc độ ${state.previewRate.toFixed(2)}× đã sẵn sàng.`, 100);
    return;
  }
  state.stage = "ready";
  $("#primary").textContent = "Áp dụng chính xác tốc độ này";
  setStatus(`Đang nghe tức thì ở ${state.speechRate.toFixed(2)}×. Thả thanh để tự tạo preview chính xác.`, 100);
}

function commitAuditionedSpeechRate() {
  if (state.stage !== "ready" || $("#review-video").hidden || !Number.isFinite(state.previewRate)) return;
  render(true);
}

function errorCode(error) {
  return error?.code || error?.detail?.code;
}

function friendlyError(error) {
  const messages = {
    COOKIE_EXPIRED: "Cookie Douyin đã hết hạn. Hãy import cookies.txt mới.",
    INVALID_GEMINI_KEY: "Gemini API key không hợp lệ hoặc đã bị khóa. Hãy thay key mới.",
    GPU_UNAVAILABLE: "Colab không cấp được GPU T4. Có thể tài khoản đã hết hạn mức GPU.",
    DOWNLOAD_FAILED: "Không tải được video Douyin. Hãy làm mới cookie rồi thử lại.",
    TUNNEL_DISCONNECTED: "Phiên Colab đã ngắt. Extension sẽ khởi động lại khi bạn bấm nút.",
    JOB_NOT_FOUND: "Runtime Colab đã khởi động lại nên job cũ không còn. Hãy chạy lại video.",
    NO_SPEECH: "Video không có lời thoại để lồng tiếng.",
    GEMINI_RESPONSE_INVALID: "Gemini trả kết quả không hợp lệ. Hãy thử lại.",
    INVALID_VOICE_MAP: `Hãy chọn đúng ${state.voiceCount} giọng khác nhau.`,
    STALE_RUNTIME: "Colab đang chạy backend cũ. Extension đang khởi động lại phiên mới…",
  };
  return messages[errorCode(error)] || error?.message || error?.detail?.message || String(error || "Có lỗi xảy ra.");
}

async function serverFetch(path, options = {}) {
  if (!state.server) throw { code: "TUNNEL_DISCONNECTED" };
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${state.server.token}`);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  let response;
  try {
    response = await fetch(`${state.server.url}${path}`, { ...options, headers, signal: options.signal || AbortSignal.timeout(30000) });
  } catch (_) {
    state.server = null;
    throw { code: "TUNNEL_DISCONNECTED" };
  }
  const data = response.headers.get("content-type")?.includes("json") ? await response.json() : null;
  if (!response.ok) throw data || { message: `HTTP ${response.status}` };
  return data;
}

async function checkServer(retries = 2) {
  const candidate = state.server;
  let lastError;
  for (let attempt = 0; attempt < retries; attempt += 1) {
    state.server = candidate;
    try {
      const health = await serverFetch("/api/health", { signal: AbortSignal.timeout(8000) });
      if (health.apiVersion !== EXPECTED_API_VERSION) throw { code: "STALE_RUNTIME" };
      state.immutableReviews = Boolean(health.immutableReviews);
      return health;
    }
    catch (error) { lastError = error; }
  }
  state.server = null;
  throw lastError || { code: "TUNNEL_DISCONNECTED" };
}

async function findCurrentVideo(preserveExisting = false) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !/^https:\/\/([^/]+\.)?douyin\.com\//.test(tab.url || "")) {
    if (!preserveExisting) state.canonicalUrl = "";
    $("#video-url").textContent = state.canonicalUrl || "Hãy mở một video trên douyin.com";
    return;
  }
  try {
    let result;
    try {
      result = await chrome.tabs.sendMessage(tab.id, { type: "GET_CURRENT_DOUYIN_VIDEO" });
    } catch (_) {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content/douyin.js"] });
      result = await chrome.tabs.sendMessage(tab.id, { type: "GET_CURRENT_DOUYIN_VIDEO" });
    }
    if (!result?.ok) throw new Error(result?.error);
    state.canonicalUrl = result.canonicalUrl;
    $("#video-url").textContent = result.canonicalUrl;
  } catch (error) {
    if (!preserveExisting) state.canonicalUrl = "";
    $("#video-url").textContent = state.canonicalUrl || "Không tìm thấy video đang phát";
    if (!state.canonicalUrl) setStatus(error.message || "Tải lại trang Douyin rồi thử lại.");
  }
}

function voiceCatalog() {
  const presets = state.voices.length ? state.voices : [
    { id: "edge:vi-VN-HoaiMyNeural", name: "Hoài My · Nữ" },
    { id: "edge:vi-VN-NamMinhNeural", name: "Nam Minh · Nam" },
  ];
  return [...presets, ...state.clones.map((voice) => ({ id: `clone:${voice.id}`, name: `${voice.name} · Clone` }))];
}

function fillVoiceSelect(select, preferred) {
  select.replaceChildren(...voiceCatalog().map((voice) => {
    const option = document.createElement("option");
    option.value = voice.id;
    option.textContent = voice.name;
    return option;
  }));
  if ([...select.options].some((option) => option.value === preferred)) select.value = preferred;
}

function rebuildVoices(preferred = $("#default-voice").value) {
  fillVoiceSelect($("#default-voice"), preferred);
  document.querySelectorAll("#speaker-voices select").forEach((select) => fillVoiceSelect(select, select.value || preferred));
}

function speakerLabel(speakerId) {
  const speaker = state.analysis?.speakers?.find((item) => item.id === speakerId);
  const gender = speaker?.gender === "female" ? " · Nữ" : speaker?.gender === "male" ? " · Nam" : "";
  return `Nhân vật ${speakerId.slice(1)}${gender}`;
}

function collectVoiceSelections() {
  if (state.voiceCount === 1) return { "*": $("#default-voice").value };
  const selections = { S1: $("#default-voice").value };
  for (let index = 2; index <= state.voiceCount; index += 1) {
    const select = document.querySelector(`#speaker-voices select[data-speaker="S${index}"]`);
    selections[`S${index}`] = select?.value || $("#default-voice").value;
  }
  return selections;
}

function validateVoiceSelections() {
  const selections = collectVoiceSelections();
  if (new Set(Object.values(selections)).size !== state.voiceCount) {
    return { code: "INVALID_VOICE_MAP", message: `Hãy chọn đúng ${state.voiceCount} giọng khác nhau.` };
  }
  return null;
}

function rememberVoiceSelections() {
  const current = collectVoiceSelections();
  if (current["*"]) state.voiceSelections.S1 = current["*"];
  else Object.assign(state.voiceSelections, current);
  return chrome.storage.local.set({
    preferredVoice: state.voiceSelections.S1,
    preferredVoiceCount: state.voiceCount,
    preferredVoiceMap: state.voiceSelections,
  });
}

function rebuildVoiceSlots() {
  $("#default-voice-label").textContent = speakerLabel("S1");
  const container = $("#speaker-voices");
  container.replaceChildren();
  container.hidden = state.voiceCount === 1;
  for (let index = 2; index <= state.voiceCount; index += 1) {
    const speakerId = `S${index}`;
    const row = document.createElement("div"); row.className = "speaker-row";
    const label = document.createElement("span"); label.textContent = speakerLabel(speakerId);
    const select = document.createElement("select"); select.dataset.speaker = speakerId;
    fillVoiceSelect(select, state.voiceSelections[speakerId] || $("#default-voice").value);
    row.append(label, select); container.append(row);
  }
  $("#voice-help").textContent = state.voiceCount === 1
    ? "Một giọng duy nhất sẽ được dùng cho mọi nhân vật trong video."
    : `Chọn ${state.voiceCount} giọng khác nhau. AI chỉ phân vai trong đúng ${state.voiceCount} nhân vật này.`;
}

async function storePending(action) {
  state.pending = action;
  await chrome.storage.session.set({ pendingAction: { action, canonicalUrl: state.canonicalUrl, blurMode: state.blurMode, createdAt: Date.now() } });
}

async function clearPending() {
  state.pending = null;
  await chrome.storage.session.remove("pendingAction");
}

async function persistJob() {
  if (!state.jobId) return chrome.storage.session.remove("activeJob");
  return chrome.storage.session.set({ activeJob: {
    jobId: state.jobId, canonicalUrl: state.canonicalUrl, stage: state.stage,
    blurMode: state.blurMode, voiceCount: state.voiceCount,
    recoveryCount: state.recoveryCount, speechRate: state.speechRate,
    renderConfig: state.renderConfig, updatedAt: Date.now(),
  } });
}

async function openManualEditor() {
  const url = chrome.runtime.getURL("sidepanel.html?manualEditor=1");
  const stored = await chrome.storage.session.get("manualEditorTab");
  const existing = stored.manualEditorTab;
  if (existing?.tabId) {
    try {
      const update = existing.jobId === state.jobId ? { active: true } : { active: true, url };
      const tab = await chrome.tabs.update(existing.tabId, update);
      if (tab.windowId !== undefined) await chrome.windows.update(tab.windowId, { focused: true });
      await chrome.storage.session.set({ manualEditorTab: { tabId: tab.id, jobId: state.jobId } });
      return tab;
    } catch (_) {
      await chrome.storage.session.remove("manualEditorTab");
    }
  }
  const tab = await chrome.tabs.create({ url, active: true });
  await chrome.storage.session.set({ manualEditorTab: { tabId: tab.id, jobId: state.jobId } });
  return tab;
}

async function openReviewPlayer() {
  const handoff = reviewSnapshot();
  state.reviewResume = handoff;
  await chrome.storage.session.set({ reviewHandoff: handoff });
  $("#review-video").pause();
  await persistJob();
  const url = chrome.runtime.getURL("sidepanel.html?reviewPlayer=1");
  const stored = await chrome.storage.session.get("reviewPlayerTab");
  const existing = stored.reviewPlayerTab;
  if (existing?.tabId) {
    try {
      const update = existing.jobId === state.jobId ? { active: true } : { active: true, url };
      const tab = await chrome.tabs.update(existing.tabId, update);
      if (tab.windowId !== undefined) await chrome.windows.update(tab.windowId, { focused: true });
      await chrome.storage.session.set({ reviewPlayerTab: { tabId: tab.id, jobId: state.jobId } });
      return tab;
    } catch (_) {
      await chrome.storage.session.remove("reviewPlayerTab");
    }
  }
  const tab = await chrome.tabs.create({ url, active: true });
  await chrome.storage.session.set({ reviewPlayerTab: { tabId: tab.id, jobId: state.jobId } });
  return tab;
}

async function ensureServer(action) {
  const stored = await chrome.storage.session.get(["serverSession", "colabProgress"]);
  state.server = state.server || stored.serverSession || null;
  if (state.server) {
    try {
      const health = await checkServer();
      state.voices = health.voices || [];
      rebuildVoices();
      return true;
    } catch (error) {
      state.server = null;
      await chrome.storage.session.remove(["serverSession", "colabProgress"]);
      if (errorCode(error) === "STALE_RUNTIME" && state.canonicalUrl) {
        state.jobId = null;
        state.stage = "idle";
        state.analysis = null;
        state.previewRate = null;
        state.renderConfig = null;
        await chrome.storage.session.remove("activeJob");
        setBusy(true);
        setStatus("Đã phát hiện Colab cũ. Đang khởi động backend 1.5.2 và tạo lại preview sạch…", 2);
        ensureServer("analyze").catch((restartError) => {
          setBusy(false);
          setStatus(friendlyError(restartError));
        });
        return;
      }
    }
  }
  await storePending(action);
  setStatus(stored.colabProgress?.message || "Đang mở Google Colab và yêu cầu GPU T4…", stored.colabProgress?.progress || 3);
  const opened = await chrome.runtime.sendMessage({ type: "OPEN_COLAB" });
  if (!opened?.ok) throw new Error(opened?.error || "Không mở được Google Colab.");
  if (opened.reused) setStatus("Đang khởi động lại Colab hiện có…", Math.max(5, stored.colabProgress?.progress || 0));
  return false;
}

async function analyze() {
  if (!state.canonicalUrl) {
    await findCurrentVideo();
    if (!state.canonicalUrl) return;
  }
  const saved = await chrome.storage.local.get(["geminiKey", "douyinCookies"]);
  if (!saved.geminiKey) {
    setBusy(false); setStatus("Hãy nhập và lưu Gemini API key."); $("#gemini-key").focus(); return;
  }
  const cookie = cookieSummary(saved.douyinCookies);
  if (!cookie.valid) { setBusy(false); setStatus(cookie.label); return; }
  const voiceError = validateVoiceSelections();
  if (voiceError) { setBusy(false); setStatus(friendlyError(voiceError)); return; }
  state.previewRate = null;
  $("#review-video").pause();
  $("#review-video").hidden = true;
  $("#speed-card").hidden = true;
  setBusy(true);
  try {
    if (!await ensureServer("analyze")) return;
    await clearPending();
    setStatus("Đang gửi video sang Colab…", 3);
    const response = await serverFetch("/api/jobs/analyze", {
      method: "POST",
      body: JSON.stringify({
        canonicalUrl: state.canonicalUrl, cookieText: saved.douyinCookies,
        geminiApiKey: saved.geminiKey, blurMode: state.blurMode, voiceCount: state.voiceCount,
      }),
    });
    state.jobId = response.jobId;
    state.stage = "analyzing";
    state.analysis = null;
    await persistJob();
    pollJob();
  } catch (error) {
    setBusy(false);
    setStatus(friendlyError(error));
  }
}

function showSpeakers(speakers) {
  state.analysis.speakers = speakers || [];
  rebuildVoiceSlots();
}

async function applyAnalysis(analysis) {
  state.analysis = analysis;
  state.stage = "ready";
  state.previewRate = null;
  state.renderConfig = null;
  await persistJob();
  showSpeakers(analysis.speakers);
  if (state.blurMode === "auto") {
    showSpeedCard();
    setStatus("Đã phân tích. Đang tự tạo preview 30 giây để kiểm tra tốc độ giọng…", 56);
    return render(true);
  }
  if (!isManualEditorPage) {
    await openManualEditor();
    setBusy(false);
    setStatus("Đã mở trình chỉnh khung trong một tab mới.", 55);
    $("#primary").textContent = "Mở lại tab chỉnh khung";
    return;
  }
  $("#editor-card").hidden = false;
  await editor.setImage(analysis.previewDataUrl);
  editor.setRegions(analysis.blurRegions, analysis.subtitleRect);
  let previewReady = true;
  try { await setupVideoPreview(); }
  catch (_) { previewReady = false; $("#preview-controls").hidden = true; }
  showSpeedCard();
  setBusy(false);
  setStatus(previewReady ? "Đã phân tích. Chỉnh khung rồi tạo preview 30 giây để nghe tốc độ." : "Đã phân tích. Hãy chỉnh trên ảnh tĩnh rồi tạo preview 30 giây.", 55);
  $("#primary").textContent = "Tạo preview 30 giây";
}

async function recoverDisconnectedWorkflow(error) {
  if (errorCode(error) !== "TUNNEL_DISCONNECTED" || state.recoveryCount >= 2 || !state.canonicalUrl) return false;
  state.recoveryCount += 1;
  state.jobId = null;
  state.stage = "idle";
  state.analysis = null;
  await chrome.storage.session.remove(["activeJob", "serverSession", "colabProgress"]);
  setStatus(`Colab bị ngắt. Đang tự khởi động lại (${state.recoveryCount}/2)…`, 2);
  try {
    await storePending("analyze");
    await ensureServer("analyze");
  } catch (restartError) {
    setBusy(false);
    setStatus(friendlyError(restartError));
  }
  return true;
}

async function pollJob() {
  clearTimeout(state.pollTimer);
  try {
    const job = await serverFetch(`/api/jobs/${state.jobId}`);
    if (job.analysis) state.analysis = job.analysis;
    setStatus(job.message || "Đang xử lý…", job.progress || 0);
    if (job.status === "analysis_ready") return applyAnalysis(job.analysis);
    if (job.status === "preview_ready") return showDubbingReview();
    if (job.status === "complete") return downloadResult();
    if (job.status === "failed" || job.status === "cancelled") throw job.error || { message: job.message };
    state.pollTimer = setTimeout(pollJob, 1500);
  } catch (error) {
    if (await recoverDisconnectedWorkflow(error)) return;
    setBusy(false);
    state.stage = "idle";
    state.jobId = null;
    state.analysis = null;
    await chrome.storage.session.remove("activeJob");
    setStatus(friendlyError(error));
  }
}

async function uploadClone(localId) {
  const clone = state.clones.find((item) => item.id === localId);
  if (!clone) throw new Error("Không tìm thấy clip clone đã chọn.");
  const form = new FormData();
  form.append("name", clone.name);
  form.append("file", clone.blob, clone.fileName);
  const response = await serverFetch("/api/voices", { method: "POST", body: form, signal: AbortSignal.timeout(120000) });
  return response.voiceId;
}

async function render(previewOnly) {
  if (!previewOnly && Math.abs((state.previewRate ?? -1) - state.speechRate) > .001) {
    invalidateDubbingReview();
    return;
  }
  $("#preview-video").pause();
  if (previewOnly) {
    const reviewVideo = $("#review-video");
    state.reviewResume = reviewVideo.hidden ? {
      jobId: state.jobId, currentTime: 0, shouldPlay: true,
      speechRate: state.speechRate, createdAt: Date.now(),
    } : reviewSnapshot();
  }
  if (!previewOnly || !state.immutableReviews) $("#review-video").pause();
  setBusy(true);
  setStatus(previewOnly ? "Đang tạo bản nghe thử 30 giây…" : "Đang xuất toàn bộ video với tốc độ đã chọn…", 58);
  try {
    if (!state.server) throw { code: "TUNNEL_DISCONNECTED" };
    let selections;
    let blurRegions;
    let subtitleRect;
    if (isReviewPlayerPage && state.renderConfig) {
      selections = { ...state.renderConfig.voiceMap };
      blurRegions = state.renderConfig.blurRegions.map((rect) => ({ ...rect }));
      subtitleRect = { ...state.renderConfig.subtitleRect };
    } else {
      const voiceError = validateVoiceSelections();
      if (voiceError) throw voiceError;
      selections = collectVoiceSelections();
      const cloneIds = [...new Set(Object.values(selections).filter((value) => value.startsWith("clone:")).map((value) => value.slice(6)))];
      for (const id of cloneIds) {
        if (!state.uploadedClones[id]) state.uploadedClones[id] = await uploadClone(id);
      }
      for (const [speaker, voice] of Object.entries(selections)) {
        if (voice.startsWith("clone:")) selections[speaker] = `clone:${state.uploadedClones[voice.slice(6)]}`;
      }
      blurRegions = state.blurMode === "manual" ? editor.blurRegions : (state.analysis?.blurRegions || []);
      subtitleRect = state.blurMode === "manual" ? editor.subtitleRect : (state.analysis?.subtitleRect || { x: .08, y: .78, w: .84, h: .16 });
      state.renderConfig = {
        voiceMap: { ...selections },
        blurRegions: blurRegions.map((rect) => ({ ...rect })),
        subtitleRect: { ...subtitleRect },
      };
    }
    await serverFetch(`/api/jobs/${state.jobId}/render`, {
      method: "POST",
      body: JSON.stringify({
        voiceMap: selections, blurRegions, subtitleRect,
        speechRate: state.speechRate, previewOnly,
      }),
      signal: AbortSignal.timeout(120000),
    });
    state.stage = previewOnly ? "rendering_preview" : "rendering";
    await persistJob();
    pollJob();
  } catch (error) {
    if (await recoverDisconnectedWorkflow(error)) return;
    setBusy(false);
    setStatus(friendlyError(error));
  }
}

async function downloadResult() {
  try {
    const result = await serverFetch(`/api/jobs/${state.jobId}/download-token`, { method: "POST" });
    if (result.size) setStatus(`Đang tải video ${(result.size / 1024 / 1024).toFixed(1)} MB…`, 100);
    const response = await chrome.runtime.sendMessage({ type: "DOWNLOAD_RESULT", url: result.url, filename: result.filename });
    if (!response?.ok) throw new Error(response?.error);
    state.downloadId = response.downloadId;
    setStatus(`Render xong. Chrome đang tải${result.size ? ` ${(result.size / 1024 / 1024).toFixed(1)} MB` : ""}…`, 100);
    state.stage = "complete";
    state.jobId = null;
    state.analysis = null;
    state.recoveryCount = 0;
    await chrome.storage.session.remove(["activeJob", "pendingAction"]);
    setBusy(false);
    $("#primary").textContent = "Lồng tiếng video mới";
  } catch (error) {
    setBusy(false);
    setStatus(friendlyError(error));
  }
}

async function resumePending() {
  const action = state.pending;
  if (!action) return;
  await clearPending();
  if (action === "analyze") analyze();
}

async function initialize() {
  if (isManualEditorPage) {
    $("header h1").textContent = "Chỉnh blur & subtitle";
  } else if (isReviewPlayerPage) {
    $("header h1").textContent = "Preview lồng tiếng 30 giây";
  }
  const [saved, session] = await Promise.all([
    chrome.storage.local.get(["geminiKey", "douyinCookies", "preferredVoice", "preferredVoiceCount", "preferredVoiceMap"]),
    chrome.storage.session.get(["serverSession", "pendingAction", "activeJob", "colabProgress", "reviewHandoff"]),
  ]);
  $("#gemini-key").value = saved.geminiKey || "";
  $("#cookie-status").textContent = saved.douyinCookies ? cookieSummary(saved.douyinCookies).label : "Chưa có cookie.";
  state.clones = await listClones();
  state.voiceCount = Math.max(1, Math.min(4, Number(saved.preferredVoiceCount || 1)));
  state.voiceSelections = { ...(saved.preferredVoiceMap || {}) };
  if (!state.voiceSelections.S1 && saved.preferredVoice) state.voiceSelections.S1 = saved.preferredVoice;
  $("#voice-count").value = String(state.voiceCount);
  rebuildVoices(state.voiceSelections.S1 || saved.preferredVoice);
  rebuildVoiceSlots();
  if (saved.geminiKey && saved.douyinCookies) { $("#settings-body").hidden = true; $("#toggle-settings").textContent = "Hiện"; }

  const restored = session.activeJob || session.pendingAction;
  if (restored) {
    state.canonicalUrl = restored.canonicalUrl || "";
    state.blurMode = restored.blurMode || "auto";
    state.voiceCount = Math.max(1, Math.min(4, Number(restored.voiceCount || state.voiceCount)));
    $("#voice-count").value = String(state.voiceCount);
    rebuildVoiceSlots();
    state.jobId = session.activeJob?.jobId || null;
    state.stage = session.activeJob?.stage || "idle";
    state.recoveryCount = session.activeJob?.recoveryCount || 0;
    state.speechRate = Number(session.activeJob?.speechRate || 1);
    state.renderConfig = session.activeJob?.renderConfig || null;
    state.pending = session.pendingAction?.action || null;
    document.querySelectorAll("[data-blur-mode]").forEach((button) => button.classList.toggle("active", button.dataset.blurMode === state.blurMode));
  }
  $("#speech-rate").value = String(state.speechRate);
  $("#speech-rate-value").textContent = `${state.speechRate.toFixed(2)}×`;
  if (isManualEditorPage) {
    const currentTab = await chrome.tabs.getCurrent();
    if (currentTab?.id) await chrome.storage.session.set({ manualEditorTab: { tabId: currentTab.id, jobId: state.jobId } });
  } else if (isReviewPlayerPage) {
    const currentTab = await chrome.tabs.getCurrent();
    if (currentTab?.id) await chrome.storage.session.set({ reviewPlayerTab: { tabId: currentTab.id, jobId: state.jobId } });
    if (session.reviewHandoff?.jobId === state.jobId) state.reviewResume = session.reviewHandoff;
  }
  await findCurrentVideo(Boolean(state.canonicalUrl));

  state.server = session.serverSession || null;
  if (state.server) {
    try {
      const health = await checkServer();
      state.voices = health.voices || [];
      rebuildVoices(saved.preferredVoice);
      if (state.jobId) { setBusy(true); pollJob(); return; }
      if (state.pending) { resumePending(); return; }
    } catch (error) {
      state.server = null;
      await chrome.storage.session.remove(["serverSession", "colabProgress"]);
      if (errorCode(error) === "STALE_RUNTIME" && state.canonicalUrl) {
        state.jobId = null;
        state.stage = "idle";
        state.analysis = null;
        state.previewRate = null;
        state.renderConfig = null;
        await chrome.storage.session.remove("activeJob");
        setBusy(true);
        setStatus("Đã phát hiện Colab cũ. Đang khởi động backend 1.5.2 và tạo lại preview sạch…", 2);
        ensureServer("analyze").catch((restartError) => {
          setBusy(false);
          setStatus(friendlyError(restartError));
        });
        return;
      }
    }
  }
  if (state.pending) {
    setBusy(true);
    setStatus(session.colabProgress?.message || "Đang tiếp tục khởi động Colab…", session.colabProgress?.progress || 3);
    ensureServer("analyze").catch((error) => { setBusy(false); setStatus(friendlyError(error)); });
  }
}

$("#save-key").addEventListener("click", async () => {
  const geminiKey = $("#gemini-key").value.trim();
  await chrome.storage.local.set({ geminiKey });
  setStatus(geminiKey ? "Đã lưu Gemini API key trong Chrome profile." : "Đã xóa Gemini API key.");
});
$("#cookie-file").addEventListener("change", async (event) => {
  const file = event.target.files[0]; if (!file) return;
  const text = await file.text(); const summary = cookieSummary(text);
  $("#cookie-status").textContent = summary.label;
  if (summary.valid) { await chrome.storage.local.set({ douyinCookies: text }); setStatus("Đã lưu cookie Douyin."); }
  event.target.value = "";
});
$("#clear-cookie").addEventListener("click", async () => { await chrome.storage.local.remove("douyinCookies"); $("#cookie-status").textContent = "Chưa có cookie."; });
$("#toggle-settings").addEventListener("click", () => { const body = $("#settings-body"); body.hidden = !body.hidden; $("#toggle-settings").textContent = body.hidden ? "Hiện" : "Ẩn"; });
$("#refresh-video").addEventListener("click", () => findCurrentVideo());
$("#default-voice").addEventListener("change", () => {
  state.voiceSelections.S1 = $("#default-voice").value;
  rememberVoiceSelections();
  state.renderConfig = null;
  invalidateDubbingReview();
});
$("#voice-count").addEventListener("change", async (event) => {
  const previous = collectVoiceSelections();
  if (previous["*"]) state.voiceSelections.S1 = previous["*"];
  else Object.assign(state.voiceSelections, previous);
  state.voiceCount = Math.max(1, Math.min(4, Number(event.target.value || 1)));
  rebuildVoiceSlots();
  await rememberVoiceSelections();
  state.renderConfig = null;
  invalidateDubbingReview();
});
$("#open-large-review").addEventListener("click", openReviewPlayer);
$("#review-video").addEventListener("play", pauseOtherReviewPlayers);
chrome.storage.onChanged.addListener((changes, areaName) => {
  const handoff = changes.reviewHandoff?.newValue;
  if (areaName !== "session" || !isReviewPlayerPage || handoff?.jobId !== state.jobId) return;
  state.reviewResume = handoff;
  state.speechRate = Number(handoff.speechRate || state.speechRate);
  showSpeedCard();
  const video = $("#review-video");
  if (video.hidden || !Number.isFinite(handoff.currentTime)) return;
  video.currentTime = Math.min(Math.max(0, handoff.currentTime), Math.max(0, video.duration - .05));
  if (handoff.shouldPlay) video.play().catch(() => {});
});
$("#primary").addEventListener("click", async () => {
  if (state.stage === "ready") {
    if (state.blurMode === "manual" && !isManualEditorPage && !isReviewPlayerPage) return openManualEditor();
    return render(true);
  }
  if (state.stage === "preview_ready") return render(false);
  if (state.stage === "complete") {
    state.stage = "idle";
    state.recoveryCount = 0;
    await findCurrentVideo();
  }
  return analyze();
});
$("#cancel").addEventListener("click", async () => {
  if (state.jobId) await serverFetch(`/api/jobs/${state.jobId}/cancel`, { method: "POST" }).catch(() => {});
  clearTimeout(state.pollTimer);
  state.jobId = null; state.stage = "idle"; state.analysis = null;
  state.previewRate = null;
  state.renderConfig = null;
  await chrome.storage.session.remove(["activeJob", "pendingAction"]);
  setBusy(false); setStatus("Đã hủy.", 0);
});
document.querySelectorAll("[data-blur-mode]").forEach((button) => button.addEventListener("click", () => {
  state.blurMode = button.dataset.blurMode;
  document.querySelectorAll("[data-blur-mode]").forEach((item) => item.classList.toggle("active", item === button));
  $("#frame-mode-label").textContent = state.blurMode === "auto" ? "Tự động + duyệt 30 giây" : "Dừng để chỉnh khung";
  $("#primary").textContent = state.blurMode === "auto" ? "Phân tích video" : "Phân tích & chỉnh khung";
}));
$("#speech-rate").addEventListener("input", (event) => {
  state.speechRate = Number(event.target.value);
  $("#speech-rate-value").textContent = `${state.speechRate.toFixed(2)}×`;
  auditionSpeechRate();
});
$("#speech-rate").addEventListener("change", commitAuditionedSpeechRate);
$("#speaker-voices").addEventListener("change", () => {
  rememberVoiceSelections();
  state.renderConfig = null;
  invalidateDubbingReview();
});
$("#tool-blur").addEventListener("click", () => { editor.tool = "blur"; $("#tool-blur").classList.add("active"); $("#tool-subtitle").classList.remove("active"); });
$("#tool-subtitle").addEventListener("click", () => { editor.tool = "subtitle"; $("#tool-subtitle").classList.add("active"); $("#tool-blur").classList.remove("active"); });
$("#delete-region").addEventListener("click", () => editor.deleteSelected());
$("#toggle-preview").addEventListener("click", async () => {
  const video = $("#preview-video");
  if (video.paused) await video.play(); else video.pause();
  updatePreviewControls();
});
$("#preview-seek").addEventListener("input", (event) => {
  const video = $("#preview-video");
  video.currentTime = Number(event.target.value);
  updatePreviewControls();
});
for (const eventName of ["timeupdate", "play", "pause", "ended", "durationchange"]) {
  $("#preview-video").addEventListener(eventName, updatePreviewControls);
}
$("#add-clone").addEventListener("click", () => $("#clone-dialog").showModal());
$("#delete-clone").addEventListener("click", async () => {
  const value = $("#default-voice").value;
  if (!value.startsWith("clone:")) { setStatus("Hãy chọn một voice clone để xóa."); return; }
  const id = value.slice(6); await deleteClone(id);
  state.clones = state.clones.filter((clone) => clone.id !== id); rebuildVoices();
  setStatus("Đã xóa voice clone khỏi Chrome profile.");
});
$("#choose-clone").addEventListener("click", (event) => {
  event.preventDefault();
  if (!$("#clone-name").value.trim()) { $("#clone-name").focus(); return; }
  $("#clone-dialog").close(); $("#clone-file").click();
});
$("#clone-file").addEventListener("change", async (event) => {
  const file = event.target.files[0]; if (!file) return;
  const objectUrl = URL.createObjectURL(file);
  try {
    const audio = new Audio(objectUrl);
    await new Promise((resolve, reject) => { audio.onloadedmetadata = resolve; audio.onerror = reject; });
    if (audio.duration < 3 || audio.duration > 10.5) throw new Error("Clip clone phải dài từ 3 đến 10 giây.");
    const record = { id: crypto.randomUUID(), name: $("#clone-name").value.trim(), fileName: file.name, type: file.type, blob: file, duration: audio.duration };
    await saveClone(record); state.clones.push(record); rebuildVoices(`clone:${record.id}`);
    $("#default-voice").value = `clone:${record.id}`;
    await chrome.storage.local.set({ preferredVoice: `clone:${record.id}` });
    setStatus(`Đã lưu giọng clone “${record.name}”.`);
  } catch (error) { setStatus(error.message || "Không đọc được clip clone."); }
  finally { URL.revokeObjectURL(objectUrl); event.target.value = ""; }
});

chrome.runtime.onMessage.addListener((message) => {
  if (message?.type === "SERVER_SESSION_UPDATED") {
    state.server = message.payload;
    setStatus("Colab đã sẵn sàng. Đang tiếp tục tự động…", 3);
    resumePending();
  }
  if (message?.type === "COLAB_PROGRESS_UPDATED" && state.pending) {
    setStatus(message.payload.message, message.payload.progress);
    if (message.payload.error) setBusy(false);
  }
  if (message?.type === "DOWNLOAD_PROGRESS" && message.payload?.id === state.downloadId) {
    const { bytesReceived = 0, totalBytes = 0, state: downloadState } = message.payload;
    const percent = totalBytes > 0 ? bytesReceived / totalBytes * 100 : 100;
    if (downloadState === "complete") {
      setStatus("Hoàn tất. Video đã tải xuống máy.", 100);
      state.downloadId = null;
    } else if (downloadState === "interrupted") {
      setStatus("Tải video bị gián đoạn. Hãy thử lại.");
      state.downloadId = null;
    } else {
      const received = (bytesReceived / 1024 / 1024).toFixed(1);
      const total = totalBytes > 0 ? ` / ${(totalBytes / 1024 / 1024).toFixed(1)} MB` : " MB";
      setStatus(`Đang tải ${received}${total}…`, percent);
    }
  }
});

initialize().catch((error) => setStatus(friendlyError(error)));
