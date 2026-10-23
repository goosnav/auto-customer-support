"use strict";

(() => {
  const $ = (selector) => document.querySelector(selector);
  const uniqueId = () => typeof crypto.randomUUID === "function" ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("");
  const embedded = new URLSearchParams(location.search).get("embedded") === "1";
  let parentOrigin = null;
  try { const candidate = new URLSearchParams(location.search).get("parent_origin") || document.referrer; if (candidate) { const url = new URL(candidate); if (["http:", "https:"].includes(url.protocol)) parentOrigin = url.origin; } } catch (_) { /* Standalone chat needs no parent. */ }
  document.body.classList.toggle("embedded", embedded);
  $("#close-widget").hidden = !embedded;
  let session = null;
  let config = { business_name: "Customer support", greeting: "", voice_enabled: false };
  let sending = false;
  let recording = null;
  let recordingTimer = null;
  let audioStream = null;
  let discardRecording = false;
  let pollTimer = null;
  let lastMessages = "";
  let lastAudio = null;
  const key = "auto_support_customer_session";
  function node(tag, value, className = "") { const element = document.createElement(tag); element.textContent = value; element.className = className; return element; }
  function showError(message) { $("#chat-error").textContent = message; $("#chat-error").hidden = !message; }
  function save() { try { if (session) sessionStorage.setItem(key, JSON.stringify(session)); else sessionStorage.removeItem(key); } catch (_) { /* In-memory conversations still work when storage is unavailable. */ } }
  async function request(path, options = {}, authorized = true) {
    const headers = new Headers(options.headers || {});
    if (authorized && session?.token) headers.set("Authorization", `Bearer ${session.token}`);
    if (options.body && !(options.body instanceof FormData)) { headers.set("Content-Type", "application/json"); options.body = JSON.stringify(options.body); }
    const response = await fetch(`/api${path}`, { ...options, headers, credentials: "same-origin" });
    const data = await response.json();
    if (!response.ok) {
      let detail = data.detail || data.error || "We couldn’t complete that request. Please try again.";
      if (Array.isArray(detail)) detail = detail.map((item) => item.msg || "Check your information.").join(" ");
      const error = new Error(typeof detail === "string" ? detail : "We couldn’t complete that request. Please try again.");
      error.status = response.status;
      throw error;
    }
    return data;
  }
  function updateVoiceButton() {
    const available = config.voice_enabled && navigator.mediaDevices?.getUserMedia && window.MediaRecorder;
    $("#voice-button").disabled = !available || sending;
    $("#voice-button").classList.toggle("recording", !!recording);
    $("#voice-label").textContent = recording ? "Stop recording" : available ? "Speak instead" : "Voice unavailable";
  }
  function updateBusy(value) {
    sending = value;
    $("#send-button").disabled = value;
    $("#new-conversation").disabled = value || !!recording;
    $("#message").disabled = value;
    $("#send-button").textContent = value ? "Working…" : "Send ↗";
    updateVoiceButton();
    let typing = $("#typing");
    if (value && !typing) { typing = node("div", "Checking your question…", "typing"); typing.id = "typing"; $("#messages").append(typing); $("#messages").scrollTop = $("#messages").scrollHeight; }
    else if (!value && typing) typing.remove();
  }
  function displayMessage(message) {
    const customer = ["user", "customer"].includes(message.role);
    const team = ["owner", "human"].includes(message.role);
    const entry = document.createElement("article");
    entry.className = `message ${customer ? "customer" : team ? "team" : "assistant"}`;
    entry.append(node("div", customer ? "YOU" : team ? "THE TEAM" : "SUPPORT", "message-label"), node("div", message.content || message.text || "", "message-body"));
    if (message.sources?.length) entry.append(node("div", `Based on: ${message.sources.map((source) => source.title || "our published information").join(" · ")}`, "message-sources"));
    return entry;
  }
  function renderMessages(messages) {
    const signature = JSON.stringify(messages);
    if (signature === lastMessages) return;
    const container = $("#messages");
    const nearBottom = container.scrollTop + container.clientHeight >= container.scrollHeight - 90;
    lastMessages = signature;
    container.replaceChildren(...messages.map(displayMessage));
    if (!messages.length) container.append(displayMessage({ role: "assistant", content: config.greeting || "Hi. What can I help you with?" }));
    if (nearBottom || sending) container.scrollTop = container.scrollHeight;
    if (sending) updateBusy(true);
  }
  async function syncMessages() {
    if (!session) return;
    const data = await request(`/conversations/${encodeURIComponent(session.id)}/messages`);
    renderMessages(data.messages || []);
  }
  function startPolling() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      if (session && !document.hidden && !sending) {
        try { await syncMessages(); } catch (error) { if ([401, 403, 404].includes(error.status)) { resetSession(); showError("This conversation is no longer available. Please start a new one."); return; } }
      }
      if (session) startPolling();
    }, 8000);
  }
  function showConversation() {
    $("#welcome").hidden = true;
    $("#conversation").hidden = false;
    $("#contact-form").hidden = !!session?.email || !session?.needs_human;
    updateVoiceButton();
    startPolling();
    $("#message").focus();
  }
  function resetSession() {
    session = null;
    save();
    lastMessages = "";
    clearTimeout(pollTimer);
    $("#messages").replaceChildren();
    $("#welcome").hidden = false;
    $("#conversation").hidden = true;
    $("#contact-form").hidden = true;
    $("#message").value = "";
    showError("");
    if (lastAudio) { lastAudio.pause(); lastAudio = null; }
  }
  function handleReply(data) {
    if (data.needs_human) { session.needs_human = true; save(); $("#contact-form").hidden = !!session.email; }
    if (data.audio_url) {
      try {
        const url = new URL(data.audio_url, location.origin);
        if (url.origin !== location.origin || !url.pathname.startsWith("/api/audio/")) return;
        if (lastAudio) lastAudio.pause();
        const audio = document.createElement("audio");
        audio.controls = true;
        audio.preload = "none";
        audio.src = url.href;
        audio.setAttribute("aria-label", "Listen to the support answer");
        const messages = $("#messages").querySelectorAll(".message:not(.customer)");
        messages[messages.length - 1]?.append(audio);
        lastAudio = audio;
        audio.play().catch(() => { /* Browser can require an additional tap; controls remain available. */ });
      } catch (_) { /* Invalid playback URL does not prevent the text response. */ }
    }
  }
  $("#start-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const control = $("#start-button");
    control.disabled = true;
    showError("");
    try {
      const name = $("#customer-name").value.trim();
      const email = $("#customer-email").value.trim();
      const data = await request("/conversations", { method: "POST", body: { name: name || undefined, email: email || undefined } }, false);
      session = { id: data.id, token: data.token, email, name, needs_human: false };
      save();
      showConversation();
      await syncMessages();
    } catch (error) { showError(error.message); } finally { control.disabled = false; }
  });
  $("#message-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = $("#message").value.trim();
    if (!message || !session || sending || recording) return;
    showError("");
    if (lastAudio) lastAudio.pause();
    updateBusy(true);
    try {
      if (session.pending?.message !== message) { session.pending = { message, id: uniqueId() }; save(); }
      const result = await request(`/conversations/${encodeURIComponent(session.id)}/messages`, { method: "POST", body: { message, client_message_id: session.pending.id } });
      $("#message").value = "";
      delete session.pending; save();
      await syncMessages();
      handleReply(result);
    } catch (error) { showError(error.message); } finally { updateBusy(false); $("#message").focus(); }
  });
  $("#message").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("#message-form").requestSubmit(); } });
  $("#contact-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!session) return;
    const control = event.currentTarget.querySelector("button");
    control.disabled = true;
    showError("");
    try {
      const email = $("#followup-email").value.trim();
      await request(`/conversations/${encodeURIComponent(session.id)}`, { method: "PATCH", body: { email } });
      session.email = email;
      save();
      $("#contact-form").hidden = true;
    } catch (error) { showError(error.message); } finally { control.disabled = false; }
  });
  $("#new-conversation").addEventListener("click", resetSession);
  function stopCapture() {
    if (lastAudio) lastAudio.pause();
    if (recording && recording.state !== "inactive") { discardRecording = true; recording.stop(); }
  }
  function closeWidget() { stopCapture(); if (embedded && parentOrigin) parent.postMessage({ type: "auto-support-close" }, parentOrigin); }
  $("#close-widget").addEventListener("click", closeWidget);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && embedded) closeWidget(); });
  window.addEventListener("message", (event) => { if (embedded && event.source === parent && event.origin === parentOrigin && event.data?.type === "auto-support-hidden") stopCapture(); });
  async function uploadRecording(blob) {
    if (!session || !blob.size) { showError("No audio was captured. Please try again or type your question."); return; }
    updateBusy(true);
    $("#voice-status").textContent = "Transcribing and preparing your answer…";
    $("#voice-status").hidden = false;
    try {
      const form = new FormData();
      const extension = blob.type.includes("mp4") ? "mp4" : blob.type.includes("ogg") ? "ogg" : "webm";
      form.append("audio", blob, `question.${extension}`);
      const result = await request(`/conversations/${encodeURIComponent(session.id)}/voice`, { method: "POST", body: form });
      await syncMessages();
      handleReply(result);
    } catch (error) { showError(error.message || "Voice is unavailable. Please type your question."); } finally { $("#voice-status").hidden = true; updateBusy(false); }
  }
  $("#voice-button").addEventListener("click", async () => {
    if (recording) { if (recording.state !== "inactive") recording.stop(); return; }
    if (!session || sending) return;
    showError("");
    try {
      if (lastAudio) lastAudio.pause();
      discardRecording = false;
      audioStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      const preferred = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"].find((type) => MediaRecorder.isTypeSupported(type));
      recording = new MediaRecorder(audioStream, preferred ? { mimeType: preferred } : undefined);
      const current = recording;
      const chunks = [];
      current.addEventListener("dataavailable", (event) => { if (event.data.size) chunks.push(event.data); });
      current.addEventListener("stop", () => {
        clearTimeout(recordingTimer);
        audioStream?.getTracks().forEach((track) => track.stop());
        audioStream = null;
        recording = null;
        if (discardRecording) { discardRecording = false; $("#voice-status").hidden = true; updateBusy(false); return; }
        updateVoiceButton();
        uploadRecording(new Blob(chunks, { type: current.mimeType || "audio/webm" }));
      });
      current.addEventListener("error", () => { clearTimeout(recordingTimer); audioStream?.getTracks().forEach((track) => track.stop()); audioStream = null; recording = null; $("#voice-status").hidden = true; updateBusy(false); showError("Recording failed. Please try again or type your question."); });
      current.start();
      recordingTimer = setTimeout(() => { if (current.state !== "inactive") current.stop(); }, 30000);
      $("#voice-status").textContent = "Listening. Tap Stop recording when you’re finished. Up to 30 seconds.";
      $("#voice-status").hidden = false;
      $("#new-conversation").disabled = true;
      $("#send-button").disabled = true;
      updateVoiceButton();
    } catch (error) {
      audioStream?.getTracks().forEach((track) => track.stop());
      audioStream = null;
      recording = null;
      $("#voice-status").hidden = true;
      updateBusy(false);
      showError(error.name === "NotAllowedError" ? "Microphone access was declined. You can type your question instead." : "This browser could not start the microphone. Use HTTPS or type your question.");
    }
  });
  window.addEventListener("pagehide", () => { clearTimeout(pollTimer); clearTimeout(recordingTimer); audioStream?.getTracks().forEach((track) => track.stop()); if (lastAudio) lastAudio.pause(); });
  (async () => {
    try {
      config = await request("/config", {}, false);
      $("#business-name").textContent = config.business_name || "Customer support";
      document.title = `${config.business_name || "Customer"} support`;
      if (config.greeting) $("#greeting").textContent = config.greeting;
      try { const stored = sessionStorage.getItem(key); if (stored) { const parsed = JSON.parse(stored); if (typeof parsed.id === "string" && typeof parsed.token === "string") session = parsed; } } catch (_) { /* Ignore unavailable or corrupt local state. */ }
      if (session) { showConversation(); try { await syncMessages(); } catch (_) { resetSession(); } }
      updateVoiceButton();
    } catch (error) { showError("Support is temporarily unavailable. Please try again shortly."); $("#start-button").disabled = true; }
  })();
})();
