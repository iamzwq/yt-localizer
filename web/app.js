// 前端逻辑：准备任务、字幕预览（所见即所得）、导出视频。

const PLAY_RES_Y = 720; // 与后端 ASS PlayResY 对齐，保证预览与烧录一致

const state = {
  jobId: null,
  cues: [],
  currentIndex: -1,
};

const $ = (id) => document.getElementById(id);

function setStatus(msg, isError = false) {
  const el = $("status");
  el.textContent = msg || "";
  el.className = `mt-3 text-sm ${isError ? "text-rose-400" : "text-slate-400"}`;
}

// 阶段标签：SSE 事件 stage -> 中文文案
const STAGE_LABELS = {
  prepare: {
    download: "下载视频",
    subtitle: "获取字幕",
    segment: "断句",
    translate: "翻译",
  },
  export: { burn: "烧录字幕", tts: "合成配音", mux: "合成视频" },
};

// 逐条读取 text/event-stream，onMessage 里抛错会中断并向上传播。
async function readSSE(res, onMessage) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const raw = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 2);
      if (raw.startsWith("data:")) onMessage(JSON.parse(raw.slice(5).trim()));
    }
  }
}

function setBar(scope, text, pct) {
  const bar = $(`${scope}-bar`);
  $(`${scope}-progress`).classList.remove("hidden");
  if (scope === "prepare") $("prepare-progress-text").textContent = text || "";
  else $("export-status").textContent = text || "";
  if (pct == null) {
    bar.style.width = "100%";
    bar.classList.add("animate-pulse");
  } else {
    bar.classList.remove("animate-pulse");
    bar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  }
}

function hideBar(scope) {
  $(`${scope}-progress`).classList.add("hidden");
  const bar = $(`${scope}-bar`);
  bar.classList.remove("animate-pulse");
  bar.style.width = "0%";
}

function renderStage(scope, msg) {
  const label = (STAGE_LABELS[scope] || {})[msg.stage] || msg.stage;
  let text = `${label}…`;
  let pct = null;
  if (msg.stage === "download" && msg.pct != null) {
    text = `下载视频 ${msg.pct}%`;
    pct = msg.pct;
  } else if (msg.stage === "translate" && msg.total) {
    text = `翻译 ${msg.done}/${msg.total}`;
    pct = Math.round((msg.done * 100) / msg.total);
  } else if (msg.stage === "burn" && msg.pct != null) {
    text = `烧录字幕 ${msg.pct}%`;
    pct = msg.pct;
  } else if (msg.stage === "tts" && msg.total) {
    text = `合成配音 ${msg.done}/${msg.total}`;
    pct = Math.round((msg.done * 100) / msg.total);
  } else if (msg.stage === "mux" && msg.pct != null) {
    text = `合成视频 ${msg.pct}%`;
    pct = msg.pct;
  }
  setBar(scope, text, pct);
}

function getStyle() {
  return {
    font_name: "LXGW WenKai Mono",
    font_size: Number($("font-size").value),
    text_color: $("text-color").value,
    bg_color: $("bg-color").value,
    bg_opacity: Number($("bg-opacity").value),
    margin_v: 40,
  };
}

// 十六进制颜色 + 不透明度 → rgba()
function toRgba(hex, opacity) {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${opacity})`;
}

function linesForMode(cue, mode) {
  const text = (cue.text || "").trim();
  const tr = (cue.translation || "").trim();
  if (mode === "translated") return [tr || text];
  if (mode === "bilingual") return tr ? [text, tr] : [text];
  return [text];
}

function applyOverlayStyle() {
  const overlay = $("subtitle-overlay");
  const style = getStyle();
  const player = $("player");
  // 字号相对 720p 画布，按当前显示高度等比换算，实现与烧录一致的预览。
  const displayed = player.clientHeight || 360;
  const px = (style.font_size * displayed) / PLAY_RES_Y;
  overlay.style.fontSize = `${px}px`;
  overlay.style.color = style.text_color;
  overlay.style.padding = `${px * 0.15}px ${px * 0.4}px`;
  overlay.style.borderRadius = `${px * 0.15}px`;
  overlay.style.backgroundColor = toRgba(style.bg_color, style.bg_opacity);
}

function findCueIndex(ms) {
  const arr = state.cues;
  const len = arr.length;
  if (len === 0) return -1;
  if (ms < arr[0].start || ms > arr[len - 1].end) return -1;
  let lo = 0;
  let hi = len - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const c = arr[mid];
    if (ms >= c.start && ms <= c.end) return mid;
    if (ms < c.start) hi = mid - 1;
    else lo = mid + 1;
  }
  return -1;
}

function renderSubtitle() {
  const overlay = $("subtitle-overlay");
  const ms = $("player").currentTime * 1000;
  const idx = findCueIndex(ms);
  if (idx === -1) {
    overlay.classList.add("hidden");
    state.currentIndex = -1;
    return;
  }
  if (idx === state.currentIndex) return;
  state.currentIndex = idx;

  const mode = $("preview-mode").value;
  const lines = linesForMode(state.cues[idx], mode).filter(Boolean);
  if (!lines.length) {
    overlay.classList.add("hidden");
    return;
  }
  overlay.textContent = lines.join("\n");
  overlay.classList.remove("hidden");
}

async function prepare() {
  const url = $("url-input").value.trim();
  if (!url) return setStatus("请输入视频链接", true);

  $("prepare-btn").disabled = true;
  setStatus("");
  setBar("prepare", "开始…", 0);

  let result = null;
  try {
    const res = await fetch("/api/prepare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, translate: $("translate-check").checked }),
    });
    if (!res.ok || !res.body) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || "准备失败");
    }
    await readSSE(res, (msg) => {
      if (msg.stage === "done") result = msg.result;
      else if (msg.stage === "error") throw new Error(msg.detail);
      else renderStage("prepare", msg);
    });
    if (!result) throw new Error("未收到结果");

    state.jobId = result.job_id;
    state.cues = result.cues || [];
    state.currentIndex = -1;

    $("player").src = result.video_url;
    $("meta").textContent =
      `标题：${result.title}　语言：${result.lang}　来源：${result.kind}　字幕：${state.cues.length} 条`;
    $("workspace").classList.remove("hidden");

    applyOverlayStyle();
    const cachedTip = result.cached ? "（已使用缓存）" : "";
    setStatus(
      result.warning
        ? `已准备${cachedTip}（注意：${result.warning}）`
        : `已准备完成${cachedTip}，可预览与下载。`,
    );
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    $("prepare-btn").disabled = false;
    hideBar("prepare");
  }
}

async function exportVideo(mode) {
  if (!state.jobId) return;
  const btnIds = ["export-original", "export-both"];
  btnIds.forEach((id) => ($(id).disabled = true));
  $("export-links").classList.add("hidden");
  $("export-links").innerHTML = "";
  setBar("export", "开始…", 0);

  let result = null;
  try {
    const res = await fetch(`/api/video/${state.jobId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode,
        sub_mode: $("burn-sub-mode").value,
        style: getStyle(),
      }),
    });
    if (!res.ok || !res.body) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || "合成失败");
    }
    await readSSE(res, (msg) => {
      if (msg.stage === "done") result = msg.result;
      else if (msg.stage === "error") throw new Error(msg.detail);
      else renderStage("export", msg);
    });
    const videos = (result && result.videos) || {};
    if (!Object.keys(videos).length) throw new Error("未生成视频");
    renderExportLinks(videos);
    $("export-status").textContent = "合成完成，点击下方链接下载。";
  } catch (err) {
    $("export-status").textContent = err.message;
  } finally {
    btnIds.forEach((id) => ($(id).disabled = false));
    hideBar("export");
  }
}

const VIDEO_LABELS = {
  original: "下载：字幕 + 原声",
  dub: "下载：字幕 + 中文配音",
};

// 多文件自动下载会被浏览器拦截，改为渲染可点击的下载链接。
function renderExportLinks(videos) {
  const box = $("export-links");
  box.innerHTML = "";
  Object.entries(videos).forEach(([key, url]) => {
    const a = document.createElement("a");
    a.href = url;
    a.download = "";
    a.textContent = VIDEO_LABELS[key] || `下载 ${key}`;
    a.className =
      "block rounded bg-sky-700 px-3 py-2 text-center text-sm hover:bg-sky-600";
    box.appendChild(a);
  });
  box.classList.remove("hidden");
}

async function clearCache() {
  if (!confirm("确定清空所有已下载/翻译的缓存？")) return;
  const btn = $("clear-cache-btn");
  btn.disabled = true;
  try {
    const res = await fetch("/api/cache", { method: "DELETE" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "清理失败");
    state.jobId = null;
    state.cues = [];
    state.currentIndex = -1;
    $("workspace").classList.add("hidden");
    setStatus(`已清理 ${data.cleared} 项缓存。`);
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    btn.disabled = false;
  }
}

function bindEvents() {
  $("prepare-btn").addEventListener("click", prepare);
  $("clear-cache-btn").addEventListener("click", clearCache);
  $("player").addEventListener("timeupdate", renderSubtitle);
  $("preview-mode").addEventListener("change", () => {
    state.currentIndex = -1;
    renderSubtitle();
  });

  $("font-size").addEventListener("input", (e) => {
    $("font-size-val").textContent = e.target.value;
    applyOverlayStyle();
  });
  $("bg-opacity").addEventListener("input", (e) => {
    $("bg-opacity-val").textContent = e.target.value;
    applyOverlayStyle();
  });
  ["text-color", "bg-color"].forEach((id) =>
    $(id).addEventListener("input", applyOverlayStyle),
  );
  window.addEventListener("resize", applyOverlayStyle);

  $("export-original").addEventListener("click", () => exportVideo("original"));
  $("export-both").addEventListener("click", () => exportVideo("both"));
}

bindEvents();
