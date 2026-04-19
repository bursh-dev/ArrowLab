"use strict";

// ==== Video browser (pre-existing, unchanged) ==============================

const videoSelect = document.getElementById("videoSelect");
const clipsRow = document.getElementById("clipsRow");
const trackedRow = document.getElementById("trackedRow");
const combinedVideo = document.getElementById("combinedVideo");
const statusEl = document.getElementById("status");

function setStatus(msg, isError = false) {
  statusEl.textContent = msg;
  statusEl.style.color = isError ? "#ff8a8a" : "#8ab4f8";
  if (msg) setTimeout(() => { if (statusEl.textContent === msg) statusEl.textContent = ""; }, 3500);
}

async function loadVideos() {
  const list = await (await fetch("/api/videos")).json();
  videoSelect.innerHTML = "";
  for (const name of list) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    videoSelect.appendChild(opt);
  }
  if (list.length) await selectVideo(list[0]);
}

function renderRow(row, paths) {
  row.innerHTML = "";
  if (!paths || !paths.length) {
    row.innerHTML = '<div style="color:#666;font-style:italic;grid-column:1/-1;">nothing processed yet</div>';
    return;
  }
  for (const p of paths) {
    const name = p.split("/").pop();
    const fig = document.createElement("figure");
    const vid = document.createElement("video");
    vid.src = p;
    vid.controls = true;
    vid.preload = "metadata";
    vid.muted = true;
    const cap = document.createElement("figcaption");
    cap.textContent = name;
    fig.appendChild(vid);
    fig.appendChild(cap);
    row.appendChild(fig);
  }
}

async function selectVideo(name) {
  const data = await (await fetch(`/api/view?video=${encodeURIComponent(name)}`)).json();
  renderRow(clipsRow, data.shot_clips);
  renderRow(trackedRow, data.tracked);
  if (data.combined) {
    combinedVideo.src = data.combined;
    combinedVideo.style.display = "";
  } else {
    combinedVideo.removeAttribute("src");
    combinedVideo.load();
    combinedVideo.style.display = "none";
    setStatus("combined synth not rendered yet", true);
  }
}

function allVideos() {
  return document.querySelectorAll("video");
}

document.getElementById("playAllBtn").addEventListener("click", () => {
  for (const v of allVideos()) {
    v.currentTime = 0;
    v.play().catch(() => {});
  }
});
document.getElementById("pauseAllBtn").addEventListener("click", () => {
  for (const v of allVideos()) v.pause();
});
document.getElementById("refreshBtn").addEventListener("click", loadVideos);

videoSelect.addEventListener("change", () => selectVideo(videoSelect.value));

// ==== Live session (WS-driven) =============================================

const phoneStatus = document.getElementById("phoneStatus");
const sessionStatus = document.getElementById("sessionStatus");
const calibStatus = document.getElementById("calibStatus");
const shotCountEl = document.getElementById("shotCount");
const startSessionBtn = document.getElementById("startSessionBtn");
const endSessionBtn = document.getElementById("endSessionBtn");
const captureFrameBtn = document.getElementById("captureFrameBtn");
const shotBtn = document.getElementById("shotBtn");
const liveCombined = document.getElementById("liveCombined");
const liveLog = document.getElementById("liveLog");

const calibrationArea = document.getElementById("calibrationArea");
const calibImage = document.getElementById("calibImage");
const calibOverlay = document.getElementById("calibOverlay");
const calibAnnotationView = document.getElementById("calibAnnotationView");
const calibFaceDiameter = document.getElementById("calibFaceDiameter");
const saveCalibBtn = document.getElementById("saveCalibBtn");
const clearCalibBtn = document.getElementById("clearCalibBtn");
const calibModeBtns = document.querySelectorAll("#calibrationArea .modes button");
const ctxCalib = calibOverlay.getContext("2d");

const fakeSourceArea = document.getElementById("fakeSourceArea");
const fakeSourceVideo = document.getElementById("fakeSourceVideo");
const fakeSourceName = document.getElementById("fakeSourceName");
const fakeCaptureBtn = document.getElementById("fakeCaptureBtn");
let currentFakeSource = null;

let liveWS = null;
let currentCalibUrl = null;

const calib = {
  mode: null,
  pendingClicks: [],
  annotation: { corridor: null, target: null },
  imgLoaded: false,
};

function logLive(line, kind = "info") {
  const div = document.createElement("div");
  const ts = new Date().toLocaleTimeString();
  div.textContent = `[${ts}] ${line}`;
  if (kind === "error") div.style.color = "#ff8080";
  if (kind === "ok") div.style.color = "#9ef89f";
  liveLog.prepend(div);
  while (liveLog.children.length > 40) liveLog.lastChild.remove();
}

function applyState(st) {
  phoneStatus.textContent = st.phone_connected ? "connected" : "none";
  phoneStatus.className = "pill " + (st.phone_connected ? "ok" : "bad");

  sessionStatus.textContent = st.active ? (st.session_id || "active") : "none";
  sessionStatus.className = "pill " + (st.active ? "ok" : "bad");

  calibStatus.textContent = st.has_annotation ? "yes" : "no";
  calibStatus.className = "pill " + (st.has_annotation ? "ok" : "bad");

  shotCountEl.textContent = `${st.shot_count || 0} shots`;

  startSessionBtn.disabled = !!st.active;
  endSessionBtn.disabled = !st.active;
  captureFrameBtn.disabled = !(st.active && st.phone_connected);
  shotBtn.disabled = !(st.active && st.phone_connected && st.has_annotation);

  if (st.active && st.fake_source) {
    fakeSourceArea.classList.remove("hidden");
    if (currentFakeSource !== st.fake_source) {
      currentFakeSource = st.fake_source;
      fakeSourceName.textContent = st.fake_source;
      fakeSourceVideo.src = "/videos/" + encodeURIComponent(st.fake_source);
    }
  } else {
    fakeSourceArea.classList.add("hidden");
    currentFakeSource = null;
    fakeSourceVideo.removeAttribute("src");
    fakeSourceVideo.load();
  }

  if (st.active && st.calibration_frame) {
    calibrationArea.classList.remove("hidden");
    if (currentCalibUrl !== st.calibration_frame) {
      currentCalibUrl = st.calibration_frame;
      calib.imgLoaded = false;
      calib.pendingClicks = [];
      calib.annotation = { corridor: null, target: null };
      calibImage.src = st.calibration_frame + (st.calibration_frame.includes("?") ? "&" : "?") + "t=" + Date.now();
    }
  } else {
    calibrationArea.classList.add("hidden");
    currentCalibUrl = null;
    calib.imgLoaded = false;
    calib.pendingClicks = [];
    calib.annotation = { corridor: null, target: null };
  }

  updateCalibAnnotationView();
}

function connectLiveWS() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/ws/view`;
  liveWS = new WebSocket(url);
  liveWS.onopen = () => logLive("view ws connected");
  liveWS.onclose = () => {
    logLive("view ws disconnected, retrying...", "error");
    setTimeout(connectLiveWS, 2000);
  };
  liveWS.onerror = () => logLive("view ws error", "error");
  liveWS.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    handleLiveMsg(msg);
  };
}

function handleLiveMsg(msg) {
  if (msg.type === "state") {
    applyState(msg);
  } else if (msg.type === "calibration_frame_ready") {
    logLive("calibration frame received", "ok");
  } else if (msg.type === "shot_uploaded") {
    logLive(`shot ${msg.shot}: ${(msg.bytes / 1024 / 1024).toFixed(1)} MB uploaded, processing...`);
    shotCountEl.textContent = `${msg.shot} shots`;
  } else if (msg.type === "shot_ready") {
    logLive(`shot ${msg.shot}: ready`, "ok");
    if (msg.combined) {
      liveCombined.src = msg.combined + "?t=" + Date.now();
      liveCombined.load();
    }
  } else if (msg.type === "shot_failed") {
    logLive(`shot ${msg.shot}: FAILED (${msg.reason || "unknown"})`, "error");
  } else if (msg.type === "error") {
    logLive(`error: ${msg.msg}`, "error");
  }
}

// ---- session buttons ------------------------------------------------------

startSessionBtn.addEventListener("click", async () => {
  const res = await fetch("/api/session", { method: "POST" });
  if (!res.ok) {
    const body = await res.text();
    logLive(`start session failed: ${body}`, "error");
  } else {
    logLive("session started");
  }
});

endSessionBtn.addEventListener("click", async () => {
  if (!confirm("End session? This clears calibration and shot count.")) return;
  await fetch("/api/session/end", { method: "POST" });
  logLive("session ended");
});

captureFrameBtn.addEventListener("click", () => {
  if (liveWS && liveWS.readyState === WebSocket.OPEN) {
    liveWS.send(JSON.stringify({ type: "request_calibration_frame" }));
    logLive("grabbing fresh calibration frame from phone...");
  }
});

fakeCaptureBtn.addEventListener("click", () => {
  if (!(liveWS && liveWS.readyState === WebSocket.OPEN)) return;
  const t = fakeSourceVideo.currentTime || 0;
  liveWS.send(JSON.stringify({ type: "request_calibration_frame", at_s: t }));
  logLive(`grabbing calibration frame at t=${t.toFixed(3)}s (fake scrub)...`);
});

shotBtn.addEventListener("click", () => {
  if (liveWS && liveWS.readyState === WebSocket.OPEN) {
    liveWS.send(JSON.stringify({ type: "trigger_shot" }));
    logLive("SHOT! triggered");
  }
});

// ---- calibration annotation ----------------------------------------------

calibImage.addEventListener("load", () => {
  calibOverlay.width = calibImage.naturalWidth;
  calibOverlay.height = calibImage.naturalHeight;
  calib.imgLoaded = true;
  redrawCalib();
});

for (const btn of calibModeBtns) {
  btn.addEventListener("click", () => setCalibMode(btn.dataset.mode));
}

function setCalibMode(mode) {
  calib.mode = mode;
  calib.pendingClicks = [];
  for (const b of calibModeBtns) b.classList.toggle("active", b.dataset.mode === mode);
  redrawCalib();
}

calibOverlay.addEventListener("click", (e) => {
  if (!calib.mode || !calib.imgLoaded) return;
  const rect = calibOverlay.getBoundingClientRect();
  const x = Math.round((e.clientX - rect.left) * (calibOverlay.width / rect.width));
  const y = Math.round((e.clientY - rect.top) * (calibOverlay.height / rect.height));
  calib.pendingClicks.push({ x, y });
  handleCalibClicks();
  redrawCalib();
});

function handleCalibClicks() {
  const clicks = calib.pendingClicks;
  if (calib.mode === "corridor" && clicks.length === 2) {
    const ys = [clicks[0].y, clicks[1].y].sort((a, b) => a - b);
    calib.annotation.corridor = { y_top: ys[0], y_bottom: ys[1] };
    calib.pendingClicks = [];
  } else if (calib.mode === "target" && clicks.length === 2) {
    const [c, edge] = clicks;
    const r = Math.round(Math.hypot(edge.x - c.x, edge.y - c.y));
    const prev = calib.annotation.target || {};
    calib.annotation.target = {
      cx: c.x, cy: c.y, r,
      bbox: prev.bbox || null,
    };
    calib.pendingClicks = [];
  } else if (calib.mode === "bbox" && clicks.length === 2) {
    const x0 = Math.min(clicks[0].x, clicks[1].x);
    const y0 = Math.min(clicks[0].y, clicks[1].y);
    const x1 = Math.max(clicks[0].x, clicks[1].x);
    const y1 = Math.max(clicks[0].y, clicks[1].y);
    if (!calib.annotation.target) {
      calib.annotation.target = {
        cx: Math.round((x0 + x1) / 2),
        cy: Math.round((y0 + y1) / 2),
        r: 0,
      };
    }
    calib.annotation.target.bbox = [x0, y0, x1, y1];
    calib.pendingClicks = [];
  }
  updateCalibAnnotationView();
}

function redrawCalib() {
  if (!calibOverlay.width) return;
  ctxCalib.clearRect(0, 0, calibOverlay.width, calibOverlay.height);

  const a = calib.annotation;
  if (a.corridor) {
    ctxCalib.strokeStyle = "#00e5ff";
    ctxCalib.lineWidth = 2;
    ctxCalib.beginPath();
    ctxCalib.moveTo(0, a.corridor.y_top); ctxCalib.lineTo(calibOverlay.width, a.corridor.y_top);
    ctxCalib.moveTo(0, a.corridor.y_bottom); ctxCalib.lineTo(calibOverlay.width, a.corridor.y_bottom);
    ctxCalib.stroke();
  }
  if (a.target) {
    if (a.target.r > 0) {
      ctxCalib.strokeStyle = "#ffa500";
      ctxCalib.lineWidth = 3;
      ctxCalib.beginPath();
      ctxCalib.arc(a.target.cx, a.target.cy, a.target.r, 0, Math.PI * 2);
      ctxCalib.stroke();
      ctxCalib.fillStyle = "#ff4040";
      ctxCalib.beginPath();
      ctxCalib.arc(a.target.cx, a.target.cy, 4, 0, Math.PI * 2);
      ctxCalib.fill();
    }
    if (a.target.bbox) {
      const [x0, y0, x1, y1] = a.target.bbox;
      ctxCalib.strokeStyle = "#ffff00";
      ctxCalib.lineWidth = 2;
      ctxCalib.setLineDash([8, 4]);
      ctxCalib.strokeRect(x0, y0, x1 - x0, y1 - y0);
      ctxCalib.setLineDash([]);
    }
  }
  for (const p of calib.pendingClicks) {
    ctxCalib.fillStyle = "#ff00ff";
    ctxCalib.beginPath();
    ctxCalib.arc(p.x, p.y, 6, 0, Math.PI * 2);
    ctxCalib.fill();
  }
}

function updateCalibAnnotationView() {
  calibAnnotationView.textContent = JSON.stringify(calib.annotation, null, 2);
  saveCalibBtn.disabled = !(calib.annotation.corridor && calib.annotation.target);
}

clearCalibBtn.addEventListener("click", async () => {
  // Wipe the captured frame + saved annotation on the server; the state
  // broadcast will hide the calibration area and the phone overlay clears via
  // the `annotation` message.
  const res = await fetch("/api/session/calibration", { method: "DELETE" });
  if (res.ok) {
    logLive("calibration cleared", "ok");
  } else {
    logLive(`clear failed: ${await res.text()}`, "error");
  }
  calib.annotation = { corridor: null, target: null };
  calib.pendingClicks = [];
  redrawCalib();
  updateCalibAnnotationView();
});

saveCalibBtn.addEventListener("click", async () => {
  if (!calib.annotation.corridor || !calib.annotation.target) return;
  const fd = parseFloat(calibFaceDiameter.value) || 0.40;
  const target = { ...calib.annotation.target, face_diameter_m: fd };
  const body = JSON.stringify({ corridor: calib.annotation.corridor, target });
  const res = await fetch("/api/session/annotation", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body,
  });
  if (res.ok) logLive("calibration saved", "ok");
  else logLive(`save failed: ${await res.text()}`, "error");
});

loadVideos().catch(e => setStatus(`load failed: ${e}`, true));
connectLiveWS();
