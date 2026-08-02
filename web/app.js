// 前端逻辑：准备任务、字幕预览（所见即所得）、下载 SRT 与导出视频。

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
  setStatus("正在下载视频与字幕、断句、翻译…（较慢，请耐心等待）");

  try {
    const res = await fetch("/api/prepare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, translate: $("translate-check").checked }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "准备失败");

    state.jobId = data.job_id;
    state.cues = data.cues || [];
    state.currentIndex = -1;

    $("player").src = data.video_url;
    $("meta").textContent = `标题：${data.title}　语言：${data.lang}　来源：${data.kind}　字幕：${state.cues.length} 条`;
    $("workspace").classList.remove("hidden");

    const base = `/api/srt/${data.job_id}`;
    $("srt-original").href = `${base}?mode=original`;
    $("srt-translated").href = `${base}?mode=translated`;
    $("srt-bilingual").href = `${base}?mode=bilingual`;

    applyOverlayStyle();
    setStatus(data.warning ? `已准备（注意：${data.warning}）` : "已准备完成，可预览与下载。");
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    $("prepare-btn").disabled = false;
  }
}

async function exportVideo(mode) {
  if (!state.jobId) return;
  const btnIds = ["export-original", "export-dub"];
  btnIds.forEach((id) => ($(id).disabled = true));
  $("export-status").textContent =
    mode === "dub" ? "正在生成配音视频（烧录 + TTS + 合成，较慢）…" : "正在烧录字幕视频…";

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
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "合成失败");

    const a = document.createElement("a");
    a.href = data.video_url;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
    $("export-status").textContent = "完成，已开始下载。";
  } catch (err) {
    $("export-status").textContent = err.message;
  } finally {
    btnIds.forEach((id) => ($(id).disabled = false));
  }
}

function bindEvents() {
  $("prepare-btn").addEventListener("click", prepare);
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
    $(id).addEventListener("input", applyOverlayStyle)
  );
  window.addEventListener("resize", applyOverlayStyle);

  $("export-original").addEventListener("click", () => exportVideo("original"));
  $("export-dub").addEventListener("click", () => exportVideo("dub"));
}

bindEvents();
