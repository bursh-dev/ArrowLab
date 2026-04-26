"use strict";

// ==== Generic status ======================================================

const statusEl = document.getElementById("status");
function setStatus(msg, isError = false) {
  statusEl.textContent = msg;
  statusEl.style.color = isError ? "#ff8a8a" : "#8ab4f8";
  if (msg) setTimeout(() => { if (statusEl.textContent === msg) statusEl.textContent = ""; }, 3500);
}

// ==== Tabs ================================================================

const tabButtons = document.querySelectorAll("#tabs button[data-tab]");
const tabPanels = document.querySelectorAll(".tab-panel");
let currentTab = "session";

const shootTabActions = document.getElementById("shootTabActions");

// ==== Sound calibration ==================================================

const soundShotsList = document.getElementById("soundShotsList");
const reloadSoundShotsBtn = document.getElementById("reloadSoundShotsBtn");
const saveSoundTemplateBtn = document.getElementById("saveSoundTemplateBtn");
const soundTemplateStatus = document.getElementById("soundTemplateStatus");

// Map<shot:number, {release:boolean, impact:boolean}>
const soundAccepts = new Map();

async function reloadSoundShots() {
  const res = await fetch("/api/calibration-sound/shots");
  if (!res.ok) { logLive("sound shots fetch failed: " + res.status, "error"); return; }
  const { shots: list } = await res.json();
  soundShotsList.innerHTML = "";
  if (!list || list.length === 0) {
    const empty = document.createElement("div");
    empty.className = "shoot-hint";
    empty.textContent = "no shots with audio events yet — fire a few armed shots first";
    soundShotsList.appendChild(empty);
  }
  for (const sh of list) {
    const row = document.createElement("div");
    row.className = "sound-shot-row";
    row.innerHTML = `
      <span class="shot-label">#${sh.shot}</span>
      <div class="sound-snippet">
        <span>release</span>
        <audio controls preload="none" src="${sh.release_snippet_url}"></audio>
        <button class="accept-btn" data-kind="release" data-shot="${sh.shot}">✓ accept</button>
      </div>
      <div class="sound-snippet">
        <span>impact</span>
        <audio controls preload="none" src="${sh.impact_snippet_url}"></audio>
        <button class="accept-btn" data-kind="impact" data-shot="${sh.shot}">✓ accept</button>
      </div>
    `;
    soundShotsList.appendChild(row);
  }
  // Wire up accept toggles
  soundShotsList.querySelectorAll(".accept-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const shot = parseInt(btn.dataset.shot, 10);
      const kind = btn.dataset.kind;
      const state = soundAccepts.get(shot) || { release: false, impact: false };
      state[kind] = !state[kind];
      soundAccepts.set(shot, state);
      btn.classList.toggle("on", state[kind]);
      updateSoundSaveButton();
    });
  });
  updateSoundSaveButton();
}

function updateSoundSaveButton() {
  let r = 0, i = 0;
  for (const v of soundAccepts.values()) {
    if (v.release) r++;
    if (v.impact) i++;
  }
  saveSoundTemplateBtn.textContent = `Save template (${r} release / ${i} impact)`;
  saveSoundTemplateBtn.disabled = !(r >= 1 && i >= 1);
}

reloadSoundShotsBtn.addEventListener("click", reloadSoundShots);
saveSoundTemplateBtn.addEventListener("click", async () => {
  const accepted_release_shots = [];
  const accepted_impact_shots = [];
  for (const [shot, v] of soundAccepts.entries()) {
    if (v.release) accepted_release_shots.push(shot);
    if (v.impact) accepted_impact_shots.push(shot);
  }
  const res = await fetch("/api/calibration-sound/template", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ accepted_release_shots, accepted_impact_shots }),
  });
  if (!res.ok) {
    soundTemplateStatus.textContent = "save failed: " + await res.text();
    return;
  }
  const j = await res.json();
  soundTemplateStatus.textContent =
    `saved: ${j.release_count} release / ${j.impact_count} impact, ${j.template_bins} bins`;
  logLive(`sound template saved (${j.release_count}R/${j.impact_count}I)`, "ok");
});

// ===== Calibration recording (30 s capture + per-peak labeling) =====
const calibRecordStartBtn = document.getElementById("calibRecordStartBtn");
const calibRecordStatus = document.getElementById("calibRecordStatus");
const calibRecordBuildBtn = document.getElementById("calibRecordBuildBtn");
const calibRecordPeaks = document.getElementById("calibRecordPeaks");

let calibRecordingState = "idle"; // "idle" | "recording" | "ready"
let calibRecordCountdownTimer = null;

calibRecordStartBtn.addEventListener("click", async () => {
  if (calibRecordingState === "recording") return;
  try {
    const res = await fetch("/api/calibration-record/start?duration=30", { method: "POST" });
    if (!res.ok) {
      calibRecordStatus.textContent = "start failed: " + await res.text();
      return;
    }
    setCalibRecording(30);
  } catch (e) {
    calibRecordStatus.textContent = "start error: " + e.message;
  }
});

function setCalibRecording(durationS) {
  calibRecordingState = "recording";
  calibRecordStartBtn.classList.add("recording");
  calibRecordStartBtn.disabled = true;
  calibRecordPeaks.innerHTML = "";
  let remaining = Math.round(durationS);
  const tick = () => {
    if (calibRecordingState !== "recording") return;
    calibRecordStatus.textContent = `recording… ${remaining}s left — fire 5 arrows`;
    remaining--;
    if (remaining < 0) {
      calibRecordStatus.textContent = "uploading & detecting peaks…";
      return;
    }
    calibRecordCountdownTimer = setTimeout(tick, 1000);
  };
  tick();
}

function clearCalibRecording() {
  calibRecordingState = "idle";
  calibRecordStartBtn.classList.remove("recording");
  calibRecordStartBtn.disabled = false;
  if (calibRecordCountdownTimer) { clearTimeout(calibRecordCountdownTimer); calibRecordCountdownTimer = null; }
}

async function loadCalibRecord() {
  try {
    const res = await fetch("/api/calibration-record");
    if (!res.ok) return;
    const j = await res.json();
    if (!j.exists) {
      calibRecordPeaks.innerHTML = "";
      calibRecordStatus.textContent = "no recording yet";
      calibRecordBuildBtn.disabled = true;
      return;
    }
    renderCalibPeaks(j);
  } catch (e) {
    calibRecordStatus.textContent = "load error: " + e.message;
  }
}

function renderCalibPeaks(data) {
  clearCalibRecording();
  calibRecordingState = "ready";
  calibRecordPeaks.innerHTML = "";
  const labels = data.labels || {};
  data.peaks.forEach((p, idx) => {
    const row = document.createElement("div");
    row.className = "calib-peak";
    const lbl = labels[String(idx)];
    if (lbl) row.classList.add(lbl);
    row.innerHTML = `
      <div class="calib-peak-head">
        <span>#${idx + 1} · t=${p.t_s.toFixed(2)}s</span>
        <span>amp ${p.amplitude.toFixed(3)}</span>
      </div>
      <audio controls preload="metadata" src="/api/calibration-record/snippet/${idx}.wav"></audio>
      <div class="calib-peak-buttons">
        <button data-label="release" class="rel ${lbl === 'release' ? 'active' : ''}">Release</button>
        <button data-label="impact"  class="imp ${lbl === 'impact'  ? 'active' : ''}">Impact</button>
        <button data-label="noise"   class="noi ${lbl === 'noise'   ? 'active' : ''}">Noise</button>
        <button data-label="">✕</button>
      </div>
    `;
    row.querySelectorAll(".calib-peak-buttons button").forEach(b => {
      b.addEventListener("click", () => labelCalibPeak(idx, b.dataset.label, row));
    });
    calibRecordPeaks.appendChild(row);
  });
  refreshCalibCounts(labels);
}

async function labelCalibPeak(peakIdx, label, row) {
  // Optimistic update
  row.classList.remove("release", "impact", "noise");
  if (label) row.classList.add(label);
  row.querySelectorAll(".calib-peak-buttons button").forEach(b => {
    b.classList.toggle("active", b.dataset.label === label && label !== "");
  });
  try {
    const body = { labels: { [String(peakIdx)]: label || null } };
    const res = await fetch("/api/calibration-record/labels", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      calibRecordStatus.textContent = "label save failed";
      return;
    }
    const j = await res.json();
    refreshCalibCounts(null, j.counts);
  } catch (e) {
    calibRecordStatus.textContent = "label error: " + e.message;
  }
}

function refreshCalibCounts(labelsMap, counts) {
  if (!counts && labelsMap) {
    counts = { release: 0, impact: 0, noise: 0 };
    Object.values(labelsMap).forEach(v => { if (counts[v] != null) counts[v]++; });
  }
  if (!counts) counts = { release: 0, impact: 0, noise: 0 };
  calibRecordStatus.textContent =
    `${counts.release} release · ${counts.impact} impact · ${counts.noise} noise`;
  calibRecordBuildBtn.disabled = !(counts.release >= 2 && counts.impact >= 2);
}

calibRecordBuildBtn.addEventListener("click", async () => {
  calibRecordBuildBtn.disabled = true;
  calibRecordStatus.textContent = "building template…";
  try {
    const res = await fetch("/api/calibration-record/build-template", { method: "POST" });
    if (!res.ok) {
      calibRecordStatus.textContent = "build failed: " + await res.text();
      calibRecordBuildBtn.disabled = false;
      return;
    }
    const j = await res.json();
    calibRecordStatus.textContent = `template saved · ${j.release_count}R/${j.impact_count}I (${j.template_bins} bins)`;
    logLive(`calibration template built (${j.release_count}R/${j.impact_count}I)`, "ok");
  } catch (e) {
    calibRecordStatus.textContent = "build error: " + e.message;
    calibRecordBuildBtn.disabled = false;
  }
});

// Try loading any existing recording on page load
loadCalibRecord();

// ===== Sound-match threshold panel =====
const soundThresholdSlider = document.getElementById("soundThresholdSlider");
const soundThresholdValue = document.getElementById("soundThresholdValue");
const soundThresholdSaveBtn = document.getElementById("soundThresholdSaveBtn");
const soundThresholdStatus = document.getElementById("soundThresholdStatus");
const soundThresholdFeed = document.getElementById("soundThresholdFeed");

let serverThreshold = 0.80;  // last value confirmed by server; slider compares

function setThresholdSlider(v, { fromServer = false } = {}) {
  const n = Number(v);
  if (!isFinite(n)) return;
  soundThresholdSlider.value = n.toFixed(2);
  soundThresholdValue.textContent = n.toFixed(2);
  if (fromServer) serverThreshold = n;
  soundThresholdSaveBtn.disabled = Math.abs(n - serverThreshold) < 0.005;
}

soundThresholdSlider.addEventListener("input", () => {
  setThresholdSlider(soundThresholdSlider.value);
});
soundThresholdSaveBtn.addEventListener("click", async () => {
  const v = Number(soundThresholdSlider.value);
  soundThresholdSaveBtn.disabled = true;
  soundThresholdStatus.textContent = "saving…";
  try {
    const res = await fetch("/api/session/sound-match-threshold", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ threshold: v }),
    });
    if (!res.ok) {
      soundThresholdStatus.textContent = "save failed: " + await res.text();
      soundThresholdSaveBtn.disabled = false;
      return;
    }
    const j = await res.json();
    serverThreshold = j.threshold;
    soundThresholdStatus.textContent = `saved · ${j.threshold.toFixed(2)}`;
    setThresholdSlider(j.threshold, { fromServer: true });
    setTimeout(() => { soundThresholdStatus.textContent = ""; }, 2500);
    logLive(`sound-match threshold → ${j.threshold.toFixed(2)}`, "ok");
  } catch (e) {
    soundThresholdStatus.textContent = "save error: " + e.message;
    soundThresholdSaveBtn.disabled = false;
  }
});

function pushThresholdFeed(msg) {
  // Keep at most 20 rows; newest on top.
  const empty = soundThresholdFeed.querySelector(".shoot-hint");
  if (empty) empty.remove();
  const r = msg.release_sim == null ? "—" : Number(msg.release_sim).toFixed(2);
  const i = msg.impact_sim  == null ? "—" : Number(msg.impact_sim).toFixed(2);
  const ts = new Date().toLocaleTimeString([], { hour12: false });
  const verdict = msg.error ? `ERR (${msg.error})` : (msg.accept ? (msg.no_template ? "NO-TPL" : "ACCEPT") : "REJECT");
  const row = document.createElement("div");
  row.className = "row " + (msg.accept ? "accept" : "reject");
  row.innerHTML = `<span class="ts">${ts}</span><span class="sim">r ${r}</span><span class="sim">i ${i}</span><span class="verdict">${verdict}</span>`;
  soundThresholdFeed.insertBefore(row, soundThresholdFeed.firstChild);
  while (soundThresholdFeed.children.length > 20) {
    soundThresholdFeed.removeChild(soundThresholdFeed.lastChild);
  }
}

function activateTab(name) {
  if (currentTab === name) return;
  currentTab = name;
  for (const b of tabButtons) b.classList.toggle("active", b.dataset.tab === name);
  for (const p of tabPanels) p.classList.toggle("active", p.dataset.tab === name);
  shootTabActions.classList.toggle("hidden", name !== "shoot");
  if (name === "shoot") {
    // Canvas size depends on visibility; resize now that it's visible.
    requestAnimationFrame(sizeCanvas);
  }
  if (name === "sound") reloadSoundShots();
}
for (const b of tabButtons) {
  // Ignore clicks on nested elements inside the tab-actions cluster.
  if (!b.dataset.tab) continue;
  b.addEventListener("click", () => activateTab(b.dataset.tab));
}

// ==== Live session DOM ====================================================

const bulbPhone = document.getElementById("bulbPhone");
const bulbSession = document.getElementById("bulbSession");
const bulbRange = document.getElementById("bulbRange");
const bulbCalib = document.getElementById("bulbCalib");
const bulbShotCount = document.getElementById("bulbShotCount");

function setBulb(el, ok, title) {
  el.className = el.classList.contains("shot-count-bulb") ? "bulb shot-count-bulb" : "bulb " + (ok ? "ok" : "bad");
  el.title = title;
}

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
const lastShotTargetPhoto = document.getElementById("lastShotTargetPhoto");
let selectedShotIdx = null; // index into shots[] of the currently displayed shot
const statsShotNum = document.getElementById("statsShotNum");
const statSpeed = document.getElementById("statSpeed");
const statTime = document.getElementById("statTime");
const statDist = document.getElementById("statDist");
const statOffset = document.getElementById("statOffset");
const statEnergy = document.getElementById("statEnergy");
const statProcessed = document.getElementById("statProcessed");
const timingsBreakdown = document.getElementById("timingsBreakdown");
const timingDecode = document.getElementById("timingDecode");
const timingDetect = document.getElementById("timingDetect");
const timingTrack = document.getElementById("timingTrack");
const timingTrim = document.getElementById("timingTrim");
// Stats panel toggle button in the Telemetry header. Persisted across reloads.
const statsToggleBtn = document.getElementById("statsToggleBtn");
const telemetryStats = document.querySelector(".telemetry-stats");
const STATS_KEY = "arrowlab.statsPanelOpen";
function applyStatsPanelVisibility() {
  const open = localStorage.getItem(STATS_KEY) === "1";
  telemetryStats.classList.toggle("hidden", !open);
  statsToggleBtn.classList.toggle("on", open);
}
statsToggleBtn.addEventListener("click", () => {
  const wasOpen = localStorage.getItem(STATS_KEY) === "1";
  localStorage.setItem(STATS_KEY, wasOpen ? "0" : "1");
  applyStatsPanelVisibility();
});
applyStatsPanelVisibility();
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
const showAllShotsChk = document.getElementById("showAllShots");
showAllShotsChk.addEventListener("change", () => renderTelemetry());

// Calibration frame used as the telemetry background — "scene" view so the
// trajectory overlays the actual capture, not an abstract box.
const telemetryBg = new Image();
let telemetryBgUrl = null;
let telemetryBgLoaded = false;
telemetryBg.addEventListener("load", () => {
  telemetryBgLoaded = true;
  sizeCanvas();
});

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
const lastState = { active: false, has_range: false, has_annotation: false, phone_connected: false };

function canShoot() {
  return lastState.active && lastState.phone_connected && lastState.has_annotation;
}

function logLive(line, kind = "info") {
  const div = document.createElement("div");
  const ts = new Date().toLocaleTimeString();
  div.textContent = `[${ts}] ${line}`;
  if (kind === "error") div.style.color = "#ff8080";
  if (kind === "ok") div.style.color = "#9ef89f";
  liveLog.prepend(div);
  while (liveLog.children.length > 40) liveLog.lastChild.remove();
}

let sessionState = {};
function applyState(st) {
  sessionState = st;
  if (armed && typeof shooterUpdateSession === "function") shooterUpdateSession();
  // Hydrate sound-match threshold slider on every state update, but only
  // when the user isn't mid-edit (slider matches server's view).
  if (typeof st.sound_match_threshold === "number") {
    serverThreshold = st.sound_match_threshold;
    if (Math.abs(Number(soundThresholdSlider.value) - serverThreshold) < 0.005) {
      setThresholdSlider(serverThreshold, { fromServer: true });
    } else {
      // Slider has unsaved local edits; just update the disabled state of
      // Save by re-evaluating the diff.
      soundThresholdSaveBtn.disabled = false;
    }
  }
  setBulb(bulbPhone, st.phone_connected,
    st.phone_connected ? "phone: connected" : "phone: none");
  setBulb(bulbSession, st.active,
    st.active ? `session: ${st.session_id || "active"}` : "session: none");
  setBulb(bulbRange, st.has_range,
    st.has_range ? "range: set" : "range: not set");
  setBulb(bulbCalib, st.has_annotation,
    st.has_annotation ? "calibrated" : "not calibrated");
  const n = st.shot_count || 0;
  bulbShotCount.textContent = String(n);
  bulbShotCount.title = `${n} shot${n === 1 ? "" : "s"}`;

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
    // Preload calibration frame for the telemetry scene background
    if (telemetryBgUrl !== st.calibration_frame) {
      telemetryBgUrl = st.calibration_frame;
      telemetryBgLoaded = false;
      telemetryBg.src = st.calibration_frame + (st.calibration_frame.includes("?") ? "&" : "?") + "bg=" + Date.now();
    }
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
  lastState.phone_connected = !!st.phone_connected;
  // Re-evaluate the SHOT button's disabled state after lastState updates
  // (covers mid-countdown / mid-shot phone disconnects etc.).
  shotBtn.disabled = shotInFlight || !canShoot();

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
    if (!armed) setShotButton("tracking");
    else shooterOnUploaded(msg);
  } else if (msg.type === "shot_ready") {
    logLive(`shot ${msg.shot}: ready`, "ok");
    // shots[] is synced by the state broadcast that follows; just select the new one.
    pendingShotIdx = shots.length;
    setTimeout(() => {
      const idx = shots.findIndex(sh => sh.shot === msg.shot);
      if (idx >= 0) selectShot(idx, { play: false });
    }, 50);
    // Don't override the ARMED UI — armed mode keeps listening for the next shot.
    if (!armed) setShotButton("idle");
    else shooterOnReady(msg);
  } else if (msg.type === "shot_failed") {
    logLive(`shot ${msg.shot}: FAILED (${msg.reason || "unknown"})`, "error");
    if (!armed) {
      setShotButton("error");
      setTimeout(() => setShotButton("idle"), 3000);
    } else {
      shooterOnFailed(msg);
    }
  } else if (msg.type === "sound_match_result") {
    // Live per-candidate similarity scores. Useful for tuning the
    // threshold during armed-mode firing — every candidate the phone
    // sends, accepted or rejected, lands here.
    const fmt = v => (v == null ? "-" : Number(v).toFixed(2));
    const r = fmt(msg.release_sim);
    const i = fmt(msg.impact_sim);
    if (msg.accept && msg.no_template) {
      logLive(`sound-match: no template (auto-accept)`);
    } else if (msg.accept && msg.error) {
      logLive(`sound-match: accept (server err=${msg.error})`);
    } else if (msg.accept) {
      logLive(`sound-match ACCEPT r=${r} i=${i}`, "ok");
    } else if (msg.error) {
      logLive(`sound-match REJECT err=${msg.error}`, "error");
    } else {
      logLive(`sound-match REJECT r=${r} i=${i}`);
    }
    if (armed) shooterOnSoundMatch(msg);
    pushThresholdFeed(msg);
  } else if (msg.type === "calibration_record_started") {
    logLive(`calibration recording started (${msg.duration}s)`);
    setCalibRecording(msg.duration);
  } else if (msg.type === "calibration_record_ready") {
    logLive(`calibration recording ready: ${msg.peak_count} peaks`, "ok");
    loadCalibRecord();
  } else if (msg.type === "armed") {
    logLive("armed — listening for release+impact", "ok");
    setArmedUI(true);
  } else if (msg.type === "disarmed") {
    logLive("disarmed");
    setArmedUI(false);
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
  // Always require an active session + connected phone + calibration.
  // In-flight shots override (already checked above via shotInFlight gate).
  shotBtn.disabled = shotInFlight || !canShoot();
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

  // Phase 2: 5→1 countdown. Operator releases the arrow ON "1". The post-
  // countdown wait gives reaction time + arrow flight + safety margin
  // before the slice command goes out — the rolling buffer needs to still
  // contain the release+impact when sliced.
  let count = 5;
  const countTick = () => {
    if (my !== countdownAbortToken) return;
    if (count > 0) {
      shotBtn.textContent = `${count}…`;
      count--;
      setTimeout(countTick, 1000);
    } else {
      shotBtn.textContent = "🏹 FIRE!";
      shotBtn.style.background = "#c04040";
      setTimeout(() => {
        if (my !== countdownAbortToken) return;
        liveWS.send(JSON.stringify({ type: "trigger_shot" }));
        logLive("SHOT! triggered");
        setShotButton("recording");
      }, 3500);
    }
  };

  walkTick();
}

function abortCountdown() {
  countdownAbortToken++;
  setShotButton("idle");
}

let armed = false;

// ===== Shooter overlay (focal armed-mode view) =====
// Full-viewport state machine shown while armed. Every peak-pair detection
// flips the panel through MATCHING → REJECTED or UPLOADING → RESULT/FAILED,
// then back to READY ("FIRE"). Designed to be readable across the room.
const shooterOverlay = document.getElementById("shooterOverlay");
const shooterHeadline = document.getElementById("shooterHeadline");
const shooterSub = document.getElementById("shooterSub");
const shooterStats = document.getElementById("shooterStats");
const shooterStatA = document.getElementById("shooterStatA");
const shooterStatLA = document.getElementById("shooterStatLA");
const shooterStatB = document.getElementById("shooterStatB");
const shooterStatLB = document.getElementById("shooterStatLB");
const shooterSession = document.getElementById("shooterSession");
const shooterStopBtn = document.getElementById("shooterStopBtn");

let shooterRevertTimer = null;
let shooterLastSims = { release: null, impact: null };

function shooterFmt(v) { return v == null ? "—" : Number(v).toFixed(2); }

function shooterSetPanel(state, opts = {}) {
  if (shooterRevertTimer) { clearTimeout(shooterRevertTimer); shooterRevertTimer = null; }
  shooterOverlay.dataset.state = state;
  shooterHeadline.textContent = opts.head || "";
  shooterSub.textContent = opts.sub || "";
  if (opts.statsShow) {
    shooterStats.hidden = false;
    shooterStatA.textContent = opts.statA;
    shooterStatLA.textContent = opts.labelA;
    shooterStatB.textContent = opts.statB;
    shooterStatLB.textContent = opts.labelB;
  } else {
    shooterStats.hidden = true;
  }
  if (opts.revertMs) {
    shooterRevertTimer = setTimeout(shooterReady, opts.revertMs);
  }
}
function shooterReady() {
  if (!armed) return;
  shooterSetPanel("ready", { head: "FIRE", sub: "armed — waiting for release" });
}
function shooterUpdateSession() {
  const id = (sessionState && sessionState.session_id) || "";
  const n = (sessionState && sessionState.shot_count) || 0;
  shooterSession.textContent = id ? `${id} · ${n} shots` : `${n} shots`;
}

function shooterOnSoundMatch(msg) {
  shooterLastSims.release = msg.release_sim;
  shooterLastSims.impact = msg.impact_sim;
  if (msg.accept) return; // wait for shot_uploaded
  shooterSetPanel("rejected", {
    head: "Rejected",
    sub: msg.error ? `error: ${msg.error}` : "sound did not match release/impact template",
    statsShow: true,
    statA: shooterFmt(msg.release_sim), labelA: "release sim",
    statB: shooterFmt(msg.impact_sim),  labelB: "impact sim",
    revertMs: 2200,
  });
}
function shooterOnUploaded(msg) {
  const showSims = shooterLastSims.release != null && shooterLastSims.impact != null;
  shooterSetPanel("uploading", {
    head: `Shot ${msg.shot}`,
    sub: "processing…",
    statsShow: showSims,
    statA: showSims ? shooterFmt(shooterLastSims.release) : "—", labelA: "release sim",
    statB: showSims ? shooterFmt(shooterLastSims.impact)  : "—", labelB: "impact sim",
  });
}
function shooterOnReady(msg) {
  const t = msg.trajectory || {};
  const speed = t.speed_audio_ms;
  const flightS = (t.audio_release_s != null && t.audio_impact_s != null)
    ? (t.audio_impact_s - t.audio_release_s) : null;
  const haveSpeed = speed != null && isFinite(speed);
  const haveFlight = flightS != null && isFinite(flightS);
  shooterSetPanel("result", {
    head: `✓ Shot ${msg.shot}`,
    sub: `${t.detections_kept ?? "?"} detections`,
    statsShow: haveSpeed || haveFlight,
    statA: haveSpeed ? speed.toFixed(1) : "—", labelA: "m/s",
    statB: haveFlight ? Math.round(flightS * 1000) : "—", labelB: "ms flight",
    revertMs: 2200,
  });
}
function shooterOnFailed(msg) {
  shooterSetPanel("failed", {
    head: `✗ Shot ${msg.shot}`,
    sub: msg.reason || "failed — check corridor / annotation",
    revertMs: 2500,
  });
}

shooterStopBtn.addEventListener("click", () => {
  if (liveWS && liveWS.readyState === WebSocket.OPEN) {
    liveWS.send(JSON.stringify({ type: "disarm" }));
  }
  setArmedUI(false);
});

function setArmedUI(on) {
  armed = on;
  if (on) {
    shotBtn.textContent = "ARMED — tap to stop";
    shotBtn.style.background = "#b05020";
    shotBtn.disabled = false;
    shooterUpdateSession();
    shooterOverlay.hidden = false;
    shooterReady();
  } else {
    setShotButton("idle");
    shooterOverlay.hidden = true;
    if (shooterRevertTimer) { clearTimeout(shooterRevertTimer); shooterRevertTimer = null; }
  }
}

shotBtn.addEventListener("click", () => {
  if (shotInFlight) {
    abortCountdown();
    return;
  }
  if (armed) {
    if (liveWS && liveWS.readyState === WebSocket.OPEN) {
      liveWS.send(JSON.stringify({ type: "disarm" }));
    }
    setArmedUI(false);
    return;
  }
  // Prefer armed mode when the phone has audio onset detection. Hold Shift
  // on click to force the legacy countdown flow.
  const forceCountdown = window.event && window.event.shiftKey;
  if (forceCountdown) {
    startShotCountdown();
    return;
  }
  if (liveWS && liveWS.readyState === WebSocket.OPEN) {
    liveWS.send(JSON.stringify({ type: "arm" }));
  }
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
  const intercept = my - slope * mx;
  return { slope, intercept, eval: (x) => slope * x + intercept };
}

// Draw a smooth curve through canvas-space points using midpoint smoothing
// (quadraticCurveTo with each raw point as the control and the midpoint to
// the next as the vertex). Hides per-frame detection jitter while still
// tracing the actual path.
function strokeSmoothCurve(pts) {
  if (pts.length < 2) return;
  telemetryCtx.beginPath();
  telemetryCtx.moveTo(pts[0].x, pts[0].y);
  if (pts.length === 2) {
    telemetryCtx.lineTo(pts[1].x, pts[1].y);
  } else {
    for (let i = 1; i < pts.length - 1; i++) {
      const p = pts[i], q = pts[i + 1];
      const mx = (p.x + q.x) / 2;
      const my = (p.y + q.y) / 2;
      telemetryCtx.quadraticCurveTo(p.x, p.y, mx, my);
    }
    const last = pts[pts.length - 1];
    telemetryCtx.quadraticCurveTo(
      pts[pts.length - 2].x, pts[pts.length - 2].y,
      last.x, last.y,
    );
  }
  telemetryCtx.stroke();
}

// Keep the leading run of detections where x advances forward. Once the
// arrow hits the target the tracker keeps firing on stuck-arrow / vibration
// motion — x stops increasing or reverses. Cut there.
function cleanDetections(dets) {
  if (!dets || dets.length === 0) return [];
  const out = [];
  let maxX = -Infinity;
  for (const d of dets) {
    if (d.x < maxX - 5) break; // 5px backward slack for single-frame jitter
    out.push(d);
    if (d.x > maxX) maxX = d.x;
  }
  return out.length >= 3 ? out : dets;
}

// Y-fit over clean detections (linear is plenty for short flight windows
// — full gravity arc is only a few pixels over this span).
function yFitFromDets(dets) {
  if (!dets || dets.length < 2) return null;
  return linearFit(dets.map(d => d.frame), dets.map(d => d.y));
}

function xFitFromDets(dets) {
  if (!dets || dets.length < 2) return null;
  return linearFit(dets.map(d => d.frame), dets.map(d => d.x));
}

function shotColor(i) {
  const hue = (i * 55) % 360;
  return `hsl(${hue}, 80%, 60%)`;
}

function getViewBox() {
  // Crop to a horizontal band around the flight corridor (plus the target
  // bbox if it falls outside) — the arrow never flies through the top/bottom
  // of the frame so those pixels are wasted and force a tall canvas.
  const latest = shots[shots.length - 1]?.trajectory;
  const imgW = latest?.width || telemetryBg.naturalWidth || 1920;
  const imgH = latest?.height || telemetryBg.naturalHeight || 1080;
  const ann = latest?.annotation || {};
  const corridor = ann.corridor;
  const bbox = ann.target?.bbox;
  if (corridor) {
    let y0 = corridor.y_top;
    let y1 = corridor.y_bottom;
    if (bbox && bbox.length === 4) {
      y0 = Math.min(y0, bbox[1]);
      y1 = Math.max(y1, bbox[3]);
    }
    const pad = Math.max(30, Math.round((y1 - y0) * 0.15));
    return {
      x0: 0,
      y0: Math.max(0, y0 - pad),
      x1: imgW,
      y1: Math.min(imgH, y1 + pad),
    };
  }
  return { x0: 0, y0: 0, x1: imgW, y1: imgH };
}

function sizeCanvas() {
  const parentW = telemetryCanvas.parentElement.clientWidth || 800;
  const vb = getViewBox();
  const aspect = (vb.x1 - vb.x0) / Math.max(1, vb.y1 - vb.y0);
  const cssW = parentW;
  // Honour the crop aspect ratio so nothing stretches. Cap height at a level
  // that keeps the whole Shoot tab visible without scrolling.
  const cssH = Math.max(100, Math.min(240, cssW / aspect));
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

  const vb = getViewBox();
  const vbW = vb.x1 - vb.x0;
  const vbH = vb.y1 - vb.y0;
  const sx = w / vbW;
  const sy = h / vbH;
  const mapX = (x) => (x - vb.x0) * sx;
  const mapY = (y) => (y - vb.y0) * sy;

  // Scene background: the calibration JPEG, cropped to the viewbox so it
  // matches the trajectory space (no stretch).
  if (telemetryBgLoaded) {
    telemetryCtx.drawImage(telemetryBg, vb.x0, vb.y0, vbW, vbH, 0, 0, w, h);
    telemetryCtx.fillStyle = "rgba(0,0,0,0.3)";
    telemetryCtx.fillRect(0, 0, w, h);
  }

  if (shots.length === 0) return;

  // Per-shot trails: real detection dots for the clean forward run, then a
  // dashed extrapolation via quadratic y-fit carries the trajectory on to
  // the target. Noisy post-impact detections are dropped.
  const FADE_WINDOW = 5;
  const newestIdx = shots.length - 1;
  const showAll = showAllShotsChk.checked;
  // When "show all" is off, suppress the entire static loop so the canvas
  // only lights up during active Play (via the pending-shot block below).
  if (showAll) shots.forEach((shot, i) => {
    // The shot that's armed to fly (pendingShotIdx = currently selected) is
    // never rendered statically — it only appears as the growing lit trail
    // during Play. Older shots render normally.
    if (i === pendingShotIdx) return;
    const age = newestIdx - i;
    if (age >= FADE_WINDOW) return;
    const alpha = 1 - age / FADE_WINDOW;
    const allDets = shot.trajectory?.detections || [];
    if (allDets.length === 0) return;
    const clean = cleanDetections(allDets);
    const color = shotColor(i);
    telemetryCtx.save();
    telemetryCtx.globalAlpha = alpha;
    // Build one continuous point list: clean detections + extrapolated samples
    // up to target.cx along the linear fit, shifted so the fit line passes
    // exactly through the last clean detection — keeps the junction C0
    // continuous so there's no visible "jump" into the extrapolation.
    const pts = clean.map(d => ({ x: mapX(d.x), y: mapY(d.y) }));
    const ann = shot.trajectory?.annotation || {};
    const tx = ann.target?.cx;
    const fx = xFitFromDets(clean);
    const fy = yFitFromDets(clean);
    if (tx != null && fx && fy) {
      const last = clean[clean.length - 1];
      const xOff = last.x - fx.eval(last.frame);
      const yOff = last.y - fy.eval(last.frame);
      const fHit = Math.abs(fx.slope) > 1e-6 ? (tx - fx.intercept - xOff) / fx.slope : last.frame;
      // One extrapolated sample per frame — matches the detection density so
      // the smoothed curve has uniform point spacing along the whole path.
      const steps = Math.max(2, Math.round(fHit - last.frame));
      for (let k = 1; k <= steps; k++) {
        const fr = last.frame + (fHit - last.frame) * (k / steps);
        pts.push({ x: mapX(fx.eval(fr) + xOff), y: mapY(fy.eval(fr) + yOff) });
      }
    }
    telemetryCtx.strokeStyle = color;
    telemetryCtx.lineWidth = 2;
    telemetryCtx.lineCap = "round";
    telemetryCtx.lineJoin = "round";
    strokeSmoothCurve(pts);
    // Shot label: at the last point of the rendered curve.
    const endPt = pts[pts.length - 1];
    telemetryCtx.font = "600 11px ui-monospace, monospace";
    telemetryCtx.fillText(`#${shot.shot}`, endPt.x + 6, endPt.y - 6);
    telemetryCtx.restore();
  });

  // Pending shot: whole trail drawn faintly, dots "lit up" as playback passes
  // each detection frame, and a marker tracks the leading edge.
  if (pendingShotIdx != null && currentTime != null) {
    const shot = shots[pendingShotIdx];
    const traj = shot.trajectory;
    const dets = traj?.detections || [];
    if (dets.length > 0 && traj?.fps) {
      const color = shotColor(pendingShotIdx);
      const clipStart = traj.clip_start_frame ?? dets[0].frame;
      const trimOffset = Number.isFinite(shot.trim_offset_s) ? shot.trim_offset_s : 0;
      const frame = clipStart + (currentTime + trimOffset) * traj.fps;
      const clean = cleanDetections(dets);
      // Build continuous path: clean detections + fit-extrapolation to target
      const tx0 = traj?.annotation?.target?.cx;
      const fx0 = xFitFromDets(clean);
      const fy0 = yFitFromDets(clean);
      const fullPts = clean.map(d => ({ x: mapX(d.x), y: mapY(d.y), f: d.frame }));
      let fHit0 = null;
      if (tx0 != null && fx0 && fy0) {
        const last = clean[clean.length - 1];
        const xOff = last.x - fx0.eval(last.frame);
        const yOff = last.y - fy0.eval(last.frame);
        fHit0 = Math.abs(fx0.slope) > 1e-6 ? (tx0 - fx0.intercept - xOff) / fx0.slope : last.frame;
        // One extrapolated sample per frame — same density as real detections,
        // so the marker moves at identical frame-rate through both regions.
        const steps = Math.max(2, Math.round(fHit0 - last.frame));
        for (let k = 1; k <= steps; k++) {
          const fr = last.frame + (fHit0 - last.frame) * (k / steps);
          fullPts.push({ x: mapX(fx0.eval(fr) + xOff), y: mapY(fy0.eval(fr) + yOff), f: fr });
        }
      }
      // (Faint pre-drawn baseline removed — the lit trail is the only render
      // of the pending shot so the animation grows cleanly from zero.)
      // Lit-up portion: same curve from start up to current playback frame
      let litIdx = -1;
      for (let i = 0; i < fullPts.length; i++) {
        if (fullPts[i].f > frame) break;
        litIdx = i;
      }
      if (litIdx >= 1) {
        telemetryCtx.strokeStyle = color;
        telemetryCtx.lineWidth = 2.5;
        strokeSmoothCurve(fullPts.slice(0, litIdx + 1));
      }
      // Marker: walk fullPts by its f value so the dot rides the already-drawn
      // continuous curve (detections + extrapolation alike).
      let markerX = null, markerY = null;
      if (frame >= fullPts[0]?.f) {
        let idx = 0;
        for (let i = 0; i < fullPts.length; i++) {
          if (fullPts[i].f > frame) break;
          idx = i;
        }
        if (idx < fullPts.length - 1) {
          const a = fullPts[idx], b = fullPts[idx + 1];
          const t = Math.max(0, Math.min(1, (frame - a.f) / (b.f - a.f)));
          markerX = a.x + (b.x - a.x) * t;
          markerY = a.y + (b.y - a.y) * t;
        } else {
          markerX = fullPts[idx].x; markerY = fullPts[idx].y;
        }
      }
      if (markerX != null) {
        telemetryCtx.strokeStyle = "#ffffff";
        telemetryCtx.lineWidth = 2;
        telemetryCtx.beginPath();
        telemetryCtx.arc(markerX, markerY, 6, 0, Math.PI * 2);
        telemetryCtx.stroke();
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
  if (sh.target_photo_url) {
    lastShotTargetPhoto.src = sh.target_photo_url + "?t=" + Date.now();
  } else {
    lastShotTargetPhoto.removeAttribute("src");
  }
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
let playActive = false;
const scheduledSoundTimers = [];
function clearScheduledSounds() {
  while (scheduledSoundTimers.length) clearTimeout(scheduledSoundTimers.pop());
}
function playSnippet(url) {
  try {
    const a = new Audio(url);
    a.volume = 1.0;
    a.playbackRate = 1.0;
    a.play().catch(() => {});
  } catch (_) {}
}
function startSyncPlay() {
  if (shots.length === 0) return;
  const idx = selectedShotIdx != null ? selectedShotIdx : shots.length - 1;
  pendingShotIdx = idx;
  const playbackRate = 0.0625;
  telemetryInfo.textContent = `shot ${shots[idx].shot} (playing ${playbackRate}×)`;
  const shot = shots[idx] || {};
  const startS = Number.isFinite(shot.start_s) ? shot.start_s : 0;
  try { lastShotClip.currentTime = startS; } catch {}
  try { lastShotTracked.currentTime = startS; } catch {}
  lastShotClip.playbackRate = playbackRate;
  lastShotTracked.playbackRate = playbackRate;
  // Mute the raw clip — slow-mo'd audio sounds awful. Instead, schedule the
  // 300 ms release/impact snippets (at their original 1× rate) to fire at
  // the correct moments in the slow-mo timeline.
  lastShotClip.muted = true;
  clearScheduledSounds();
  const trimOff = Number.isFinite(shot.trim_offset_s) ? shot.trim_offset_s : 0;
  const releaseS = Number.isFinite(shot.audio_release_s) ? shot.audio_release_s : null;
  const impactS = Number.isFinite(shot.audio_impact_s) ? shot.audio_impact_s : null;
  const scheduleSnippet = (atSrcSec, url) => {
    if (atSrcSec == null) return;
    const delayMs = Math.max(0, (atSrcSec - trimOff - startS) / playbackRate * 1000);
    scheduledSoundTimers.push(setTimeout(() => playSnippet(url), delayMs));
  };
  scheduleSnippet(releaseS, `/api/calibration-sound/snippet/${shot.shot}/release`);
  scheduleSnippet(impactS, `/api/calibration-sound/snippet/${shot.shot}/impact`);

  Promise.all([lastShotClip.play(), lastShotTracked.play()]).catch(() => {});
  playActive = true;
  if (playRaf) cancelAnimationFrame(playRaf);
  const loop = () => {
    if (!playActive) { playRaf = 0; renderTelemetry(); return; }
    // Prefer whichever video is actually progressing; raw trim may have a
    // broken duration or load slower than the tracked.
    const trackedEnded = lastShotTracked.ended;
    const rawEnded = lastShotClip.ended;
    const src = (!rawEnded && lastShotClip.currentTime > 0)
      ? lastShotClip
      : (!trackedEnded ? lastShotTracked : lastShotClip);
    renderTelemetry(src.currentTime);
    if (rawEnded && trackedEnded) {
      playActive = false;
      playRaf = 0;
      renderTelemetry();
    } else {
      playRaf = requestAnimationFrame(loop);
    }
  };
  playRaf = requestAnimationFrame(loop);
}
function stopSyncPlay() {
  playActive = false;
  lastShotClip.pause();
  lastShotTracked.pause();
  clearScheduledSounds();
  if (playRaf) { cancelAnimationFrame(playRaf); playRaf = 0; }
  renderTelemetry();
}
playAllBtn.addEventListener("click", startSyncPlay);
pauseAllBtn.addEventListener("click", stopSyncPlay);
lastShotClip.addEventListener("ended", () => {
  // The RAF loop exits itself once both videos report ended. Just refresh
  // the info text here.
  telemetryInfo.textContent = `${shots.length} shot${shots.length === 1 ? "" : "s"}`;
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
  const tim = shot.timings;
  const missing = !tim;
  const fmtS = (v) => (missing ? "not recorded" : (Number.isFinite(v) ? `${v.toFixed(1)} s` : "—"));
  timingDecode.textContent = fmtS(tim?.decode_s);
  timingDetect.textContent = fmtS(tim?.detect_s);
  timingTrack.textContent = fmtS(tim?.track_s);
  timingTrim.textContent = fmtS(tim?.trim_s);
  const s = computeShotStats(shot);
  // Speed + flight time: prefer the acoustic chronograph (more accurate than
  // the pixel tracker). Fall back to the tracker derivation if audio is
  // missing. Energy also uses the preferred speed.
  const audioSpeed = Number.isFinite(shot.speed_audio_ms) ? shot.speed_audio_ms : null;
  const audioTimeMs = Number.isFinite(shot.audio_release_s) && Number.isFinite(shot.audio_impact_s)
    ? (shot.audio_impact_s - shot.audio_release_s) * 1000
    : null;
  const speedMs = audioSpeed != null ? audioSpeed : (s?.speedMs ?? null);
  const flightMs = audioTimeMs != null ? audioTimeMs : (s ? s.durSec * 1000 : null);
  statSpeed.textContent = speedMs != null ? fmt(speedMs, 1, " m/s") : "—";
  statTime.textContent = flightMs != null ? fmt(flightMs, 0, " ms") : "—";
  if (s) {
    statDist.textContent = fmt(s.distanceM, 2, " m");
    statOffset.textContent = fmt(s.hitOffsetCm, 1, " cm");
    // Recompute energy against the preferred speed
    const massGrains = shot.trajectory?.range?.arrow_mass_grains
      ?? (currentRange?.arrow_mass_grains ?? null);
    const massKg = massGrains ? massGrains * 6.479891e-5 : null;
    if (speedMs != null && massKg) {
      const keJ = 0.5 * massKg * speedMs * speedMs;
      const keFtLbs = keJ * 0.737562;
      statEnergy.textContent = `${keJ.toFixed(1)} J / ${keFtLbs.toFixed(1)} ft·lbs`;
    } else {
      statEnergy.textContent = "— (need arrow mass)";
    }
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
