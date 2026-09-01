import { CanvasEditor } from "./canvas-editor.js";
import { cookieSummary, deleteClone, listClones, saveClone } from "./storage.js";

const $ = (selector) => document.querySelector(selector);
const editor = new CanvasEditor($("#preview-canvas"));
const state = { canonicalUrl: "", server: null, jobId: null, stage: "idle", blurMode: "auto", clones: [], voices: [], pending: null, pollTimer: null };

function setStatus(message, progress = null) {
  $("#status").textContent = message;
  if (progress !== null) $("#progress-bar").style.width = `${Math.max(0, Math.min(100, progress))}%`;
}

function setBusy(busy) {
  $("#primary").disabled = busy;
  $("#cancel").hidden = !busy || !state.jobId;
}

function friendlyError(error) {
  const code = error?.code || error?.detail?.code;
  const messages = {
    COOKIE_EXPIRED: "Cookie Douyin đã hết hạn. Hãy import cookies.txt mới.",
    INVALID_GEMINI_KEY: "Gemini API key không hợp lệ hoặc đã bị khóa. Hãy thay key mới.",
    GPU_UNAVAILABLE: "Colab không có GPU. Chọn Runtime → Change runtime type → T4 GPU.",
    DOWNLOAD_FAILED: "Không tải được video Douyin. Hãy làm mới cookie rồi thử lại.",
    TUNNEL_DISCONNECTED: "Phiên Colab đã ngắt. Bấm lại để mở và khởi động notebook."
  };
  return messages[code] || error?.message || error?.detail?.message || String(error || "Có lỗi xảy ra.");
}

async function serverFetch(path, options = {}) {
  if (!state.server) throw { code: "TUNNEL_DISCONNECTED" };
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${state.server.token}`);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  let response;
  try { response = await fetch(`${state.server.url}${path}`, { ...options, headers }); }
  catch (_) { state.server = null; throw { code: "TUNNEL_DISCONNECTED" }; }
  const data = response.headers.get("content-type")?.includes("json") ? await response.json() : null;
  if (!response.ok) throw data || { message: `HTTP ${response.status}` };
  return data;
}

async function findCurrentVideo() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id || !/^https:\/\/([^/]+\.)?douyin\.com\//.test(tab.url || "")) {
    state.canonicalUrl = "";
    $("#video-url").textContent = "Hãy mở một video trên douyin.com";
    return;
  }
  try {
    const result = await chrome.tabs.sendMessage(tab.id, { type: "GET_CURRENT_DOUYIN_VIDEO" });
    if (!result?.ok) throw new Error(result?.error);
    state.canonicalUrl = result.canonicalUrl;
    $("#video-url").textContent = result.canonicalUrl;
  } catch (error) {
    state.canonicalUrl = "";
    $("#video-url").textContent = "Không tìm thấy video đang phát";
    setStatus(error.message || "Tải lại trang Douyin rồi thử lại.");
  }
}

function optionMarkup(value, label) { return `<option value="${value.replaceAll('"','&quot;')}">${label}</option>`; }
function rebuildVoices() {
  const previous = $("#default-voice").value;
  const presets = state.voices.length ? state.voices : [
    { id: "edge:vi-VN-HoaiMyNeural", name: "Hoài My · Nữ" },
    { id: "edge:vi-VN-NamMinhNeural", name: "Nam Minh · Nam" }
  ];
  const options = [...presets.map((voice) => optionMarkup(voice.id, voice.name)), ...state.clones.map((voice) => optionMarkup(`clone:${voice.id}`, `${voice.name} · Clone`))].join("");
  $("#default-voice").innerHTML = options;
  if ([...$("#default-voice").options].some((o) => o.value === previous)) $("#default-voice").value = previous;
  document.querySelectorAll("#speaker-voices select").forEach((select) => { const selected = select.value; select.innerHTML = options; if ([...select.options].some((o) => o.value === selected)) select.value = selected; });
}

async function ensureServer(action) {
  const stored = await chrome.storage.session.get("serverSession");
  state.server = state.server || stored.serverSession || null;
  if (state.server) {
    try {
      const health = await serverFetch("/api/health");
      state.voices = health.voices || [];
      rebuildVoices();
      return true;
    } catch (_) { state.server = null; await chrome.storage.session.remove("serverSession"); }
  }
  state.pending = action;
  setStatus("Đang mở Google Colab. Nếu cần, bấm nút đỏ “Khởi động Douyin Dubbing” trong notebook.", 3);
  await chrome.runtime.sendMessage({ type: "OPEN_COLAB" });
  return false;
}

async function analyze() {
  if (!state.canonicalUrl) { await findCurrentVideo(); if (!state.canonicalUrl) return; }
  const saved = await chrome.storage.local.get(["geminiKey", "douyinCookies"]);
  if (!saved.geminiKey) { setStatus("Hãy nhập và lưu Gemini API key."); $("#gemini-key").focus(); return; }
  const cookie = cookieSummary(saved.douyinCookies);
  if (!cookie.valid) { setStatus(cookie.label); return; }
  if (!await ensureServer("analyze")) return;
  state.pending = null; setBusy(true); setStatus("Đang gửi video sang Colab…", 5);
  try {
    const response = await serverFetch("/api/jobs/analyze", { method: "POST", body: JSON.stringify({ canonicalUrl: state.canonicalUrl, cookieText: saved.douyinCookies, geminiApiKey: saved.geminiKey, blurMode: state.blurMode }) });
    state.jobId = response.jobId; state.stage = "analyzing"; pollJob();
  } catch (error) { setBusy(false); setStatus(friendlyError(error)); }
}

function showSpeakers(speakers) {
  const container = $("#speaker-voices");
  container.innerHTML = "";
  if (!speakers || speakers.length <= 1) { container.hidden = true; return; }
  container.hidden = false;
  for (const speaker of speakers) {
    const row = document.createElement("div"); row.className = "speaker-row";
    const label = document.createElement("span"); label.textContent = `${speaker.id} · ${speaker.gender === "female" ? "Nữ" : speaker.gender === "male" ? "Nam" : "?"}`;
    const select = document.createElement("select"); select.dataset.speaker = speaker.id;
    row.append(label, select); container.append(row);
  }
  rebuildVoices();
  document.querySelectorAll("#speaker-voices select").forEach((select) => { select.value = $("#default-voice").value; });
}

async function applyAnalysis(analysis) {
  await editor.setImage(analysis.previewDataUrl);
  editor.setRegions(analysis.blurRegions, analysis.subtitleRect);
  $("#editor-card").hidden = false;
  showSpeakers(analysis.speakers);
  state.stage = "ready"; setBusy(false); setStatus("Đã phân tích. Kiểm tra khung và chọn giọng trước khi render.", 55);
  $("#primary").textContent = "Tạo lồng tiếng & tải xuống";
}

async function pollJob() {
  clearTimeout(state.pollTimer);
  try {
    const job = await serverFetch(`/api/jobs/${state.jobId}`);
    setStatus(job.message || "Đang xử lý…", job.progress || 0);
    if (job.status === "analysis_ready") return applyAnalysis(job.analysis);
    if (job.status === "complete") return downloadResult();
    if (job.status === "failed" || job.status === "cancelled") throw job.error || { message: job.message };
    state.pollTimer = setTimeout(pollJob, 1500);
  } catch (error) { setBusy(false); state.stage = "idle"; setStatus(friendlyError(error)); }
}

async function uploadClone(localId) {
  const clone = state.clones.find((item) => item.id === localId);
  if (!clone) throw new Error("Không tìm thấy clip clone đã chọn.");
  const form = new FormData(); form.append("name", clone.name); form.append("file", clone.blob, clone.fileName);
  const response = await serverFetch("/api/voices", { method: "POST", body: form });
  return response.voiceId;
}

async function render() {
  if (!await ensureServer("render")) return;
  setBusy(true); setStatus("Đang chuẩn bị các giọng đã chọn…", 58);
  try {
    const selections = {};
    const speakerSelects = [...document.querySelectorAll("#speaker-voices select")];
    if (speakerSelects.length) speakerSelects.forEach((select) => { selections[select.dataset.speaker] = select.value; });
    else selections["*"] = $("#default-voice").value;
    const cloneIds = [...new Set(Object.values(selections).filter((value) => value.startsWith("clone:")).map((value) => value.slice(6)))];
    const uploaded = {};
    for (const id of cloneIds) uploaded[id] = await uploadClone(id);
    for (const [speaker, voice] of Object.entries(selections)) if (voice.startsWith("clone:")) selections[speaker] = `clone:${uploaded[voice.slice(6)]}`;
    await serverFetch(`/api/jobs/${state.jobId}/render`, { method: "POST", body: JSON.stringify({ voiceMap: selections, blurRegions: editor.blurRegions, subtitleRect: editor.subtitleRect }) });
    state.stage = "rendering"; pollJob();
  } catch (error) { setBusy(false); setStatus(friendlyError(error)); }
}

async function downloadResult() {
  try {
    const result = await serverFetch(`/api/jobs/${state.jobId}/download-token`, { method: "POST" });
    const response = await chrome.runtime.sendMessage({ type: "DOWNLOAD_RESULT", url: result.url, filename: result.filename });
    if (!response?.ok) throw new Error(response?.error);
    setStatus("Hoàn tất. Chrome đang tải video đã lồng tiếng.", 100); state.stage = "complete"; setBusy(false); $("#primary").textContent = "Phân tích video mới";
  } catch (error) { setBusy(false); setStatus(friendlyError(error)); }
}

async function initialize() {
  const saved = await chrome.storage.local.get(["geminiKey", "douyinCookies"]);
  $("#gemini-key").value = saved.geminiKey || "";
  $("#cookie-status").textContent = saved.douyinCookies ? cookieSummary(saved.douyinCookies).label : "Chưa có cookie.";
  state.clones = await listClones(); rebuildVoices(); await findCurrentVideo();
  if (saved.geminiKey && saved.douyinCookies) { $("#settings-body").hidden = true; $("#toggle-settings").textContent = "Hiện"; }
}

$("#save-key").addEventListener("click", async () => { const geminiKey = $("#gemini-key").value.trim(); await chrome.storage.local.set({ geminiKey }); setStatus(geminiKey ? "Đã lưu Gemini API key trong Chrome profile." : "Đã xóa Gemini API key."); });
$("#cookie-file").addEventListener("change", async (event) => { const file = event.target.files[0]; if (!file) return; const text = await file.text(); const summary = cookieSummary(text); $("#cookie-status").textContent = summary.label; if (summary.valid) { await chrome.storage.local.set({ douyinCookies: text }); setStatus("Đã lưu cookie Douyin."); } event.target.value = ""; });
$("#clear-cookie").addEventListener("click", async () => { await chrome.storage.local.remove("douyinCookies"); $("#cookie-status").textContent = "Chưa có cookie."; });
$("#toggle-settings").addEventListener("click", () => { const body = $("#settings-body"); body.hidden = !body.hidden; $("#toggle-settings").textContent = body.hidden ? "Hiện" : "Ẩn"; });
$("#refresh-video").addEventListener("click", findCurrentVideo);
$("#primary").addEventListener("click", async () => { if (state.stage === "ready") return render(); if (state.stage === "complete") { state.jobId = null; state.stage = "idle"; await findCurrentVideo(); } return analyze(); });
$("#cancel").addEventListener("click", async () => { if (state.jobId) await serverFetch(`/api/jobs/${state.jobId}/cancel`, { method: "POST" }).catch(() => {}); clearTimeout(state.pollTimer); setBusy(false); state.stage = "idle"; setStatus("Đã hủy.", 0); });
document.querySelectorAll("[data-blur-mode]").forEach((button) => button.addEventListener("click", () => { state.blurMode = button.dataset.blurMode; document.querySelectorAll("[data-blur-mode]").forEach((b) => b.classList.toggle("active", b === button)); $("#frame-mode-label").textContent = state.blurMode === "auto" ? "Blur tự động" : "Blur thủ công"; }));
$("#tool-blur").addEventListener("click", () => { editor.tool = "blur"; $("#tool-blur").classList.add("active"); $("#tool-subtitle").classList.remove("active"); });
$("#tool-subtitle").addEventListener("click", () => { editor.tool = "subtitle"; $("#tool-subtitle").classList.add("active"); $("#tool-blur").classList.remove("active"); });
$("#delete-region").addEventListener("click", () => editor.deleteSelected());
$("#add-clone").addEventListener("click", () => $("#clone-dialog").showModal());
$("#delete-clone").addEventListener("click", async () => { const value = $("#default-voice").value; if (!value.startsWith("clone:")) { setStatus("Hãy chọn một voice clone để xóa."); return; } const id = value.slice(6); await deleteClone(id); state.clones = state.clones.filter((clone) => clone.id !== id); rebuildVoices(); setStatus("Đã xóa voice clone khỏi Chrome profile."); });
$("#choose-clone").addEventListener("click", (event) => { event.preventDefault(); if (!$("#clone-name").value.trim()) { $("#clone-name").focus(); return; } $("#clone-dialog").close(); $("#clone-file").click(); });
$("#clone-file").addEventListener("change", async (event) => { const file = event.target.files[0]; if (!file) return; try { const audio = new Audio(URL.createObjectURL(file)); await new Promise((resolve, reject) => { audio.onloadedmetadata = resolve; audio.onerror = reject; }); if (audio.duration < 3 || audio.duration > 10.5) throw new Error("Clip clone phải dài từ 3 đến 10 giây."); const record = { id: crypto.randomUUID(), name: $("#clone-name").value.trim(), fileName: file.name, type: file.type, blob: file, duration: audio.duration }; await saveClone(record); state.clones.push(record); rebuildVoices(); $("#default-voice").value = `clone:${record.id}`; setStatus(`Đã lưu giọng clone “${record.name}”.`); } catch (error) { setStatus(error.message || "Không đọc được clip clone."); } event.target.value = ""; });
chrome.runtime.onMessage.addListener((message) => { if (message?.type === "SERVER_SESSION_UPDATED") { state.server = message.payload; setStatus("Colab đã sẵn sàng. Đang tiếp tục…", 4); const action = state.pending; state.pending = null; if (action === "analyze") analyze(); else if (action === "render") render(); } });

initialize();
