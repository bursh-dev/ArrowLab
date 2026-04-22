"use strict";

// ==== Generic status ======================================================

const statusEl = document.getElementById("status");
function setStatus(msg, isError = false) {
  statusEl.textContent = msg;
  statusEl.style.color = isError ? "#ff8a8a" : "#8ab4f8";
  if (msg) setTimeout(() => { if (statusEl.textContent === msg) statusEl.textContent = ""; }, 3500);
}

// ==== Tabs ================================================================

const tabButtons = document.querySelectorAll("#tabs button");
const tabPanels = document.querySelectorAll(".tab-panel");
let currentTab = "session";

function activateTab(name) {
  if (currentTab === name) return;
  currentTab = name;
  for (const b of tabButtons) b.classList.toggle("active", b.dataset.tab === name);
  for (const p of tabPanels) p.classList.toggle("active", p.dataset.tab === name);
  if (name === "shoot") {
    // Canvas size depends on visibility; resize now that it's visible.
    requestAnimationFrame(sizeCanvas);
  }
}
for (const b of tabButtons) b.addEventListener("click", () => activateTab(b.dataset.tab));

// ==== Live session DOM ====================================================

const phoneStatus = document.getElementById("phoneStatus");
const sessionStatus = document.getElementById("sessionStatus");
const rangeStatus = document.getElementById("rangeStatus");
const calibStatus = document.getElementById("calibStatus");
const shotCountEl = document.getElementById("shotCount");

const rangeSt = document.getElementById("rangeSt");
const rangeCp = document.getElementById("rangeCp");
const rangeCa = document.getElementById("rangeCa");
const rangeMass = document.getElementById("rangeMass");
const rangeBow = document.getElementById("rangeBow");
const rangeNotes = document.getElementById("rangeNotes");
const saveRangeBtn = document.getElementById("saveRangeBtn");
const clearRangeBtn = document.getElementById("clearRangeBtn");
const rangeDiagram = document.getElementById("rangeDiagram");
const startSessionBtn = document.getElementById("startSessionBtn");
const endSessionBtn = document.getElementById("endSessionBtn");
const captureFrameBtn = document.getElementById("captureFrameBtn");
const shotBtn = document.getElementById("shotBtn");
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

const lastShot = document.getElementById("lastShot");
const lastShotClip = document.getElementById("lastShotClip");
const lastShotTracked = document.getElementById("lastShotTracked");
const lastShotNum = document.getElementById("lastShotNum");
const shotHistory = document.getElementById("shotHistory");
const shotHistoryList = document.getElementById("shotHistoryList");
let selectedShotIdx = null; // index into shots[] of the currently displayed shot
const statsShotNum = document.getElementById("statsShotNum");
const statSpeed = document.getElementById("statSpeed");
const statTime = document.getElementById("statTime");
const statDist = document.getElementById("statDist");
const statOffset = document.getElementById("statOffset");
const statEnergy = document.getElementById("statEnergy");
const statProcessed = document.getElementById("statProcessed");
const groupCount = document.getElementById("groupCount");
const statExtreme = document.getElementById("statExtreme");
const statMeanR = document.getElementById("statMeanR");

// Latest session range (populated from state). Null when no range is set.
let currentRange = null;

const telemetryCanvas = document.getElementById("telemetryCanvas");
const telemetryCtx = telemetryCanvas.getContext("2d");
const telemetryInfo = document.getElementById("telemetryInfo");
const playAllBtn = document.getElementById("playAllBtn");
const pauseAllBtn = document.getElementById("pauseAllBtn");

let liveWS = null;
let currentCalibUrl = null;

const calib = {
  mode: null,
  pendingClicks: [],
  annotation: { corridor: null, target: null },
  imgLoaded: false,
};

const shots = [];
// Index into shots[] for the most recent shot whose trajectory is NOT yet
// revealed on the static canvas — it only appears while Play is running,
// trail growing with the video. null when all shots are revealed.
let pendingShotIdx = null;

// Last seen state, used to decide tab auto-advance
const lastState = { active: false, has_range: false, has_annotation: false };

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

  rangeStatus.textContent = st.has_range ? "yes" : "no";
  rangeStatus.className = "pill " + (st.has_range ? "ok" : "bad");

  calibStatus.textContent = st.has_annotation ? "yes" : "no";
  calibStatus.className = "pill " + (st.has_annotation ? "ok" : "bad");

  shotCountEl.textContent = `${st.shot_count || 0} shots`;

  startSessionBtn.disabled = !!st.active;
  endSessionBtn.disabled = !st.active;
  captureFrameBtn.disabled = !(st.active && st.phone_connected);
  // Only take over the button when we're NOT mid-shot; during a shot the
  // setShotButton() state machine owns the disabled flag.
  if (!shotInFlight) {
    shotBtn.disabled = !(st.active && st.phone_connected && st.has_annotation);
  }

  // Fake-phone source scrubber (only in calibrate tab content)
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

  // Calibration image + annotation canvas: always live inside the Calibrate tab,
  // but hidden once an annotation is saved (to de-clutter). Re-opening happens
  // by tapping Grab calibration frame again (which fires request_calibration_frame
  // and clears the stored annotation by capturing a fresh frame).
  if (st.active && st.calibration_frame) {
    calibrationArea.classList.remove("hidden");
    if (currentCalibUrl !== st.calibration_frame) {
      currentCalibUrl = st.calibration_frame;
      calib.imgLoaded = false;
      calib.pendingClicks = [];
      calib.annotation = st.annotation
        ? { corridor: st.annotation.corridor || null, target: st.annotation.target || null }
        : { corridor: null, target: null };
      calibImage.src = st.calibration_frame + (st.calibration_frame.includes("?") ? "&" : "?") + "t=" + Date.now();
    } else if (st.annotation && !(calib.annotation.corridor || calib.annotation.target)) {
      // Same image, but we just learned the saved annotation — hydrate and redraw.
      calib.annotation = { corridor: st.annotation.corridor || null, target: st.annotation.target || null };
      if (calib.imgLoaded) {
        redrawCalib();
        updateCalibAnnotationView();
      }
    }
  } else {
    calibrationArea.classList.add("hidden");
    currentCalibUrl = null;
    calib.imgLoaded = false;
    calib.pendingClicks = [];
    calib.annotation = { corridor: null, target: null };
  }

  // Clear shot history + telemetry when session ends
  if (!st.active) {
    shots.length = 0;
    selectedShotIdx = null;
    lastShot.classList.add("hidden");
    shotHistory.classList.add("hidden");
    lastShotClip.removeAttribute("src"); lastShotClip.load();
    lastShotTracked.removeAttribute("src"); lastShotTracked.load();
    telemetryInfo.textContent = "";
    updateStats();
    renderTelemetry();
    renderShotHistory();
  } else if (Array.isArray(st.shots)) {
    // Sync persisted shots from server (reloaded session, or after delete)
    const prevCount = shots.length;
    shots.length = 0;
    for (const sh of st.shots) shots.push(sh);
    renderShotHistory();
    if (shots.length === 0) {
      selectedShotIdx = null;
      lastShot.classList.add("hidden");
      lastShotClip.removeAttribute("src"); lastShotClip.load();
      lastShotTracked.removeAttribute("src"); lastShotTracked.load();
      telemetryInfo.textContent = "";
      updateStats();
      renderTelemetry();
    } else if (prevCount === 0 || selectedShotIdx == null || selectedShotIdx >= shots.length) {
      // Auto-select latest on reload or when selection is gone
      selectShot(shots.length - 1, { play: false });
    }
  }

  currentRange = st.range || null;

  // Populate range form from server state (or keep user's in-progress input)
  if (st.range) {
    rangeSt.value = st.range.shooter_to_target_m ?? "";
    rangeCp.value = st.range.camera_perpendicular_m ?? "";
    rangeCa.value = st.range.camera_along_m ?? "";
    rangeMass.value = st.range.arrow_mass_grains ?? "";
    rangeBow.value = st.range.bow_weight_lbs ?? "";
    rangeNotes.value = st.range.notes ?? "";
  } else if (!lastState.active && st.active) {
    // Fresh session: try to pre-fill from localStorage (last used range)
    try {
      const saved = JSON.parse(localStorage.getItem("arrowlab.range") || "null");
      if (saved) {
        rangeSt.value = saved.shooter_to_target_m ?? "";
        rangeCp.value = saved.camera_perpendicular_m ?? "";
        rangeCa.value = saved.camera_along_m ?? "";
        rangeMass.value = saved.arrow_mass_grains ?? "";
        rangeBow.value = saved.bow_weight_lbs ?? "";
        rangeNotes.value = saved.notes ?? "";
      }
    } catch {}
  }
  renderRangeDiagram();

  // Auto-advance tabs on state transitions
  if (!lastState.active && st.active) activateTab(st.has_range ? "calibrate" : "range");
  else if (lastState.active && !st.active) activateTab("session");
  else if (!lastState.has_range && st.has_range) activateTab("calibrate");
  else if (!lastState.has_annotation && st.has_annotation) activateTab("shoot");
  lastState.active = st.active;
  lastState.has_range = !!st.has_range;
  lastState.has_annotation = !!st.has_annotation;

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
    // Force reload — filename is stable per session so the src URL alone
    // wouldn't change and the browser would keep the cached image.
    calib.imgLoaded = false;
    calib.pendingClicks = [];
    calib.annotation = { corridor: null, target: null };
    currentCalibUrl = msg.url;
    calibrationArea.classList.remove("hidden");
    calibImage.src = msg.url + (msg.url.includes("?") ? "&" : "?") + "t=" + Date.now();
    updateCalibAnnotationView();
  } else if (msg.type === "shot_uploaded") {
    logLive(`shot ${msg.shot}: ${(msg.bytes / 1024 / 1024).toFixed(1)} MB uploaded, processing...`);
    setShotButton("tracking");
  } else if (msg.type === "shot_ready") {
    logLive(`shot ${msg.shot}: ready`, "ok");
    // shots[] is synced by the state broadcast that follows; just select the new one.
    pendingShotIdx = shots.length; // the incoming shot will be appended at this index
    // Defer selection until the state broadcast lands shots[]
    setTimeout(() => {
      const idx = shots.findIndex(sh => sh.shot === msg.shot);
      if (idx >= 0) selectShot(idx, { play: false });
    }, 50);
    setShotButton("idle");
  } else if (msg.type === "shot_failed") {
    logLive(`shot ${msg.shot}: FAILED (${msg.reason || "unknown"})`, "error");
    setShotButton("error");
    setTimeout(() => setShotButton("idle"), 3000);
  } else if (msg.type === "error") {
    logLive(`error: ${msg.msg}`, "error");
  }
}

// ==== Session control =====================================================

startSessionBtn.addEventListener("click", async () => {
  const res = await fetch("/api/session", { method: "POST" });
  if (!res.ok) logLive(`start session failed: ${await res.text()}`, "error");
  else logLive("session started");
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
let shotInFlight = false;

function setShotButton(state) {
  switch (state) {
    case "idle":
      shotBtn.textContent = "SHOT!";
      shotBtn.style.background = "";
      shotInFlight = false;
      break;
    case "recording":
      shotBtn.textContent = "⏺ RECORDING…";
      shotBtn.style.background = "#b84040";
      shotInFlight = true;
      break;
    case "uploading":
      shotBtn.textContent = "⬆ UPLOADING…";
      shotBtn.style.background = "#b0802e";
      break;
    case "tracking":
      shotBtn.textContent = "⚙ TRACKING…";
      shotBtn.style.background = "#406080";
      break;
    case "error":
      shotBtn.textContent = "✖ FAILED — try again";
      shotBtn.style.background = "#803030";
      shotInFlight = false;
      break;
  }
  shotBtn.disabled = shotInFlight;
}

let countdownAbortToken = 0;

function startShotCountdown() {
  if (shotInFlight) return;
  if (!(liveWS && liveWS.readyState === WebSocket.OPEN)) return;
  shotInFlight = true;
  const my = ++countdownAbortToken;
  shotBtn.disabled = false; // keep enabled so user can abort on click
  shotBtn.style.background = "#406080";

  // Phase 1: 5s silent "walk away" buffer — pre-ring-buffer so operator motion doesn't leak in
  let walkAway = 5;
  const walkTick = () => {
    if (my !== countdownAbortToken) return;
    if (walkAway > 0) {
      shotBtn.textContent = `walk away… ${walkAway}`;
      walkAway--;
      setTimeout(walkTick, 1000);
    } else {
      countTick();
    }
  };

  // Phase 2: 5→1 countdown
  let count = 5;
  const countTick = () => {
    if (my !== countdownAbortToken) return;
    if (count > 0) {
      shotBtn.textContent = `${count}…`;
      count--;
      setTimeout(countTick, 1000);
    } else {
      shotBtn.textContent = "🏹 SHOOT!";
      shotBtn.style.background = "#c04040";
      setTimeout(() => {
        if (my !== countdownAbortToken) return;
        liveWS.send(JSON.stringify({ type: "trigger_shot" }));
        logLive("SHOT! triggered");
        setShotButton("recording");
      }, 1500);
    }
  };

  walkTick();
}

function abortCountdown() {
  countdownAbortToken++;
  setShotButton("idle");
}

shotBtn.addEventListener("click", () => {
  if (shotInFlight) {
    abortCountdown();
    return;
  }
  startShotCountdown();
});

// ==== Calibration annotation canvas ======================================

calibImage.addEventListener("load", () => {
  calibOverlay.width = calibImage.naturalWidth;
  calibOverlay.height = calibImage.naturalHeight;
  calib.imgLoaded = true;
  redrawCalib();
  updateCalibAnnotationView();
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
    calib.annotation.target = { cx: c.x, cy: c.y, r, bbox: prev.bbox || null };
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
  const res = await fetch("/api/session/calibration", { method: "DELETE" });
  if (res.ok) logLive("calibration cleared", "ok");
  else logLive(`clear failed: ${await res.text()}`, "error");
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

// ==== Telemetry canvas ====================================================

function linearFit(xs, ys) {
  const n = xs.length;
  if (n < 2) return null;
  const mx = xs.reduce((a, b) => a + b, 0) / n;
  const my = ys.reduce((a, b) => a + b, 0) / n;
  let num = 0, den = 0;
  for (let i = 0; i < n; i++) {
    num += (xs[i] - mx) * (ys[i] - my);
    den += (xs[i] - mx) * (xs[i] - mx);
  }
  if (den <= 0) return null;
  const slope = num / den;
  return { slope, intercept: my - slope * mx };
}

function shotColor(i) {
  const hue = (i * 55) % 360;
  return `hsl(${hue}, 80%, 60%)`;
}

function getViewBox() {
  const latest = shots[shots.length - 1]?.trajectory;
  const imgW = latest?.width || 1920;
  const imgH = latest?.height || 1080;
  const t = latest?.annotation?.target;

  if (t && t.cx != null && t.cy != null && t.r) {
    // Span the actual flight horizontally so the arrow is on-canvas the whole
    // time (same pacing as the videos), but keep vertical zoom tight so small
    // differences between shots stand out.
    const r = t.r;
    let leftExtent = r * 20;
    for (const shot of shots) {
      const dets = shot.trajectory?.detections;
      if (!dets || dets.length === 0) continue;
      const firstX = dets[0].x;
      leftExtent = Math.max(leftExtent, (t.cx - firstX) + r * 2);
    }
    const right = r * 4;
    const vert = r * 5;
    return {
      x0: Math.max(0, t.cx - leftExtent),
      y0: Math.max(0, t.cy - vert),
      x1: Math.min(imgW, t.cx + right),
      y1: Math.min(imgH, t.cy + vert),
    };
  }

  const corridor = latest?.annotation?.corridor;
  if (corridor) {
    const band = corridor.y_bottom - corridor.y_top;
    const pad = Math.round(band * 0.25);
    return {
      x0: 0,
      y0: Math.max(0, corridor.y_top - pad),
      x1: imgW,
      y1: Math.min(imgH, corridor.y_bottom + pad),
    };
  }
  return { x0: 0, y0: 0, x1: imgW, y1: imgH };
}

function sizeCanvas() {
  const parentW = telemetryCanvas.parentElement.clientWidth || 800;
  const vb = getViewBox();
  const aspect = (vb.x1 - vb.x0) / Math.max(1, vb.y1 - vb.y0);
  const cssW = parentW;
  const cssH = Math.max(120, Math.min(360, cssW / aspect));
  telemetryCanvas.style.height = `${Math.round(cssH)}px`;
  telemetryCanvas.width = Math.round(cssW);
  telemetryCanvas.height = Math.round(cssH);
  renderTelemetry();
}
window.addEventListener("resize", sizeCanvas);

function shotFit(shot) {
  const dets = shot.trajectory?.detections || [];
  if (dets.length < 2) return null;
  const frames = dets.map(d => d.frame);
  const fx = linearFit(frames, dets.map(d => d.x));
  const fy = linearFit(frames, dets.map(d => d.y));
  if (!fx || !fy) return null;
  const tx = shot.trajectory?.annotation?.target?.cx;
  const f0 = frames[0];
  const fEnd = frames[frames.length - 1];
  const startX = fx.slope * f0 + fx.intercept;
  const startY = fy.slope * f0 + fy.intercept;
  let hitX, hitY, fHit;
  if (tx != null && Math.abs(fx.slope) > 1e-6) {
    fHit = (tx - fx.intercept) / fx.slope;
    hitX = tx;
    hitY = fy.slope * fHit + fy.intercept;
  } else {
    fHit = fEnd;
    hitX = fx.slope * fHit + fx.intercept;
    hitY = fy.slope * fHit + fy.intercept;
  }
  return { fx, fy, f0, fEnd, fHit, startX, startY, hitX, hitY };
}

function renderTelemetry(currentTime = null) {
  const w = telemetryCanvas.width;
  const h = telemetryCanvas.height;
  telemetryCtx.fillStyle = "#0a0a0e";
  telemetryCtx.fillRect(0, 0, w, h);

  if (shots.length === 0) return;

  const latest = shots[shots.length - 1].trajectory;
  const ann = latest.annotation || {};
  const vb = getViewBox();
  const vbW = vb.x1 - vb.x0;
  const vbH = vb.y1 - vb.y0;
  const sx = w / vbW;
  const sy = h / vbH;
  const sAvg = (sx + sy) / 2;
  const mapX = (x) => (x - vb.x0) * sx;
  const mapY = (y) => (y - vb.y0) * sy;

  // Corridor
  if (ann.corridor) {
    telemetryCtx.strokeStyle = "#444455";
    telemetryCtx.lineWidth = 1;
    telemetryCtx.beginPath();
    telemetryCtx.moveTo(0, mapY(ann.corridor.y_top));
    telemetryCtx.lineTo(w, mapY(ann.corridor.y_top));
    telemetryCtx.stroke();
    telemetryCtx.beginPath();
    telemetryCtx.moveTo(0, mapY(ann.corridor.y_bottom));
    telemetryCtx.lineTo(w, mapY(ann.corridor.y_bottom));
    telemetryCtx.stroke();
  }

  // Target bbox (from annotation)
  const t = ann.target;
  if (t?.bbox && t.bbox.length === 4) {
    const [bx0, by0, bx1, by1] = t.bbox;
    telemetryCtx.strokeStyle = "#6a8fbf";
    telemetryCtx.lineWidth = 1;
    telemetryCtx.setLineDash([4, 3]);
    telemetryCtx.strokeRect(
      mapX(bx0), mapY(by0),
      (bx1 - bx0) * sx, (by1 - by0) * sy,
    );
    telemetryCtx.setLineDash([]);
  }

  // Target face
  if (t && t.r && t.cx != null && t.cy != null) {
    const cx = mapX(t.cx), cy = mapY(t.cy), rBase = t.r * sAvg;
    const rings = [
      [rBase, "#ffffff"],
      [rBase * 0.75, "#ff4040"],
      [rBase * 0.5, "#ffcc00"],
      [rBase * 0.25, "#ffff99"],
    ];
    for (const [rr, col] of rings) {
      telemetryCtx.strokeStyle = col;
      telemetryCtx.lineWidth = 1.5;
      telemetryCtx.beginPath();
      telemetryCtx.arc(cx, cy, rr, 0, Math.PI * 2);
      telemetryCtx.stroke();
    }
    telemetryCtx.fillStyle = "#ffffff";
    telemetryCtx.beginPath();
    telemetryCtx.arc(cx, cy, 2, 0, Math.PI * 2);
    telemetryCtx.fill();
  }

  // Per-shot static fit lines — skip the pending (unrevealed) one.
  // Newest = fully opaque, each older step fades, after 5 oldest are gone.
  const FADE_WINDOW = 5;
  const newestIdx = shots.length - 1;
  shots.forEach((shot, i) => {
    if (i === pendingShotIdx) return;
    const age = newestIdx - i; // 0 = newest
    if (age >= FADE_WINDOW) return;
    const alpha = 1 - age / FADE_WINDOW; // 1.0, 0.8, 0.6, 0.4, 0.2
    const f = shotFit(shot);
    if (!f) return;
    const color = shotColor(i);
    telemetryCtx.save();
    telemetryCtx.globalAlpha = alpha;
    telemetryCtx.strokeStyle = color;
    telemetryCtx.lineWidth = 1.5;
    telemetryCtx.lineCap = "round";
    telemetryCtx.beginPath();
    telemetryCtx.moveTo(mapX(f.startX), mapY(f.startY));
    telemetryCtx.lineTo(mapX(f.hitX), mapY(f.hitY));
    telemetryCtx.stroke();

    telemetryCtx.fillStyle = color;
    telemetryCtx.beginPath();
    telemetryCtx.arc(mapX(f.hitX), mapY(f.hitY), 3.5, 0, Math.PI * 2);
    telemetryCtx.fill();
    telemetryCtx.font = "600 11px ui-monospace, monospace";
    telemetryCtx.fillText(`#${shot.shot}`, mapX(f.hitX) + 6, mapY(f.hitY) - 6);
    telemetryCtx.restore();
  });

  // Pending shot: trail grows with video time; no trail until Play pressed.
  if (pendingShotIdx != null && currentTime != null) {
    const shot = shots[pendingShotIdx];
    const fit = shotFit(shot);
    const traj = shot.trajectory;
    if (fit && traj?.fps) {
      const color = shotColor(pendingShotIdx);
      const clipStart = traj.clip_start_frame ?? fit.f0;
      const trimOffset = Number.isFinite(shot.trim_offset_s) ? shot.trim_offset_s : 0;
      const frame = clipStart + (currentTime + trimOffset) * traj.fps;
      const f = Math.max(fit.f0, Math.min(fit.fHit, frame));
      const ax = fit.fx.slope * f + fit.fx.intercept;
      const ay = fit.fy.slope * f + fit.fy.intercept;
      // Growing trail from start to current arrow position
      telemetryCtx.strokeStyle = color;
      telemetryCtx.lineWidth = 1.5;
      telemetryCtx.lineCap = "round";
      telemetryCtx.beginPath();
      telemetryCtx.moveTo(mapX(fit.startX), mapY(fit.startY));
      telemetryCtx.lineTo(mapX(ax), mapY(ay));
      telemetryCtx.stroke();
      // Moving arrow marker
      if (frame >= fit.f0 && frame <= fit.fHit) {
        telemetryCtx.fillStyle = "#ffffff";
        telemetryCtx.strokeStyle = "#ffffff";
        telemetryCtx.lineWidth = 2;
        telemetryCtx.beginPath();
        telemetryCtx.arc(mapX(ax), mapY(ay), 6, 0, Math.PI * 2);
        telemetryCtx.stroke();
        telemetryCtx.beginPath();
        telemetryCtx.arc(mapX(ax), mapY(ay), 2, 0, Math.PI * 2);
        telemetryCtx.fill();
      }
    }
  }
}

// ==== Shot history =======================================================

function renderShotHistory() {
  shotHistoryList.innerHTML = "";
  if (shots.length === 0) {
    shotHistory.classList.add("hidden");
    return;
  }
  shotHistory.classList.remove("hidden");
  shots.forEach((sh, i) => {
    const chip = document.createElement("span");
    chip.className = "shot-chip" + (i === selectedShotIdx ? " active" : "");
    chip.title = "click to view";
    chip.innerHTML = `#${sh.shot}<button class="del" title="delete">✕</button>`;
    chip.addEventListener("click", (e) => {
      if (e.target.classList.contains("del")) return;
      selectShot(i, { play: false });
    });
    chip.querySelector(".del").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete shot #${sh.shot}?`)) return;
      const res = await fetch(`/api/session/shot/${sh.shot}`, { method: "DELETE" });
      if (!res.ok) logLive(`delete shot ${sh.shot} failed: ${await res.text()}`, "error");
      else logLive(`deleted shot ${sh.shot}`);
    });
    shotHistoryList.appendChild(chip);
  });
}

function selectShot(idx, { play = false } = {}) {
  if (idx < 0 || idx >= shots.length) return;
  selectedShotIdx = idx;
  const sh = shots[idx];
  lastShotNum.textContent = sh.shot;
  if (sh.clip_url) lastShotClip.src = sh.clip_url + "?t=" + Date.now();
  if (sh.tracked_url) lastShotTracked.src = sh.tracked_url + "?t=" + Date.now();
  lastShot.classList.remove("hidden");
  playAllBtn.disabled = false;
  pauseAllBtn.disabled = false;
  pendingShotIdx = idx;
  telemetryInfo.textContent = `shot ${sh.shot} of ${shots.length} (press Play to reveal)`;
  renderShotHistory();
  updateStats();
  sizeCanvas();
  if (play) startSyncPlay();
}

// ==== Synced play ========================================================

let playRaf = 0;
function startSyncPlay() {
  if (shots.length === 0) return;
  const idx = selectedShotIdx != null ? selectedShotIdx : shots.length - 1;
  // Re-arm the selected shot as pending so its trajectory hides and animates
  // fresh on every Play press.
  pendingShotIdx = idx;
  telemetryInfo.textContent = `shot ${shots[idx].shot} (playing 0.0625×)`;
  const shot = shots[idx] || {};
  const startS = Number.isFinite(shot.start_s) ? shot.start_s : 0;
  try { lastShotClip.currentTime = startS; } catch {}
  try { lastShotTracked.currentTime = startS; } catch {}
  lastShotClip.playbackRate = 0.0625;
  lastShotTracked.playbackRate = 0.0625;
  Promise.all([lastShotClip.play(), lastShotTracked.play()]).catch(() => {});
  if (playRaf) cancelAnimationFrame(playRaf);
  const loop = () => {
    renderTelemetry(lastShotClip.currentTime);
    if (!lastShotClip.paused && !lastShotClip.ended) {
      playRaf = requestAnimationFrame(loop);
    } else {
      playRaf = 0;
      renderTelemetry();
    }
  };
  playRaf = requestAnimationFrame(loop);
}
function stopSyncPlay() {
  lastShotClip.pause();
  lastShotTracked.pause();
  if (playRaf) { cancelAnimationFrame(playRaf); playRaf = 0; }
  renderTelemetry();
}
playAllBtn.addEventListener("click", startSyncPlay);
pauseAllBtn.addEventListener("click", stopSyncPlay);
lastShotClip.addEventListener("ended", () => {
  if (playRaf) { cancelAnimationFrame(playRaf); playRaf = 0; }
  // Playback done — promote pending shot to the static view
  pendingShotIdx = null;
  telemetryInfo.textContent = `${shots.length} shot${shots.length === 1 ? "" : "s"}`;
  renderTelemetry();
});

// ==== Shot stats =========================================================

function fmt(n, digits = 1, suffix = "") {
  if (n == null || !isFinite(n)) return "—";
  return n.toFixed(digits) + suffix;
}

function computeShotStats(shot) {
  const traj = shot.trajectory;
  const t = traj?.annotation?.target;
  const dets = traj?.detections || [];
  if (!t || !t.r || !t.face_diameter_m || dets.length < 2) return null;
  const fps = traj.fps || 30;
  const mPerPx = t.face_diameter_m / (2 * t.r);
  const first = dets[0];
  const last = dets[dets.length - 1];
  const pxDx = Math.hypot(last.x - first.x, last.y - first.y);
  const distanceM = pxDx * mPerPx;
  const durSec = (last.frame - first.frame) / fps;
  const speedMs = durSec > 0 ? distanceM / durSec : null;
  const fit = shotFit(shot);
  let hitOffsetCm = null;
  if (fit) {
    const dxPx = fit.hitX - t.cx;
    const dyPx = fit.hitY - t.cy;
    hitOffsetCm = Math.hypot(dxPx, dyPx) * mPerPx * 100;
  }
  let keJ = null, keFtLbs = null;
  const mass = currentRange?.arrow_mass_grains;
  if (speedMs != null && mass) {
    const massKg = mass * 0.0000647989;
    keJ = 0.5 * massKg * speedMs * speedMs;
    keFtLbs = keJ / 1.3558;
  }
  return { speedMs, durSec, distanceM, hitOffsetCm, keJ, keFtLbs };
}

function computeGroup(shots) {
  const pts = [];
  for (const shot of shots) {
    const t = shot.trajectory?.annotation?.target;
    const fit = shotFit(shot);
    if (!t || !t.r || !t.face_diameter_m || !fit) continue;
    const mPerPx = t.face_diameter_m / (2 * t.r);
    pts.push({
      x: (fit.hitX - t.cx) * mPerPx * 100,
      y: (fit.hitY - t.cy) * mPerPx * 100,
    });
  }
  if (pts.length < 2) return { count: pts.length, extremeCm: null, meanRCm: null };
  let maxD = 0;
  for (let i = 0; i < pts.length; i++) {
    for (let j = i + 1; j < pts.length; j++) {
      const d = Math.hypot(pts[i].x - pts[j].x, pts[i].y - pts[j].y);
      if (d > maxD) maxD = d;
    }
  }
  const cx = pts.reduce((s, p) => s + p.x, 0) / pts.length;
  const cy = pts.reduce((s, p) => s + p.y, 0) / pts.length;
  const meanR = pts.reduce((s, p) => s + Math.hypot(p.x - cx, p.y - cy), 0) / pts.length;
  return { count: pts.length, extremeCm: maxD, meanRCm: meanR };
}

function updateStats() {
  const idx = selectedShotIdx != null ? selectedShotIdx : shots.length - 1;
  const shot = shots[idx];
  if (!shot) {
    statsShotNum.textContent = "?";
    statSpeed.textContent = statTime.textContent = statDist.textContent =
      statOffset.textContent = statEnergy.textContent = statProcessed.textContent = "—";
    groupCount.textContent = "0";
    statExtreme.textContent = statMeanR.textContent = "—";
    return;
  }
  statsShotNum.textContent = shot.shot;
  statProcessed.textContent = Number.isFinite(shot.processing_s) ? `${shot.processing_s.toFixed(1)} s` : "—";
  const s = computeShotStats(shot);
  if (s) {
    statSpeed.textContent = fmt(s.speedMs, 1, " m/s");
    statTime.textContent = fmt(s.durSec * 1000, 0, " ms");
    statDist.textContent = fmt(s.distanceM, 2, " m");
    statOffset.textContent = fmt(s.hitOffsetCm, 1, " cm");
    statEnergy.textContent = s.keJ != null
      ? `${s.keJ.toFixed(1)} J / ${s.keFtLbs.toFixed(1)} ft·lbs`
      : "— (need arrow mass)";
  }
  const g = computeGroup(shots);
  groupCount.textContent = g.count;
  statExtreme.textContent = g.extremeCm != null ? fmt(g.extremeCm, 1, " cm") : "—";
  statMeanR.textContent = g.meanRCm != null ? fmt(g.meanRCm, 1, " cm") : "—";
}

// ==== Range (shooting-range geometry) =====================================

function renderRangeDiagram() {
  const svg = rangeDiagram;
  const vbW = 800, vbH = 360;
  const pad = 50;
  const stT = parseFloat(rangeSt.value);
  const cp = parseFloat(rangeCp.value);
  const ca = parseFloat(rangeCa.value);
  if (!isFinite(stT) || stT <= 0) {
    svg.innerHTML =
      `<text x="${vbW / 2}" y="${vbH / 2}" fill="#666" text-anchor="middle" font-size="14">` +
      `enter shooter↔target distance to see layout</text>`;
    return;
  }
  // World frame: shooter at (0, 0), target at (stT, 0), camera at (ca, -cp)
  const camY = isFinite(cp) && cp > 0 ? -cp : 0;
  const camX = isFinite(ca) ? ca : stT / 2;
  const wxMin = Math.min(-0.5, camX - 0.5);
  const wxMax = Math.max(stT + 0.5, camX + 0.5);
  const wyMin = Math.min(camY - 0.5, -1);
  const wyMax = 1.2;
  const scaleX = (vbW - 2 * pad) / (wxMax - wxMin);
  const scaleY = (vbH - 2 * pad) / (wyMax - wyMin);
  const s = Math.min(scaleX, scaleY);
  const mapX = (x) => pad + (x - wxMin) * s;
  const mapY = (y) => vbH - pad - (y - wyMin) * s;
  const parts = [];
  parts.push(
    `<line x1="${mapX(0)}" y1="${mapY(0)}" x2="${mapX(stT)}" y2="${mapY(0)}" ` +
    `stroke="#666" stroke-width="2" stroke-dasharray="6,4" />`
  );
  // shooter
  parts.push(
    `<circle cx="${mapX(0)}" cy="${mapY(0)}" r="9" fill="#4080ff" />` +
    `<text x="${mapX(0)}" y="${mapY(0) + 24}" fill="#ddd" font-size="12" text-anchor="middle">shooter</text>`
  );
  // target
  parts.push(
    `<rect x="${mapX(stT) - 14}" y="${mapY(0) - 14}" width="28" height="28" ` +
    `fill="none" stroke="#ff6060" stroke-width="2" />` +
    `<text x="${mapX(stT)}" y="${mapY(0) + 28}" fill="#ddd" font-size="12" text-anchor="middle">target</text>`
  );
  // shooter-target distance label
  parts.push(
    `<text x="${(mapX(0) + mapX(stT)) / 2}" y="${mapY(0) - 10}" ` +
    `fill="#ccc" font-size="11" text-anchor="middle">${stT.toFixed(2)} m</text>`
  );
  // camera + perpendicular
  if (isFinite(cp) && cp > 0 && isFinite(ca)) {
    const cX = mapX(ca);
    const cY = mapY(-cp);
    const footX = mapX(ca);
    const footY = mapY(0);
    parts.push(
      `<line x1="${cX}" y1="${cY}" x2="${footX}" y2="${footY}" ` +
      `stroke="#80ff80" stroke-width="1" stroke-dasharray="4,4" />`
    );
    parts.push(
      `<circle cx="${cX}" cy="${cY}" r="8" fill="#80ff80" />` +
      `<text x="${cX}" y="${cY - 12}" fill="#ddd" font-size="12" text-anchor="middle">camera</text>`
    );
    parts.push(
      `<text x="${cX + 10}" y="${(cY + footY) / 2}" fill="#9f9" font-size="11">${cp.toFixed(2)} m</text>`
    );
    // along-line label (shooter → camera foot)
    if (Math.abs(ca) > 0.1) {
      parts.push(
        `<text x="${(mapX(0) + footX) / 2}" y="${mapY(0) + 42}" ` +
        `fill="#88f" font-size="10" text-anchor="middle">${ca.toFixed(2)} m</text>`
      );
      parts.push(
        `<line x1="${mapX(0)}" y1="${mapY(0) + 30}" x2="${footX}" y2="${mapY(0) + 30}" ` +
        `stroke="#88f" stroke-width="1" />`
      );
    }
  }
  svg.innerHTML = parts.join("");
}

for (const inp of [rangeSt, rangeCp, rangeCa]) {
  inp.addEventListener("input", renderRangeDiagram);
}

saveRangeBtn.addEventListener("click", async () => {
  const stT = parseFloat(rangeSt.value);
  const cp = parseFloat(rangeCp.value);
  const ca = parseFloat(rangeCa.value);
  if (!(isFinite(stT) && stT > 0)) {
    logLive("Shooter↔Target distance is required", "error");
    return;
  }
  if (!(isFinite(cp) && cp > 0)) {
    logLive("Camera perpendicular distance is required", "error");
    return;
  }
  if (!isFinite(ca)) {
    logLive("Camera along-line distance is required", "error");
    return;
  }
  const body = {
    shooter_to_target_m: stT,
    camera_perpendicular_m: cp,
    camera_along_m: ca,
  };
  const mass = parseFloat(rangeMass.value);
  if (isFinite(mass) && mass > 0) body.arrow_mass_grains = mass;
  const bow = parseFloat(rangeBow.value);
  if (isFinite(bow) && bow > 0) body.bow_weight_lbs = bow;
  const notes = rangeNotes.value.trim();
  if (notes) body.notes = notes;
  const res = await fetch("/api/session/range", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.ok) {
    logLive("range saved", "ok");
    try { localStorage.setItem("arrowlab.range", JSON.stringify(body)); } catch {}
  } else {
    logLive(`range save failed: ${await res.text()}`, "error");
  }
});

clearRangeBtn.addEventListener("click", async () => {
  const res = await fetch("/api/session/range", { method: "DELETE" });
  if (res.ok) logLive("range cleared", "ok");
  else logLive(`range clear failed: ${await res.text()}`, "error");
});

// ==== Boot ================================================================

connectLiveWS();
requestAnimationFrame(sizeCanvas);
renderRangeDiagram();
