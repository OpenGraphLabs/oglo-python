"use strict";

const $ = (id) => document.getElementById(id);
const actions = [
  { id: "toggle", label: "Start / stop", defaultCode: "F9" },
  { id: "confirm", label: "Calibrate / keep", defaultCode: "F10" },
  { id: "discard", label: "Discard", defaultCode: "F11" },
];
const mappingKey = "oglo-studio-button-map-v1";
let map = { toggle: "F9", confirm: "F10", discard: "F11" };
try {
  const saved = JSON.parse(localStorage.getItem(mappingKey) || "null");
  if (saved && actions.every((item) => typeof saved[item.id] === "string")) map = saved;
} catch (_) { /* Keep documented defaults. */ }

let currentStatus = null;
let currentLive = null;
let currentStep = Number(sessionStorage.getItem("oglo-studio-step") || 1);
if (!Number.isInteger(currentStep) || currentStep < 1 || currentStep > 6) currentStep = 1;
let unlockedStep = Number(sessionStorage.getItem("oglo-studio-unlocked") || 1);
if (!Number.isInteger(unlockedStep) || unlockedStep < 1 || unlockedStep > 6) unlockedStep = 1;
let shownStep = null;
let renderedReviewKey = "";
let selectedEpisodeId = null;
let learning = null;
let testing = false;
let busy = false;
let discovering = false;
let calibrationConfirmed = false;
let renderedCalibration = null;
const pressed = new Set();
const lastPress = new Map();

function notice(message, error = false) {
  const box = $("notice");
  box.textContent = message;
  box.className = `notice visible${error ? " error" : ""}`;
}

function setStep(step) {
  if (step < 1 || step > 6 || step > unlockedStep) return;
  const changed = shownStep !== step;
  currentStep = step;
  shownStep = step;
  sessionStorage.setItem("oglo-studio-step", String(step));
  sessionStorage.setItem("oglo-studio-unlocked", String(unlockedStep));
  for (let index = 1; index <= 6; index++) {
    $(`step-${index}`).hidden = index !== step;
    const button = document.querySelector(`.step-button[data-step="${index}"]`);
    button.disabled = index > unlockedStep;
    button.classList.toggle("active", index === step);
    button.classList.toggle("done", index < step);
    if (index === step) button.setAttribute("aria-current", "step");
    else button.removeAttribute("aria-current");
  }
  if (changed) window.scrollTo({ top: 0, behavior: "smooth" });
}

async function request(path, body) {
  const response = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${response.status})`);
  return data;
}

async function perform(path, body) {
  if (busy) return;
  busy = true;
  try {
    render(await request(path, body));
    notice("Action completed.");
  } catch (error) {
    notice(error.message, true);
    await refresh();
  } finally {
    busy = false;
    if (currentStatus) render(currentStatus);
  }
}

function pairConnected(status) {
  return status?.gloves?.length === 2 &&
    status.gloves.some((glove) => glove.side === "left") &&
    status.gloves.some((glove) => glove.side === "right") && !!status.camera;
}

function updateConnectButton() {
  const left = $("left-port").value;
  const right = $("right-port").value;
  $("connect").disabled = busy || discovering ||
    !["disconnected", "ready", "error"].includes(currentStatus?.state) ||
    !$("camera-index").value || !left || !right || left === right;
}

function fillDeviceSelect(id, choices, placeholder, preferred) {
  const select = $(id);
  const previous = select.value;
  select.replaceChildren(new Option(placeholder, ""));
  for (const choice of choices) select.add(new Option(choice.label, String(choice.value)));
  const values = choices.map((choice) => String(choice.value));
  select.value = values.includes(previous) ? previous :
    (preferred !== undefined && values.includes(String(preferred)) ? String(preferred) : "");
}

async function discoverDevices() {
  if (discovering || busy) return;
  discovering = true;
  $("refresh-devices").disabled = true;
  $("device-discovery").textContent = "Scanning USB devices…";
  updateConnectButton();
  try {
    const response = await fetch("/api/devices", { cache: "no-store" });
    const devices = await response.json();
    if (!response.ok) throw new Error(devices.detail || `Discovery failed (${response.status})`);
    const cameras = devices.cameras.map((camera) => ({
      value: JSON.stringify({ index: camera.index, mode: camera.mode || "default",
                              name: camera.name || null }),
      label: camera.label,
    }));
    fillDeviceSelect("camera-index", cameras, "Select a camera",
      cameras.length === 1 ? cameras[0].value : undefined);
    for (const side of ["left", "right"]) {
      const candidates = devices.gloves.filter((glove) => glove.side === side || glove.side == null)
        .map((glove) => ({ value: glove.port, label: glove.label }));
      const identified = devices.gloves.filter((glove) => glove.side === side);
      fillDeviceSelect(`${side}-port`, candidates, `Select ${side} glove`,
        identified.length === 1 ? identified[0].port : undefined);
    }
    $("device-discovery").textContent = devices.gloves.length ?
      `${devices.gloves.length} OGLO USB device${devices.gloves.length === 1 ? "" : "s"} found` :
      "No OGLO USB gloves found. Plug them in and refresh.";
  } catch (error) {
    $("device-discovery").textContent = error.message;
    notice(`Device discovery failed: ${error.message}`, true);
  } finally {
    discovering = false;
    $("refresh-devices").disabled = false;
    updateConnectButton();
  }
}

function liveReady() {
  if (!pairConnected(currentStatus) || !currentLive?.camera?.ready || currentLive.camera.dark ||
      currentLive.camera.age_ms > 1500 || Object.keys(currentLive.errors || {}).length) return false;
  return ["left", "right"].every((side) =>
    currentLive.gloves?.[side] && currentLive.gloves[side].age_ms <= 1500);
}

function renderGrid(container, sample) {
  const fingers = sample?.fingers || ["pinky", "ring", "middle", "index", "thumb"];
  if (container.children.length !== 5 || container.dataset.fingers !== fingers.join(",")) {
    container.replaceChildren();
    container.dataset.fingers = fingers.join(",");
    for (const finger of fingers) {
      const block = document.createElement("div");
      block.className = "finger";
      const name = document.createElement("span");
      name.className = "finger-name";
      name.textContent = finger;
      const cells = document.createElement("div");
      cells.className = "taxels";
      for (let i = 0; i < 16; i++) {
        const cell = document.createElement("span");
        cell.className = "taxel";
        cells.append(cell);
      }
      block.append(name, cells);
      container.append(block);
    }
  }
  const cells = container.querySelectorAll(".taxel");
  cells.forEach((cell, index) => {
    const value = sample?.values?.[index] || 0;
    const denominator = sample?.display_mode === "raw_uncalibrated" ? 4095 : 400;
    cell.style.setProperty("--level", `${Math.min(100, Math.round(value * 100 / denominator))}%`);
    cell.classList.toggle("live", !!sample);
    cell.title = sample ? `${fingers[Math.floor(index / 16)]}: ${value} ADC counts` : "No sample yet";
  });
}

function badge(id, ready, label, bad = false) {
  const element = $(id);
  element.textContent = label;
  element.classList.toggle("ready", ready);
  element.classList.toggle("bad", bad);
}

function renderCalibrationResult(calibration) {
  const signature = calibration?.completed_wall_ns || null;
  if (renderedCalibration === signature) return;
  renderedCalibration = signature;
  const result = $("calibration-result");
  result.hidden = !calibration;
  if (!calibration) return;
  const maps = $("calibration-maps");
  maps.replaceChildren();
  let maximum = 0;
  for (const side of ["left", "right"]) {
    const glove = calibration.gloves?.[side];
    if (!glove || glove.noise?.length !== 80) continue;
    const section = document.createElement("section");
    section.className = "calibration-map";
    const heading = document.createElement("strong");
    heading.textContent = `${side === "left" ? "Left" : "Right"} · ${glove.serial}`;
    const fingers = document.createElement("div");
    fingers.className = "calibration-fingers";
    for (let fingerIndex = 0; fingerIndex < 5; fingerIndex++) {
      const finger = document.createElement("div");
      const name = document.createElement("span");
      name.className = "finger-name";
      name.textContent = glove.fingers?.[fingerIndex] || `Finger ${fingerIndex + 1}`;
      const cells = document.createElement("div");
      cells.className = "calibration-taxels";
      for (let taxelIndex = 0; taxelIndex < 16; taxelIndex++) {
        const spread = Number(glove.noise[fingerIndex * 16 + taxelIndex]);
        maximum = Math.max(maximum, spread);
        const cell = document.createElement("span");
        cell.className = `calibration-taxel ${spread < 50 ? "low" : spread < 150 ? "medium" : "high"}`;
        cell.title = `${name.textContent}: ${spread} ADC counts of sweep spread`;
        cells.append(cell);
      }
      finger.append(name, cells);
      fingers.append(finger);
    }
    section.append(heading, fingers);
    maps.append(section);
  }
  const thresholds = ["left", "right"].map((side) => calibration.gloves?.[side]?.threshold);
  $("calibration-summary").textContent = `Maximum spread: ${maximum} ADC counts. Contact cutoff: ${thresholds.join(" / ")} counts (left / right).`;
}

function renderLive(live) {
  currentLive = live;
  for (const side of ["left", "right"]) {
    const sample = live.gloves?.[side];
    const error = live.errors?.[side];
    const fresh = sample && sample.age_ms <= 1500 && !error;
    const label = error || (fresh ? `${sample.display_mode === "raw_uncalibrated" ? "Raw" : "Calibrated"} · frame ${sample.seq} · peak ${sample.peak} · ${sample.age_ms} ms ago` : "Waiting for live samples");
    $(`setup-${side}-status`).textContent = label;
    $(`cal-live-${side}-status`).textContent = label;
    $(`record-${side}-status`).textContent = label;
    document.querySelectorAll(`.taxel-display[data-hand="${side}"]`).forEach((container) => renderGrid(container, sample));
    $(`${side}-dot`).className = `status-dot${fresh ? " ready" : error ? " bad" : ""}`;
  }
  const cameraFresh = live.camera?.ready && live.camera.age_ms <= 1500;
  const cameraDark = cameraFresh && live.camera.dark;
  badge("camera-live-badge", cameraFresh && !cameraDark,
        live.camera?.error || (cameraDark ? "Image too dark · check OVISION lens/exposure" :
          cameraFresh ? `Live · ${live.camera.age_ms} ms ago` : "Waiting for frames"),
        !!live.camera?.error || cameraDark);
  badge("record-live-badge", liveReady(), liveReady() ? "Camera + both gloves live" : "Check device streams",
        !!live.camera?.error || cameraDark || Object.keys(live.errors || {}).length > 0);
  $("camera-dot").className = `status-dot${cameraFresh && !cameraDark ? " ready" : live.camera?.error || cameraDark ? " bad" : ""}`;
  if (currentStatus) render(currentStatus);
}

function mappingRows() {
  const holder = $("mapping");
  holder.replaceChildren();
  for (const item of actions) {
    const row = document.createElement("div");
    row.className = "mapping-row";
    const label = document.createElement("span");
    label.textContent = item.label;
    const key = document.createElement("kbd");
    key.textContent = map[item.id];
    const button = document.createElement("button");
    button.className = "button subtle";
    button.textContent = learning === item.id ? "Press a button…" : "Change key";
    button.addEventListener("click", () => {
      learning = item.id;
      testing = false;
      mappingRows();
      $("input-status").textContent = `Press the button for ${item.label.toLowerCase()}`;
    });
    row.append(label, key, button);
    holder.append(row);
  }
}

function latestReview(status) {
  return [...status.episodes].reverse().find((episode) => episode.complete &&
    episode.selection === "unreviewed" && episode.gloves?.length === 2);
}

function renderReview(status) {
  const episodes = [...status.episodes].reverse();
  if (!episodes.some((episode) => episode.id === selectedEpisodeId)) {
    selectedEpisodeId = episodes[0]?.id || null;
  }
  $("episode-count").textContent = `${episodes.length} take${episodes.length === 1 ? "" : "s"}`;
  const key = `${JSON.stringify(episodes)}:${selectedEpisodeId}`;
  if (key === renderedReviewKey) return;
  renderedReviewKey = key;
  const list = $("episodes");
  const detail = $("review-detail");
  list.replaceChildren();
  detail.replaceChildren();
  if (!episodes.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "Your first take will appear here.";
    list.append(empty);
    return;
  }
  for (const episode of episodes) {
    const button = document.createElement("button");
    button.className = `review-item${episode.id === selectedEpisodeId ? " active" : ""}`;
    button.setAttribute("aria-pressed", String(episode.id === selectedEpisodeId));
    const title = document.createElement("strong");
    title.textContent = episode.task_description || episode.id;
    const summary = document.createElement("small");
    summary.className = episode.complete ? "" : "bad";
    summary.textContent = episode.complete ?
      `${episode.selection.toUpperCase()} · ${episode.camera?.frames_decoded || 0} frames` : "INCOMPLETE";
    button.append(title, summary);
    button.addEventListener("click", () => {
      selectedEpisodeId = episode.id;
      renderedReviewKey = "";
      renderReview(currentStatus);
    });
    list.append(button);
  }
  const episode = episodes.find((item) => item.id === selectedEpisodeId);
  const head = document.createElement("div");
  head.className = "review-detail-head";
  const title = document.createElement("h2");
  title.textContent = episode.task_description || episode.id;
  const state = document.createElement("small");
  state.className = episode.complete ? "" : "bad";
  state.textContent = episode.complete ? episode.selection.toUpperCase() : "INCOMPLETE";
  head.append(title, state);
  detail.append(head);
  const message = document.createElement("p");
  message.className = `review-message${episode.complete ? "" : " bad"}`;
  message.textContent = episode.complete ?
    `${episode.camera?.width || 0}×${episode.camera?.height || 0} video · ${episode.camera?.frames_decoded || 0} frames · left + right glove` :
    (episode.error || "This take is still being checked.");
  detail.append(message);
  if (episode.complete) {
    const sensorStatus = document.createElement("p");
    sensorStatus.className = "review-message";
    sensorStatus.textContent = episode.delivery_validation?.og_center_postprocessing === "ready" ?
      "OG Center sensor inputs verified. Check visible hands, task, and contact before keeping." :
      "OG Center stereo post processing inputs are incomplete for this take.";
    detail.append(sensorStatus);
  }
  if (!episode.complete || !episode.camera?.video) return;
  const video = document.createElement("video");
  video.controls = true;
  video.preload = "metadata";
  video.poster = `/api/episodes/${encodeURIComponent(episode.id)}/poster`;
  video.src = `/api/episodes/${encodeURIComponent(episode.id)}/video`;
  const playback = document.createElement("p");
  playback.className = "review-message";
  playback.textContent = "Preparing browser playback…";
  video.addEventListener("loadedmetadata", () => {
    playback.textContent = `Ready to play · ${Math.round(video.duration * 10) / 10} seconds`;
  });
  video.addEventListener("error", () => {
    playback.textContent = "Browser playback is unavailable. Download the original video below.";
    playback.classList.add("bad");
  });
  detail.append(video, playback);
  const source = document.createElement("a");
  source.className = "source-link";
  source.href = `/api/episodes/${encodeURIComponent(episode.id)}/source-video`;
  source.textContent = ["ovision_uvc_stereo_host_timed", "ovision_native_stereo"].includes(episode.camera?.kind) ?
    "Download original packed stereo video" : "Download original video";
  detail.append(source);
  const buttons = document.createElement("div");
  buttons.className = "controls";
  for (const selection of ["kept", "discarded"]) {
    const button = document.createElement("button");
    button.className = `button${selection === "discarded" ? " danger" : ""}`;
    button.textContent = selection === "kept" ? "Keep" : "Discard";
    button.disabled = episode.selection === selection;
    button.addEventListener("click", () => perform(`/api/episodes/${episode.id}/selection`, { selection }));
    buttons.append(button);
  }
  detail.append(buttons);
}

function render(status) {
  if (!status || !Array.isArray(status.episodes)) return;
  const previousState = currentStatus?.state;
  currentStatus = status;
  $("state-pill").textContent = status.state.replaceAll("_", " ");
  const pair = pairConnected(status);
  for (const side of ["left", "right"]) {
    const glove = status.gloves.find((item) => item.side === side);
    $(`${side}-device`).textContent = glove ? glove.serial : "Not connected";
    $(`cal-${side}-status`).textContent = glove ?
      (glove.zero_valid ? `Baseline verified · cutoff ${glove.threshold}` : "Needs calibration") : "Not connected";
    $(`cal-${side}-dot`).className = `status-dot${glove?.zero_valid ? " ready" : ""}`;
  }
  $("camera-device").textContent = status.camera ?
    `${status.camera.name || `Index ${status.camera.index}`}${status.camera.eye ? ` · ${status.camera.eye} eye` : ""}${status.camera.size ? ` · ${status.camera.size.join("×")}` : ""}${status.camera.error ? ` · ${status.camera.error}` : ""}` : "Not connected";
  updateConnectButton();
  $("calibrate").disabled = !pair || !["ready", "needs_calibration"].includes(status.state) || busy;

  const calibrated = pair && status.gloves.every((glove) => glove.zero_valid);
  renderCalibrationResult(status.calibration);
  $("reuse-calibration").disabled = !calibrated || busy;
  $("calibration-help").textContent = !calibrated ?
    "Both gloves need a fresh sweep before recording." : calibrationConfirmed ?
      "Calibration chosen for this session. Check fingertip response, then continue." :
      "Saved baselines are available. Choose reuse or run a fresh sweep before continuing.";
  const hasEpisodes = status.episodes.length > 0;
  if (status.state === "recording" || status.state === "finalizing") {
    unlockedStep = Math.max(unlockedStep, 4);
    if (status.state === "recording" && currentStep !== 4) currentStep = 4;
  }
  if (hasEpisodes) unlockedStep = Math.max(unlockedStep, 5);
  const finishedTake = ["finalizing", "recording"].includes(previousState) &&
    ["review", "error"].includes(status.state);
  if (hasEpisodes && finishedTake) {
    selectedEpisodeId = status.episodes[status.episodes.length - 1].id;
    renderedReviewKey = "";
  }
  if (hasEpisodes && (finishedTake || status.state === "review" && currentStep === 4)) {
    currentStep = 5;
  }
  if (pair && !calibrationConfirmed && !hasEpisodes &&
      !["recording", "finalizing", "review"].includes(status.state)) {
    unlockedStep = Math.min(unlockedStep, 2);
    if (currentStep > 2) currentStep = 2;
  }
  if (!pair && status.state === "disconnected") {
    unlockedStep = hasEpisodes ? 5 : 1;
    if (!hasEpisodes) currentStep = 1;
  }
  $("next-1").disabled = !liveReady() || busy;
  $("next-2").disabled = !calibrated || !calibrationConfirmed || busy;

  $("record-state").textContent = {
    disconnected: "Connect both gloves and camera", needs_calibration: "Calibration needed",
    calibrating: "Calibrating both gloves…", ready: "Ready for the next episode",
    recording: "Recording now", finalizing: "Checking recorded files…",
    review: "Review this take", error: "Capture needs attention",
  }[status.state] || status.state;
  $("record-detail").textContent = status.current || status.error || "F9 starts and stops by default";
  $("record-dot").classList.toggle("active", status.state === "recording");
  const profile = $("profile").value;
  const needsNative = profile === "annotation_handoff";
  const needsCenter = profile === "og_center_postprocessing";
  const cameraSupportsNative = !!status.camera?.native_device_timestamps;
  const cameraSupportsCenter = !!status.camera?.postprocessing_capable;
  $("start").disabled = status.state !== "ready" || !calibrationConfirmed ||
    !liveReady() || busy || (needsNative && !cameraSupportsNative) ||
    (needsCenter && !cameraSupportsCenter);
  $("stop").disabled = status.state !== "recording" || busy;
  $("profile-status").textContent = needsCenter ?
    (cameraSupportsCenter ?
      "Native OVISION saves both eyes, camera motion, exposure timing, calibration, and both gloves. Review task content after recording." :
      "OG Center processing needs native OVISION capture on Linux. This camera can still make a source archive.") :
    (needsNative ?
      (cameraSupportsNative ? "Native frame timing is available for this adapter." :
        "This camera has no native frame timestamp. Choose Source archive or connect a native camera adapter.") :
      "The source archive keeps video and both gloves. OVISION preview selection does not crop its saved stereo video.");

  renderReview(status);
  const count = status.episodes.filter((episode) => episode.complete && episode.selection === "kept" &&
    episode.gloves?.length === 2 && episode.delivery_validation?.[profile] === "ready").length;
  $("next-4").disabled = !hasEpisodes || ["recording", "finalizing"].includes(status.state) || busy;
  $("next-5").disabled = count === 0 || busy;
  $("export-count").textContent = `${count} kept episode${count === 1 ? "" : "s"}`;
  const profileLabel = {source_archive: "Source archive", annotation_handoff: "Annotation handoff",
    og_center_postprocessing: "OG Center sensor source"}[profile] || profile;
  $("export-status").textContent = count ? `${profileLabel} ready` : "Nothing to export for this profile";
  $("export").setAttribute("aria-disabled", String(count === 0));
  $("export").style.pointerEvents = count ? "" : "none";
  $("export").style.opacity = count ? "" : ".45";
  if (count) unlockedStep = Math.max(unlockedStep, 6);
  currentStep = Math.min(currentStep, unlockedStep);
  setStep(currentStep);
}

async function refresh() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    render(await response.json());
  } catch (error) { notice(`Cannot reach the local recorder: ${error.message}`, true); }
}

async function refreshLive() {
  try {
    const response = await fetch("/api/live", { cache: "no-store" });
    renderLive(await response.json());
  } catch (_) { /* Status polling reports server loss. */ }
}

function refreshCamera() {
  if (!currentStatus?.camera || ![1, 4].includes(currentStep)) return;
  for (const image of document.querySelectorAll(".camera-preview")) {
    image.onload = () => {
      image.classList.add("active");
      image.parentElement.querySelector(".preview-placeholder").hidden = true;
    };
    image.src = `/api/preview?t=${Date.now()}`;
  }
}

document.addEventListener("keydown", (event) => {
  if (learning) {
    event.preventDefault();
    if (Object.values(map).includes(event.code) && map[learning] !== event.code) {
      $("input-status").textContent = "That key is already assigned. Try another button.";
      return;
    }
    map[learning] = event.code;
    localStorage.setItem(mappingKey, JSON.stringify(map));
    $("input-status").textContent = `Mapped ${event.code}. Test it again before recording.`;
    learning = null;
    mappingRows();
    return;
  }
  if (testing) {
    event.preventDefault();
    $("input-status").textContent = `Received ${event.code}${event.repeat ? " (held/repeating)" : ""}`;
    return;
  }
  if (event.repeat || event.altKey || event.ctrlKey || event.metaKey || ![4, 5].includes(currentStep)) return;
  if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
  const mapped = actions.find((item) => map[item.id] === event.code);
  if (!mapped || !currentStatus || busy) return;
  event.preventDefault();
  if (pressed.has(event.code)) return;
  pressed.add(event.code);
  const last = lastPress.get(event.code) || 0;
  if (Date.now() - last < 400) return;
  lastPress.set(event.code, Date.now());
  const state = currentStatus.state;
  const input_event = { control_id: event.code };
  if (mapped.id === "toggle" && currentStep === 4) {
    if (state === "ready" && !$("start").disabled) perform("/api/start", {
      task: $("task").value, source: "keyboard", profile: $("profile").value, mapping: map, input_event,
    });
    if (state === "recording") perform("/api/stop", { source: "keyboard", input_event });
  }
  if (mapped.id === "confirm") {
    if (state === "ready" && currentStep === 4) {
      setStep(2);
      $("calibrate").click();
    }
    if (state === "review") {
      const episode = latestReview(currentStatus);
      if (episode) perform(`/api/episodes/${episode.id}/selection`, {
        selection: "kept", source: "keyboard", input_event,
      });
    }
  }
  if (mapped.id === "discard" && state === "review") {
    const episode = latestReview(currentStatus);
    if (episode) perform(`/api/episodes/${episode.id}/selection`, {
      selection: "discarded", source: "keyboard", input_event,
    });
  }
});
document.addEventListener("keyup", (event) => pressed.delete(event.code));
window.addEventListener("blur", () => {
  pressed.clear();
  if (currentStatus?.state === "recording") perform("/api/stop", { source: "focus_loss" });
});
window.addEventListener("pagehide", () => {
  if (currentStatus?.state === "recording") navigator.sendBeacon("/api/stop",
    new Blob([JSON.stringify({ source: "focus_loss" })], { type: "application/json" }));
});

document.querySelectorAll(".step-button").forEach((button) =>
  button.addEventListener("click", () => setStep(Number(button.dataset.step))));
document.querySelectorAll(".back").forEach((button) =>
  button.addEventListener("click", () => setStep(Number(button.dataset.back))));
$("next-1").addEventListener("click", () => { unlockedStep = Math.max(unlockedStep, 2); setStep(2); });
$("next-2").addEventListener("click", () => { unlockedStep = Math.max(unlockedStep, 3); setStep(3); });
$("next-3").addEventListener("click", () => { unlockedStep = Math.max(unlockedStep, 4); setStep(4); });
$("next-4").addEventListener("click", () => { unlockedStep = Math.max(unlockedStep, 5); setStep(5); });
$("next-5").addEventListener("click", () => { unlockedStep = 6; setStep(6); });
for (const id of ["camera-index", "left-port", "right-port"]) {
  $(id).addEventListener("change", updateConnectButton);
}
$("refresh-devices").addEventListener("click", discoverDevices);
$("connect").addEventListener("click", async () => {
  calibrationConfirmed = false;
  const camera = JSON.parse($("camera-index").value);
  await perform("/api/connect", {
    camera_index: camera.index, camera_mode: camera.mode, camera_name: camera.name,
    left_port: $("left-port").value, right_port: $("right-port").value,
  });
  await discoverDevices();
});
$("reuse-calibration").addEventListener("click", () => {
  if (!currentStatus?.gloves?.every((glove) => glove.zero_valid) || busy) return;
  calibrationConfirmed = true;
  notice("Saved calibration selected for both gloves. Check fingertip response before continuing.");
  render(currentStatus);
});
$("calibrate").addEventListener("click", async () => {
  if (busy) return;
  const thresholdText = $("calibration-threshold").value.trim();
  const threshold = Number(thresholdText);
  if (!thresholdText || !Number.isInteger(threshold) || threshold < 0 || threshold > 500) {
    notice("Enter a contact threshold from 0 to 500 ADC counts.", true);
    $("calibration-threshold").focus();
    return;
  }
  calibrationConfirmed = false;
  busy = true;
  renderCalibrationResult(null);
  const overlay = $("calibration-overlay");
  const countdown = $("calibration-countdown");
  const started = performance.now();
  const tick = () => {
    const seconds = Math.ceil(5 - (performance.now() - started) / 1000);
    countdown.textContent = seconds > 0 ? String(seconds) : "Verifying…";
  };
  overlay.hidden = false;
  tick();
  const timer = setInterval(tick, 100);
  try {
    const status = await request("/api/calibrate", { threshold });
    calibrationConfirmed = status.gloves?.length === 2 &&
      status.gloves.every((glove) => glove.zero_valid) &&
      ["left", "right"].every((side) => status.calibration?.gloves?.[side]?.noise?.length === 80);
    render(status);
    notice(calibrationConfirmed ? "Sweep saved and verified for both gloves." :
      "Sweep finished, but the saved calibration could not be verified.", !calibrationConfirmed);
  } catch (error) {
    notice(`Calibration failed: ${error.message}`, true);
    await refresh();
  } finally {
    clearInterval(timer);
    overlay.hidden = true;
    busy = false;
    if (currentStatus) render(currentStatus);
  }
});
$("profile").addEventListener("change", () => { if (currentStatus) render(currentStatus); });
$("start").addEventListener("click", () => perform("/api/start", {
  task: $("task").value, profile: $("profile").value, mapping: map,
}));
$("stop").addEventListener("click", () => perform("/api/stop", { source: "onscreen" }));
$("test-input").addEventListener("click", () => {
  testing = !testing;
  learning = null;
  $("test-input").textContent = testing ? "Finish test" : "Connect / test USB button";
  $("input-status").textContent = testing ? "Press any button now" : "Input test complete";
  mappingRows();
});
$("export").addEventListener("click", async (event) => {
  event.preventDefault();
  if ($("export").getAttribute("aria-disabled") === "true" || busy) return;
  busy = true;
  try {
    const response = await fetch("/api/export", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile: $("profile").value }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || "Export failed");
    const download = document.createElement("a");
    download.href = result.download;
    download.download = result.filename;
    document.body.append(download);
    download.click();
    download.remove();
    notice("Dataset ZIP downloaded.");
  } catch (error) { notice(error.message, true); }
  finally { busy = false; }
});

mappingRows();
setStep(1);
refresh();
discoverDevices();
refreshLive();
setInterval(refresh, 800);
setInterval(refreshLive, 250);
setInterval(refreshCamera, 500);
