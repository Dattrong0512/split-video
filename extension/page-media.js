// Executed in the page's MAIN world; return only the requested video's media URL.
export function readPageMedia(videoId) {
  const roots = [];
  const visible = [...document.querySelectorAll("video")].filter((video) => {
    const rect = video.getBoundingClientRect();
    return rect.bottom > 0 && rect.right > 0 && rect.top < innerHeight && rect.left < innerWidth;
  });
  for (const video of visible) {
    let node = video;
    for (let depth = 0; node && depth < 12; depth += 1, node = node.parentElement) {
      for (const key of Object.keys(node)) {
        if (/^__react(?:Fiber|Props|InternalInstance)\$/.test(key)) roots.push(node[key]);
      }
    }
  }
  for (const script of document.querySelectorAll('script#RENDER_DATA, script#__NEXT_DATA__, script[type="application/json"]')) {
    const text = script.textContent || "";
    if (!text.includes(videoId) || text.length > 5_000_000) continue;
    try { roots.push(JSON.parse(text)); }
    catch (_) { try { roots.push(JSON.parse(decodeURIComponent(text))); } catch (_) {} }
  }

  const seen = new WeakSet();
  const deadline = Date.now() + 150;
  let inspected = 0;
  const values = (object) => Object.keys(object).slice(0, 150)
    .map((key) => Object.getOwnPropertyDescriptor(object, key)?.value);
  while (roots.length && inspected++ < 15000 && Date.now() < deadline) {
    const value = roots.pop();
    if (!value || typeof value !== "object" || seen.has(value) || value instanceof Node) continue;
    seen.add(value);
    const id = value.aweme_id ?? value.awemeId ?? value.group_id ?? value.id;
    if (String(id) === videoId && value.video) {
      const media = value.video;
      const queue = [media.play_addr, media.playAddr, media.play_addr_h264, media.playAddrH264];
      const mediaSeen = new WeakSet();
      for (let count = 0; queue.length && count < 150; count += 1) {
        const item = queue.shift();
        if (typeof item === "string" && /^https:\/\//i.test(item)) return { mediaUrl: item };
        if (!item || typeof item !== "object" || mediaSeen.has(item)) continue;
        mediaSeen.add(item);
        if (Array.isArray(item)) queue.push(...item.slice(0, 20));
        else queue.push(item.url_list, item.urlList, item.src, item.url);
      }
    }
    roots.push(...values(value).filter((item) => item && typeof item === "object"));
  }
  return null;
}
