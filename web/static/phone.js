"use strict";

const el = id => document.getElementById(id);
const video = el("preview");
const canvas = document.createElement("canvas");
const context = canvas.getContext("2d");
const hasCamera = Boolean(navigator.mediaDevices?.getUserMedia);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let token = "";
let previous = null;
let facing = "user";
let cameraWanted = false;
let cameraVersion = 0;
let modeVersion = 0;
let changingMode = false;
let frameRequest = null;
let wakeLock = null;
let flashTimer;

el("https-help").hidden = hasCamera;
el("camera").disabled = !hasCamera;
el("flip").disabled = true;
try {
  el("name").value = localStorage.getItem("caree.name") || "";
  el("box").value = localStorage.getItem("caree.box") || "";
} catch { /* Storage is optional in private browsing. */ }

function message(id, value) {
  if (el(id).textContent !== value) el(id).textContent = value;
}

function setTracking(seen) {
  el("tracking").classList.toggle("seen", seen);
  message("tracking", seen ? "I can see you" : "Step into view");
}

function render(status) {
  if (previous && previous.mode === status.mode) {
    const flash = status.partial > previous.partial ? "partial" : status.reps > previous.reps ? "rep" : "";
    if (flash) {
      document.body.dataset.flash = flash;
      clearTimeout(flashTimer);
      flashTimer = setTimeout(() => delete document.body.dataset.flash, 300);
    }
  }
  previous = status;
  message("student", status.name);
  message("reps", String(status.reps));
  message("partial", String(status.partial));
  message("stage", status.stage || "waiting");
  message("angle", status.angle == null ? "—" : status.angle + "°");
  message("device", status.box || "None");
  for (const mode of ["arms", "legs"]) {
    el(mode).setAttribute("aria-pressed", String(mode === status.mode));
  }
  setTracking(cameraWanted && Boolean(status.tracking));
  message("exercise-error", status.error || "");
}

function stopCapture() {
  ++cameraVersion;
  frameRequest?.abort();
  video.srcObject?.getTracks().forEach(track => track.stop());
  video.srcObject = null;
  video.hidden = true;
  if (wakeLock) wakeLock.release().catch(() => {});
  wakeLock = null;
  setTracking(false);
  el("camera").textContent = cameraWanted ? "Stop camera" : "Start camera";
  el("flip").disabled = !cameraWanted;
}

function backToJoin(error = "") {
  token = "";
  cameraWanted = false;
  stopCapture();
  previous = null;
  changingMode = false;
  el("arms").disabled = el("legs").disabled = false;
  el("exercise").hidden = el("leave").hidden = true;
  el("join").hidden = false;
  message("join-error", error);
  el("code").focus();
}

async function api(path, options = {}) {
  const sentToken = token;
  const response = await fetch("/api/phone/" + path, {
    ...options, headers: { "X-Phone-Token": sentToken, ...options.headers },
  });
  if (response.status === 401 && sentToken && sentToken === token) {
    backToJoin("Your session ended. Enter the game code to join again.");
    throw new Error("Session ended.");
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "The request failed. Try again.");
  return data;
}

function json(method, body) {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

el("form").addEventListener("submit", async event => {
  event.preventDefault();
  const name = el("name").value.trim();
  const box = el("box").value.trim().toUpperCase();
  el("submit").disabled = true;
  el("submit").textContent = "Joining…";
  message("join-error", "");
  try {
    const status = await api("join", json("POST", { code: el("code").value, name, box }));
    token = status.token;
    facing = "user";
    try {
      localStorage.setItem("caree.name", name);
      localStorage.setItem("caree.box", box);
    } catch { /* The current session works without storage. */ }
    render(status);
    el("join").hidden = true;
    el("exercise").hidden = el("leave").hidden = false;
    el("student").focus();
  } catch (error) {
    message("join-error", error.message);
  } finally {
    el("submit").disabled = false;
    el("submit").textContent = "Join";
  }
});

async function keepAwake(version) {
  if (!navigator.wakeLock) return;
  try {
    const lock = await navigator.wakeLock.request("screen");
    if (version === cameraVersion && !document.hidden) wakeLock = lock;
    else await lock.release();
  } catch { /* Some browsers or power-saving modes decline wake locks. */ }
}

async function sendFrames(version) {
  while (version === cameraVersion && !document.hidden) {
    if (video.readyState < 2 || !video.videoWidth || changingMode) {
      await delay(50);
      continue;
    }
    const sentMode = modeVersion;
    try {
      const scale = Math.min(1, 640 / Math.max(video.videoWidth, video.videoHeight));
      const width = Math.max(1, Math.round(video.videoWidth * scale));
      const height = Math.max(1, Math.round(video.videoHeight * scale));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      context.drawImage(video, 0, 0, width, height);
      const frame = await new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", 0.7));
      if (version !== cameraVersion || document.hidden) return;
      if (!frame) throw new Error("Could not capture the camera frame.");
      frameRequest = new AbortController();
      const status = await api("frame", {
        method: "POST", headers: { "Content-Type": "image/jpeg" }, body: frame, signal: frameRequest.signal,
      });
      if (version !== cameraVersion) return;
      if (sentMode === modeVersion && !changingMode) render(status);
      await delay(status.error ? 1000 : 0);
    } catch (error) {
      if (version !== cameraVersion) return;
      setTracking(false);
      message("exercise-error", error.message + " Retrying…");
      await delay(1000);
    }
  }
}

async function startCapture() {
  stopCapture();
  const version = cameraVersion;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: { facingMode: facing } });
    if (version !== cameraVersion || document.hidden) {
      stream.getTracks().forEach(track => track.stop());
      return;
    }
    video.srcObject = stream;
    video.style.transform = facing === "user" ? "scaleX(-1)" : "";
    video.hidden = false;
    await video.play();
    if (version !== cameraVersion) return;
    message("exercise-error", "");
    keepAwake(version);
    await sendFrames(version);
  } catch (error) {
    if (version !== cameraVersion) return;
    cameraWanted = false;
    stopCapture();
    message("exercise-error", "Could not start the camera. " + error.message);
  }
}

el("camera").onclick = () => {
  cameraWanted = !cameraWanted;
  if (cameraWanted) startCapture();
  else stopCapture();
};
el("flip").onclick = () => {
  facing = facing === "user" ? "environment" : "user";
  startCapture();
};
document.addEventListener("visibilitychange", () => {
  if (!cameraWanted) return;
  if (document.hidden) stopCapture();
  else startCapture();
});

for (const mode of ["arms", "legs"]) {
  el(mode).onclick = async () => {
    if (mode === previous?.mode) return;
    const sentToken = token;
    changingMode = el("arms").disabled = el("legs").disabled = true;
    ++modeVersion;
    try {
      const status = await api("mode", json("PATCH", { mode }));
      if (sentToken === token) render(status);
    } catch (error) {
      if (sentToken === token) message("exercise-error", error.message);
    } finally {
      if (sentToken === token) changingMode = el("arms").disabled = el("legs").disabled = false;
    }
  };
}

el("leave").onclick = async () => {
  cameraWanted = false;
  stopCapture();
  el("leave").disabled = true;
  try {
    await api("leave", { method: "POST" });
    backToJoin();
  } catch (error) {
    if (token) message("exercise-error", error.message + " Tap Leave to try again.");
  } finally {
    el("leave").disabled = false;
  }
};
