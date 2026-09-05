if (!globalThis.__DOUYIN_DUBBING_VIDEO_DETECTOR__) {
globalThis.__DOUYIN_DUBBING_VIDEO_DETECTOR__ = true;

function canonicalFromText(value) {
  const match = String(value || "").match(/(?:douyin\.com\/video\/|(?:aweme|modal|video)[_-]?id[=\"':%]+)(\d{15,25})/i);
  return match ? `https://www.douyin.com/video/${match[1]}` : null;
}

function visibleScore(element) {
  if (!element.getBoundingClientRect) return 0;
  const rect = element.getBoundingClientRect();
  const width = Math.max(0, Math.min(innerWidth, rect.right) - Math.max(0, rect.left));
  const height = Math.max(0, Math.min(innerHeight, rect.bottom) - Math.max(0, rect.top));
  return width * height * (element.paused ? 1 : 4);
}

function activeMedia() {
  const userAgent = globalThis.navigator?.userAgent || "";
  const video = [...document.querySelectorAll("video")]
    .filter((item) => visibleScore(item) > 0)
    .sort((a, b) => visibleScore(b) - visibleScore(a))[0];
  if (!video) return { userAgent };
  const candidates = [video.currentSrc, video.src, ...[...(video.querySelectorAll?.("source[src]") || [])].map((item) => item.src)];
  const mediaUrl = candidates.find((value) => /^https:\/\//i.test(value || ""));
  return mediaUrl ? { mediaUrl, userAgent } : { userAgent };
}

function currentDouyinVideo() {
  let routeState = "";
  try { routeState = JSON.stringify(history.state || {}); } catch (_) {}
  const fromUrl = canonicalFromText(`${location.href} ${routeState}`);
  if (fromUrl) return { canonicalUrl: fromUrl, source: "url" };

  const videos = [...document.querySelectorAll("video")].sort((a, b) => visibleScore(b) - visibleScore(a));
  for (const video of videos) {
    for (const value of [video.currentSrc, video.src, video.poster, ...Object.values(video.dataset || {})]) {
      const canonicalUrl = canonicalFromText(value);
      if (canonicalUrl) return { canonicalUrl, source: "active-video-media" };
    }
    let node = video;
    for (let depth = 0; node && depth < 12 && node !== document.body; depth += 1, node = node.parentElement) {
      const links = node.matches?.("a[href]") ? [node] : [...(node.querySelectorAll?.("a[href]") || [])];
      for (const link of links) {
        const canonicalUrl = canonicalFromText(link.href);
        if (canonicalUrl) return { canonicalUrl, source: "active-video" };
      }
      for (const attribute of ["data-e2e", "data-aweme-id", "data-id"]) {
        const raw = node.getAttribute?.(attribute) || "";
        const canonicalUrl = attribute === "data-aweme-id" && /^\d{15,25}$/.test(raw)
          ? `https://www.douyin.com/video/${raw}` : canonicalFromText(`${attribute}=${raw}`);
        if (canonicalUrl) return { canonicalUrl, source: "active-video-data" };
      }
      const nearby = canonicalFromText(node.outerHTML);
      if (nearby) return { canonicalUrl: nearby, source: "active-video-markup" };
    }
  }

  const bodyMatch = canonicalFromText(document.documentElement.innerHTML);
  return bodyMatch ? { canonicalUrl: bodyMatch, source: "page-data" } : null;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "GET_CURRENT_DOUYIN_VIDEO") {
    const result = currentDouyinVideo();
    sendResponse(result ? { ok: true, ...result, ...activeMedia() } : { ok: false, error: "Không tìm thấy video Douyin đang hiển thị." });
    return false;
  }
  if (message?.type === "PAUSE_DOUYIN_VIDEO") {
    const result = currentDouyinVideo();
    if (message.canonicalUrl && result?.canonicalUrl !== message.canonicalUrl) {
      sendResponse({ ok: true, paused: 0 });
      return false;
    }
    let paused = 0;
    for (const video of document.querySelectorAll("video")) {
      if (video.paused) continue;
      video.pause();
      paused += 1;
    }
    sendResponse({ ok: true, paused });
    return false;
  }
  return false;
});
}
