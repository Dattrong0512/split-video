import { CanvasEditor } from "./canvas-editor.js";
import { cookieSummary, deleteClone, listClones, saveClone } from "./storage.js";

const $ = (selector) => document.querySelector(selector);
const isManualEditorPage = new URLSearchParams(location.search).get("manualEditor") === "1";
if (isManualEditorPage) document.body.classList.add("manual-editor-page");
const editor = new CanvasEditor($("#preview-canvas"));
const state = {
  canonicalUrl: "", server: null, jobId: null, stage: "idle", blurMode: "auto",
  clones: [], voices: [], analysis: null, pending: null, pollTimer: null, recoveryCount: 0, downloadId: null,
  speechRate: 1, previewRate: null, uploadedClones: {},
};

function setStatus(message, progress = null) {
  $("#status").textContent = message;
  if (progress !== null) $("#progress-bar").style.width = `${Math.max(0, Math.min(100, progress))}%`;
}

function setBusy(busy) {
  $("#primary").disabled = busy;
  $("#cancel").hidden = !busy || !state.jobId;
  $("#speech-rate").disabled = busy;
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
  video.src = review.url;
  video.hidden = false;
  video.load();
  state.previewRate = Number(review.speechRate);
  state.speechRate = state.previewRate;
  state.stage = "preview_ready";
  showSpeedCard();
  setBusy(false);
  setStatus(`Đã tạo preview ${review.seconds.toFixed(0)} giây ở tốc độ ${state.previewRate.toFixed(2)}×. Hãy nghe thử.`, 100);
  $("#primary").textContent = "Dùng tốc độ này & tải toàn bộ";
  await persistJob();
  video.play().catch(() => {});
}

function invalidateDubbingReview() {
  if (!state.jobId || !["ready", "preview_ready"].includes(state.stage)) return;
  state.previewRate = null;
  state.stage = "ready";
  $("#review-video").pause();
  $("#review-video").hidden = true;
  $("#primary").textContent = "Tạo lại preview 30 giây";
  setStatus(`Tốc độ ${state.speechRate.toFixed(2)}× chưa được preview. Hãy tạo lại 30 giây để nghe thử.`, 55);
  persistJob().catch(() => {});
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
    try { return await serverFetch("/api/health", { signal: AbortSignal.timeout(8000) }); }
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
    blurMode: state.blurMode, recoveryCount: state.recoveryCount, speechRate: state.speechRate, updatedAt: Date.now(),
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

async function ensureServer(action) {
  const stored = await chrome.storage.session.get(["serverSession", "colabProgress"]);
  state.server = state.server || stored.serverSession || null;
  if (state.server) {
    try {
      const health = await checkServer();
      state.voices = health.voices || [];
      rebuildVoices();
      return true;
    } catch (_) {
      state.server = null;
      await chrome.storage.session.remove(["serverSession", "colabProgress"]);
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
        geminiApiKey: saved.geminiKey, blurMode: state.blurMode,
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
  const container = $("#speaker-voices");
  container.replaceChildren();
  if (!speakers || speakers.length <= 1) { container.hidden = true; return; }
  container.hidden = false;
  for (const speaker of speakers) {
    const row = document.createElement("div"); row.className = "speaker-row";
    const label = document.createElement("span");
    label.textContent = `${speaker.id} · ${speaker.gender === "female" ? "Nữ" : speaker.gender === "male" ? "Nam" : "?"}`;
    const select = document.createElement("select"); select.dataset.speaker = speaker.id;
    fillVoiceSelect(select, $("#default-voice").value);
    row.append(label, select); container.append(row);
  }
}

async function applyAnalysis(analysis) {
  state.analysis = analysis;
  state.stage = "ready";
  state.previewRate = null;
  await persistJob();
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
  showSpeakers(analysis.speakers);
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
  $("#review-video").pause();
  setBusy(true);
  setStatus(previewOnly ? "Đang tạo bản nghe thử 30 giây…" : "Đang xuất toàn bộ video với tốc độ đã chọn…", 58);
  try {
    if (!state.server) throw { code: "TUNNEL_DISCONNECTED" };
    const selections = {};
    if (state.blurMode === "manual") {
      const speakerSelects = [...document.querySelectorAll("#speaker-voices select")];
      if (speakerSelects.length) speakerSelects.forEach((select) => { selections[select.dataset.speaker] = select.value; });
      else selections["*"] = $("#default-voice").value;
    } else {
      selections["*"] = $("#default-voice").value;
    }
    const cloneIds = [...new Set(Object.values(selections).filter((value) => value.startsWith("clone:")).map((value) => value.slice(6)))];
    for (const id of cloneIds) {
      if (!state.uploadedClones[id]) state.uploadedClones[id] = await uploadClone(id);
    }
    for (const [speaker, voice] of Object.entries(selections)) {
      if (voice.startsWith("clone:")) selections[speaker] = `clone:${state.uploadedClones[voice.slice(6)]}`;
    }
    const blurRegions = state.blurMode === "manual" ? editor.blurRegions : (state.analysis?.blurRegions || []);
    const subtitleRect = state.blurMode === "manual" ? editor.subtitleRect : (state.analysis?.subtitleRect || { x: .08, y: .78, w: .84, h: .16 });
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
  }
  const [saved, session] = await Promise.all([
    chrome.storage.local.get(["geminiKey", "douyinCookies", "preferredVoice"]),
    chrome.storage.session.get(["serverSession", "pendingAction", "activeJob", "colabProgress"]),
  ]);
  $("#gemini-key").value = saved.geminiKey || "";
  $("#cookie-status").textContent = saved.douyinCookies ? cookieSummary(saved.douyinCookies).label : "Chưa có cookie.";
  state.clones = await listClones();
  rebuildVoices(saved.preferredVoice);
  if (saved.geminiKey && saved.douyinCookies) { $("#settings-body").hidden = true; $("#toggle-settings").textContent = "Hiện"; }

  const restored = session.activeJob || session.pendingAction;
  if (restored) {
    state.canonicalUrl = restored.canonicalUrl || "";
    state.blurMode = restored.blurMode || "auto";
    state.jobId = session.activeJob?.jobId || null;
    state.stage = session.activeJob?.stage || "idle";
    state.recoveryCount = session.activeJob?.recoveryCount || 0;
    state.speechRate = Number(session.activeJob?.speechRate || 1);
    state.pending = session.pendingAction?.action || null;
    document.querySelectorAll("[data-blur-mode]").forEach((button) => button.classList.toggle("active", button.dataset.blurMode === state.blurMode));
  }
  $("#speech-rate").value = String(state.speechRate);
  $("#speech-rate-value").textContent = `${state.speechRate.toFixed(2)}×`;
  if (isManualEditorPage) {
    const currentTab = await chrome.tabs.getCurrent();
    if (currentTab?.id) await chrome.storage.session.set({ manualEditorTab: { tabId: currentTab.id, jobId: state.jobId } });
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
    } catch (_) {
      state.server = null;
      await chrome.storage.session.remove(["serverSession", "colabProgress"]);
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
  chrome.storage.local.set({ preferredVoice: $("#default-voice").value });
  invalidateDubbingReview();
});
$("#primary").addEventListener("click", async () => {
  if (state.stage === "ready") {
    if (state.blurMode === "manual" && !isManualEditorPage) return openManualEditor();
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
  invalidateDubbingReview();
});
$("#speaker-voices").addEventListener("change", invalidateDubbingReview);
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
