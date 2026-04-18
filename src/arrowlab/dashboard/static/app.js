"use strict";

const video = document.getElementById("video");
const overlay = document.getElementById("overlay");
const ctx = overlay.getContext("2d");
const videoSelect = document.getElementById("videoSelect");
const statusEl = document.getElementById("status");
const curFrameEl = document.getElementById("curFrame");
const totalFramesEl = document.getElementById("totalFrames");
const curTimeDisp = document.getElementById("curTimeDisp");
const durDisp = document.getElementById("durDisp");
const seekBar = document.getElementById("seekBar");
const gotoFrameInput = document.getElementById("gotoFrame");
const annotationView = document.getElementById("annotationView");
const shotControls = document.getElementById("shotControls");
const shotStartBtn = document.getElementById("shotStartBtn");
const shotEndBtn = document.getElementById("shotEndBtn");
const shotCancelBtn = document.getElementById("shotCancelBtn");
const faceDiameterInput = document.getElementById("faceDiameter");

const state = {
  videoPath: null,
  fps: 30,
  info: null,
  mode: null,
  pendingClicks: [],
  pendingShot: null,
  annotation: emptyAnnotation(),
};

function emptyAnnotation() {
  return { corridor: null, target: null, shots: [] };
}

function setStatus(msg, isError = false) {
  statusEl.textContent = msg;
  statusEl.style.color = isError ? "#ff8a8a" : "#8ab4f8";
  if (msg) setTimeout(() => { if (statusEl.textContent === msg) statusEl.textContent = ""; }, 3500);
}

async function loadVideos() {
  const res = await fetch("/api/videos");
  const list = await res.json();
  videoSelect.innerHTML = "";
  for (const name of list) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    videoSelect.appendChild(opt);
  }
  if (list.length) {
    await selectVideo(list[0]);
  }
}

async function selectVideo(path) {
  state.videoPath = path;
  state.pendingClicks = [];
  state.pendingShot = null;
  video.src = `/videos/${path}`;

  const [info, annotation] = await Promise.all([
    fetch(`/api/video-info?path=${encodeURIComponent(path)}`).then(r => r.json()),
    fetch(`/api/annotations/${encodeURIComponent(path)}`).then(r => r.json()),
  ]);
  state.info = info;
  state.fps = info.avg_frame_rate || info.r_frame_rate || 30;
  totalFramesEl.textContent = info.nb_frames || "?";

  state.annotation = {
    corridor: annotation.corridor || null,
    target: annotation.target || null,
    shots: annotation.shots || [],
  };
  if (state.annotation.target?.face_diameter_m) {
    faceDiameterInput.value = state.annotation.target.face_diameter_m;
  }

  updateAnnotationView();
  updateShotButtons();
}

video.addEventListener("loadedmetadata", () => {
  overlay.width = video.videoWidth;
  overlay.height = video.videoHeight;
  seekBar.max = video.duration;
  durDisp.textContent = formatTime(video.duration);
  redraw();
});
video.addEventListener("timeupdate", () => {
  updateFrameReadout();
  seekBar.value = video.currentTime;
  redraw();
});
video.addEventListener("seeked", () => {
  updateFrameReadout();
  seekBar.value = video.currentTime;
  redraw();
});

seekBar.addEventListener("input", () => {
  video.currentTime = parseFloat(seekBar.value);
});

function formatTime(t) {
  if (!isFinite(t)) return "0:00";
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function updateFrameReadout() {
  const f = Math.max(1, Math.round(video.currentTime * state.fps) + 1);
  curFrameEl.textContent = f;
  curTimeDisp.textContent = formatTime(video.currentTime);
}

function currentFrame() {
  return Math.max(1, Math.round(video.currentTime * state.fps) + 1);
}

document.getElementById("playPauseBtn").addEventListener("click", () => {
  if (video.paused) video.play(); else video.pause();
});

for (const btn of document.querySelectorAll(".step")) {
  btn.addEventListener("click", () => stepFrames(parseInt(btn.dataset.step, 10)));
}

function stepFrames(n) {
  video.pause();
  const dt = n / state.fps;
  video.currentTime = Math.max(0, Math.min(video.duration || 1e9, video.currentTime + dt));
}

document.getElementById("gotoBtn").addEventListener("click", gotoFrame);
gotoFrameInput.addEventListener("keydown", (e) => { if (e.key === "Enter") gotoFrame(); });
function gotoFrame() {
  const f = parseInt(gotoFrameInput.value, 10);
  if (!isFinite(f) || f < 1) return;
  video.pause();
  video.currentTime = (f - 1) / state.fps;
}

window.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  if (e.code === "Space") { e.preventDefault(); if (video.paused) video.play(); else video.pause(); }
  else if (e.code === "ArrowLeft") { e.preventDefault(); stepFrames(e.shiftKey ? -10 : -1); }
  else if (e.code === "ArrowRight") { e.preventDefault(); stepFrames(e.shiftKey ? 10 : 1); }
});

for (const btn of document.querySelectorAll(".modes button")) {
  btn.addEventListener("click", () => setMode(btn.dataset.mode));
}

function setMode(mode) {
  state.mode = mode;
  state.pendingClicks = [];
  for (const b of document.querySelectorAll(".modes button")) {
    b.classList.toggle("active", b.dataset.mode === mode);
  }
  shotControls.classList.toggle("hidden", mode !== "shot");
  video.pause();
  redraw();
}

overlay.addEventListener("click", (e) => {
  if (!state.mode || state.mode === "shot") return;
  const rect = overlay.getBoundingClientRect();
  const x = Math.round((e.clientX - rect.left) * (overlay.width / rect.width));
  const y = Math.round((e.clientY - rect.top) * (overlay.height / rect.height));
  state.pendingClicks.push({ x, y });
  handleModeClicks();
  redraw();
});

function handleModeClicks() {
  const clicks = state.pendingClicks;
  if (state.mode === "corridor" && clicks.length === 2) {
    const ys = [clicks[0].y, clicks[1].y].sort((a, b) => a - b);
    state.annotation.corridor = { y_top: ys[0], y_bottom: ys[1] };
    state.pendingClicks = [];
  } else if (state.mode === "target" && clicks.length === 2) {
    const [c, edge] = clicks;
    const r = Math.round(Math.hypot(edge.x - c.x, edge.y - c.y));
    const prev = state.annotation.target || {};
    state.annotation.target = {
      cx: c.x, cy: c.y, r,
      bbox: prev.bbox || null,
      face_diameter_m: parseFloat(faceDiameterInput.value) || 0.40,
    };
    state.pendingClicks = [];
  } else if (state.mode === "bbox" && clicks.length === 2) {
    const x0 = Math.min(clicks[0].x, clicks[1].x);
    const y0 = Math.min(clicks[0].y, clicks[1].y);
    const x1 = Math.max(clicks[0].x, clicks[1].x);
    const y1 = Math.max(clicks[0].y, clicks[1].y);
    if (!state.annotation.target) {
      state.annotation.target = {
        cx: Math.round((x0 + x1) / 2),
        cy: Math.round((y0 + y1) / 2),
        r: 0,
        face_diameter_m: parseFloat(faceDiameterInput.value) || 0.40,
      };
    }
    state.annotation.target.bbox = [x0, y0, x1, y1];
    state.pendingClicks = [];
  }
  updateAnnotationView();
}

shotStartBtn.addEventListener("click", () => {
  state.pendingShot = { start: currentFrame() };
  updateShotButtons();
  setStatus(`shot start at frame ${state.pendingShot.start}`);
});
shotEndBtn.addEventListener("click", () => {
  if (!state.pendingShot) return;
  const end = currentFrame();
  if (end < state.pendingShot.start) {
    setStatus("end frame is before start", true);
    return;
  }
  state.annotation.shots.push({ flight_window: [state.pendingShot.start, end] });
  state.pendingShot = null;
  updateShotButtons();
  updateAnnotationView();
  setStatus(`shot saved (${state.annotation.shots.length} total)`);
});
shotCancelBtn.addEventListener("click", () => {
  state.pendingShot = null;
  updateShotButtons();
  setStatus("pending shot cancelled");
});

function updateShotButtons() {
  const pending = state.pendingShot !== null;
  shotEndBtn.disabled = !pending;
  shotCancelBtn.disabled = !pending;
}

faceDiameterInput.addEventListener("change", () => {
  if (state.annotation.target) {
    state.annotation.target.face_diameter_m = parseFloat(faceDiameterInput.value) || 0.40;
    updateAnnotationView();
  }
});

document.getElementById("clearBtn").addEventListener("click", () => {
  if (!confirm("Clear this video's annotation?")) return;
  state.annotation = emptyAnnotation();
  state.pendingClicks = [];
  state.pendingShot = null;
  updateShotButtons();
  updateAnnotationView();
  redraw();
});

document.getElementById("saveBtn").addEventListener("click", async () => {
  if (!state.videoPath) return;
  const payload = {
    corridor: state.annotation.corridor || null,
    target: state.annotation.target || null,
    shots: state.annotation.shots || [],
  };
  const res = await fetch(`/api/annotations/${encodeURIComponent(state.videoPath)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.ok) setStatus("saved");
  else setStatus("save failed", true);
});

videoSelect.addEventListener("change", () => selectVideo(videoSelect.value));

function redraw() {
  if (!overlay.width) return;
  ctx.clearRect(0, 0, overlay.width, overlay.height);

  const a = state.annotation;
  if (a.corridor) {
    ctx.strokeStyle = "#00e5ff";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(0, a.corridor.y_top); ctx.lineTo(overlay.width, a.corridor.y_top);
    ctx.moveTo(0, a.corridor.y_bottom); ctx.lineTo(overlay.width, a.corridor.y_bottom);
    ctx.stroke();
  }
  if (a.target) {
    if (a.target.r > 0) {
      ctx.strokeStyle = "#ffa500";
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.arc(a.target.cx, a.target.cy, a.target.r, 0, Math.PI * 2);
      ctx.stroke();
      ctx.fillStyle = "#ff4040";
      ctx.beginPath();
      ctx.arc(a.target.cx, a.target.cy, 4, 0, Math.PI * 2);
      ctx.fill();
    }
    if (a.target.bbox) {
      const [x0, y0, x1, y1] = a.target.bbox;
      ctx.strokeStyle = "#ffff00";
      ctx.lineWidth = 2;
      ctx.setLineDash([8, 4]);
      ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
      ctx.setLineDash([]);
    }
  }

  const curF = currentFrame();
  for (const s of a.shots) {
    const [start, end] = s.flight_window;
    const inside = curF >= start && curF <= end;
    ctx.fillStyle = inside ? "#40ff80cc" : "#40808080";
    ctx.fillRect(10, 10 + 30 * a.shots.indexOf(s), 320, 24);
    ctx.fillStyle = "#000";
    ctx.font = "16px system-ui";
    ctx.fillText(`shot ${a.shots.indexOf(s) + 1}: [${start}, ${end}]`, 16, 28 + 30 * a.shots.indexOf(s));
  }

  for (const p of state.pendingClicks) {
    ctx.fillStyle = "#ff00ff";
    ctx.beginPath();
    ctx.arc(p.x, p.y, 6, 0, Math.PI * 2);
    ctx.fill();
  }
}

function updateAnnotationView() {
  const a = state.annotation;
  const shown = {
    corridor: a.corridor,
    target: a.target,
    shots: a.shots,
  };
  annotationView.textContent = JSON.stringify(shown, null, 2);
  redraw();
}

loadVideos().catch(e => setStatus(`load failed: ${e}`, true));
