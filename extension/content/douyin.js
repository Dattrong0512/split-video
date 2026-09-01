function canonicalFromText(value) {
  const match = String(value || "").match(/(?:douyin\.com\/video\/|aweme_id[=\"':%]+)(\d{15,25})/i);
  return match ? `https://www.douyin.com/video/${match[1]}` : null;
}

function visibleScore(element) {
  const rect = element.getBoundingClientRect();
  const width = Math.max(0, Math.min(innerWidth, rect.right) - Math.max(0, rect.left));
  const height = Math.max(0, Math.min(innerHeight, rect.bottom) - Math.max(0, rect.top));
  return width * height * (element.paused ? 1 : 4);
}

function currentDouyinVideo() {
  const fromUrl = canonicalFromText(location.href);
  if (fromUrl) return { canonicalUrl: fromUrl, source: "url" };

  const videos = [...document.querySelectorAll("video")].sort((a, b) => visibleScore(b) - visibleScore(a));
  for (const video of videos) {
    let node = video;
    for (let depth = 0; node && depth < 9; depth += 1, node = node.parentElement) {
      const links = node.matches?.("a[href]") ? [node] : [...(node.querySelectorAll?.("a[href]") || [])];
      for (const link of links) {
        const canonicalUrl = canonicalFromText(link.href);
        if (canonicalUrl) return { canonicalUrl, source: "active-video" };
      }
      for (const attribute of ["data-e2e", "data-aweme-id", "data-id"]) {
        const canonicalUrl = canonicalFromText(`${attribute}=${node.getAttribute?.(attribute) || ""}`);
        if (canonicalUrl) return { canonicalUrl, source: "active-video-data" };
      }
    }
  }

  const bodyMatch = canonicalFromText(document.documentElement.innerHTML);
  return bodyMatch ? { canonicalUrl: bodyMatch, source: "page-data" } : null;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "GET_CURRENT_DOUYIN_VIDEO") return false;
  const result = currentDouyinVideo();
  sendResponse(result ? { ok: true, ...result } : { ok: false, error: "Không tìm thấy video Douyin đang hiển thị." });
  return false;
});
