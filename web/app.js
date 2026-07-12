/*
 * J.A.R.V.I.S. web client — Holographic HUD showpiece.
 *
 * - VOICE INPUT is platform-aware:
 *     * DESKTOP: STREAMING STT via Web Speech API (webkitSpeechRecognition): tap
 *       mic to start, interim transcript shows live, final transcript is sent as
 *       {"type":"text", "text": final}. Also powers the hands-free WAKE word mode.
 *     * MOBILE: Default path records with MediaRecorder and sends audio as a BINARY
 *       WebSocket frame. The server transcribes with Whisper (mlx-whisper) and
 *       replies with {"type":"transcript"} + reply + mp3.
 *       TEST FLAG: MOBILE_USE_WEBSPEECH=true routes mobile tap-to-talk through the
 *       same Web Speech path as desktop (set false to revert to Whisper).
 *       Wake word is desktop-only (continuous Web Speech), always hidden on mobile.
 * - WebSocket to ws(s)://<host>/ws (wss on https — Tailscale ready) w/ reconnect.
 * - Incoming binary frame = mp3 audio: decoded via WebAudio, routed through an
 *   AnalyserNode that drives the orb's SPEAKING flares.
 * - Incoming JSON: transcript / reply / actions / approval / audio_incoming / status.
 * - Orb state machine: IDLE / LISTENING / THINKING / SPEAKING.
 */
(function () {
  "use strict";

  // ----- Platform detection (mobile vs desktop) -----
  // Mobile gets the MediaRecorder→server→Whisper path (Web Speech is broken on
  // Android Chrome / installed PWAs). Desktop keeps the live Web Speech path.
  const IS_MOBILE =
    (navigator.userAgentData && navigator.userAgentData.mobile === true) ||
    /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent) ||
    (window.matchMedia && window.matchMedia("(pointer: coarse)").matches);

  // ----- Mobile Web Speech test toggle -----
  // Set true  → mobile tap-to-talk uses Chrome Web Speech (same as desktop).
  // Set false → mobile tap-to-talk uses MediaRecorder→server→Whisper (original).
  // Web Speech must actually be available; if not, falls back to Whisper regardless.
  const MOBILE_USE_WEBSPEECH = false;  // mobile mic uses MediaRecorder→server Whisper (reliable on Android/Incognito/PWA). Web Speech is desktop-only.

  // USE_RECORDER = true  → MediaRecorder/Whisper path
  //              = false → Web Speech path
  // (Resolved after SR is declared below; see USE_RECORDER_FINAL for the runtime flag.)

  // ----- DOM -----
  const body = document.body;
  const canvas = document.getElementById("orb");
  const logEl = document.getElementById("log");
  const approvalsEl = document.getElementById("approvals");
  const micBtn = document.getElementById("mic");
  const micLabel = document.getElementById("micLabel");
  const interimEl = document.getElementById("interim");
  const statusEl = document.getElementById("status");
  const statusText = document.getElementById("statusText");
  const stateLabel = document.getElementById("stateLabel");
  const textInput = document.getElementById("textInput");
  const textSend = document.getElementById("textSend");
  const wakeToggle = document.getElementById("wakeToggle");
  const wakeStateEl = document.getElementById("wakeState");
  const telCore = document.getElementById("telCore");
  const telBar = document.getElementById("telBar");
  const telPwr = document.getElementById("telPwr");
  const telClock = document.getElementById("telClock");

  // ----- Single-active-session state (BUG 1) -----
  // Only ONE Jarvis tab/PWA may be the live voice session at a time. The server
  // sends {"type":"session","active":bool}. An INACTIVE tab must never play
  // audio and never auto-trigger wake — otherwise one tab's spoken reply is
  // heard by another's mic → double-talk. Default FALSE (BUG 1): the client
  // stays muted until the server explicitly grants it active with
  // {"type":"session","active":true} (which now only happens when this tab
  // takes a turn). This stops a reconnecting/flapping tab from talking.
  let sessionActive = false;
  // Remembers whether wake was ON before this tab got muted, so taking over can
  // restore it. (We always show the persisted state via localStorage anyway.)
  let wakeWasOnBeforeMute = false;

  // ----- Orb -----
  const orb = window.initOrb
    ? window.initOrb(canvas)
    : { setAnalyser() {}, setMicAnalyser() {}, setState() {}, getLevel() { return 0; } };

  // ----- HUD state machine -----
  let hudState = "idle";
  function setHudState(s) {
    hudState = s;
    body.setAttribute("data-state", s);
    stateLabel.textContent = s.toUpperCase();
    orb.setState(s);
  }
  setHudState("idle");

  // ----- Audio playback (WebAudio) -----
  let audioCtx = null;
  let voiceAnalyser = null;
  let currentAudioSrc = null;   // active TTS BufferSource (so we can hard-stop it)
  // Streaming voice: the server now sends MULTIPLE audio chunks per turn (one per
  // sentence). We queue decoded buffers and play them back-to-back, then finalize
  // when the server signals {type:"audio_end"}. This is what makes the first words
  // land ~1s after you stop talking instead of after the whole reply is built.
  let audioQueue = [];          // decoded AudioBuffers waiting to play, in order
  let queuePlaying = false;     // is a chunk currently playing?
  let turnAudioComplete = false; // has the server sent audio_end for this turn?
  let pendingDecodes = 0;       // mp3 chunks still being async-decoded (race guard vs audio_end)
  // Echo guard: true while TTS is playing; after it ends we keep ignoring wake
  // results until `echoGuardUntil` (a short tail) so Jarvis can't trigger itself.
  let isSpeaking = false;
  let echoGuardUntil = 0;
  // Web Speech finalizes buffered audio 1–3s late, so the tail must outlast that
  // or Jarvis's own sentence finalizes after the guard and fires as a fake command.
  const ECHO_GUARD_TAIL_MS = 1500;
  function echoGuardActive() {
    return isSpeaking || Date.now() < echoGuardUntil;
  }
  function ensureAudio() {
    if (!audioCtx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      audioCtx = new AC();
      voiceAnalyser = audioCtx.createAnalyser();
      voiceAnalyser.fftSize = 256;
      voiceAnalyser.smoothingTimeConstant = 0.72;
      voiceAnalyser.connect(audioCtx.destination);
    }
    if (audioCtx.state === "suspended") audioCtx.resume();
    return audioCtx;
  }

  // Decode an incoming mp3 chunk and ENQUEUE it. A muted tab drops the frame.
  async function playMp3(arrayBuf) {
    if (!sessionActive) return;
    pendingDecodes++;                         // mark an in-flight decode (audio_end must wait for it)
    try {
      ensureAudio();
      const buf = await audioCtx.decodeAudioData(arrayBuf.slice(0));
      if (!sessionActive) return;            // state may have flipped during decode
      audioQueue.push(buf);
      if (!queuePlaying) startQueuePlayback();
    } catch (e) {
      console.error("[app] mp3 decode failed", e);
    } finally {
      pendingDecodes = Math.max(0, pendingDecodes - 1);
      // Race fix: if this was the last in-flight chunk, the turn is already marked
      // done, and nothing is playing/queued — finalize now (audio_end arrived while
      // we were still decoding this chunk).
      if (pendingDecodes === 0 && turnAudioComplete && !queuePlaying && audioQueue.length === 0) {
        finalizeSpeaking();
      }
    }
  }

  // Play queued TTS chunks back-to-back. Re-invoked on each chunk's onended and
  // whenever a new chunk arrives while idle.
  function startQueuePlayback() {
    if (!sessionActive) { audioQueue = []; queuePlaying = false; return; }
    const buf = audioQueue.shift();
    if (!buf) {
      queuePlaying = false;
      if (turnAudioComplete) finalizeSpeaking();   // nothing left + turn done
      return;
    }
    queuePlaying = true;
    // Enter speaking state (echo guard ON, mic freed) — idempotent across chunks.
    isSpeaking = true;
    stopWakeRecognizer();
    setHudState("speaking");
    setStatus("speaking", "live");
    orb.setAnalyser(voiceAnalyser);
    try {
      const src = audioCtx.createBufferSource();
      src.buffer = buf;
      src.connect(voiceAnalyser);
      currentAudioSrc = src;
      src.onended = () => {
        currentAudioSrc = null;
        if (audioQueue.length > 0) {
          startQueuePlayback();                // next sentence, gaplessly
        } else {
          queuePlaying = false;
          if (turnAudioComplete) finalizeSpeaking();  // else wait for more / audio_end
        }
      };
      src.start(0);
    } catch (e) {
      console.error("[app] chunk play failed", e);
      currentAudioSrc = null;
      if (audioQueue.length > 0) startQueuePlayback();
      else { queuePlaying = false; if (turnAudioComplete) finalizeSpeaking(); }
    }
  }

  // The turn's audio is fully played — return to idle + restart wake.
  function finalizeSpeaking() {
    orb.setAnalyser(null);
    isSpeaking = false;
    echoGuardUntil = Date.now() + ECHO_GUARD_TAIL_MS;  // keep ignoring wake briefly
    setHudState("idle");
    setStatus("ready", "live");
    commandTurnActive = false;
    turnAudioComplete = false;
    queuePlaying = false;
    // TTS tail may have polluted the wake recognizer's buffer — restart it clean.
    scheduleWakeRestart(ECHO_GUARD_TAIL_MS + 80);
  }

  // ----- Mic-level analyser (drives LISTENING reaction) -----
  let micStream = null;
  let micAnalyser = null;
  async function startMicMeter() {
    try {
      ensureAudio();
      if (!micStream) {
        micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      }
      if (!micAnalyser) {
        micAnalyser = audioCtx.createAnalyser();
        micAnalyser.fftSize = 256;
        micAnalyser.smoothingTimeConstant = 0.7;
        const srcNode = audioCtx.createMediaStreamSource(micStream);
        srcNode.connect(micAnalyser); // not connected to destination = no echo
      }
      orb.setMicAnalyser(micAnalyser);
    } catch (e) {
      // mic meter is a nice-to-have; recognition still works without it
      console.warn("[app] mic meter unavailable", e);
    }
  }
  function stopMicMeter() {
    orb.setMicAnalyser(null);
    micStream?.getTracks().forEach(t => t.stop());
    micStream = null;
    // Drop the analyser too so the next startMicMeter rebuilds the source node
    // against the freshly acquired stream (a stopped stream's node is dead).
    micAnalyser = null;
  }

  // ----- Logging UI -----
  function addMsg(role, text) {
    const div = document.createElement("div");
    div.className = "msg " + role;
    const r = document.createElement("div");
    r.className = "role";
    r.textContent = role === "user" ? "You" : role === "jarvis" ? "Jarvis" : "System";
    const b = document.createElement("div");
    b.textContent = text;
    div.appendChild(r);
    div.appendChild(b);
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
    return div;
  }

  function addActions(msgDiv, actions) {
    if (!actions || !actions.length) return;
    const wrap = document.createElement("div");
    wrap.className = "actions";
    actions.forEach((a) => {
      const el = document.createElement("div");
      el.className = "action";
      const tool = a.tool || "tool";
      const input = (a.input || "").toString();
      el.innerHTML = "<b>" + escapeHtml(tool) + "</b>";
      if (input) el.appendChild(document.createTextNode("  " + truncate(input, 220)));
      wrap.appendChild(el);
    });
    msgDiv.appendChild(wrap);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }
  function truncate(s, n) { return s.length > n ? s.slice(0, n) + "…" : s; }

  function setStatus(text, cls) {
    statusText.textContent = text;
    statusEl.className = cls || "";
  }

  // ----- Session banner (shown when THIS tab is muted by another, BUG 1) -----
  // Created in JS so index.html doesn't need editing; styled via #sessionBanner
  // in style.css (with a sensible inline fallback if the rule is missing).
  let sessionBanner = null;
  function ensureBanner() {
    if (sessionBanner) return sessionBanner;
    sessionBanner = document.createElement("div");
    sessionBanner.id = "sessionBanner";
    sessionBanner.setAttribute("role", "status");
    sessionBanner.hidden = true;
    document.body.appendChild(sessionBanner);
    return sessionBanner;
  }
  function showSessionBanner() {
    const el = ensureBanner();
    el.textContent =
      "⏸ Another Jarvis session is active — this one is muted. " +
      "Tap the mic or talk to take over.";
    el.hidden = false;
    el.classList.add("show");
  }
  function hideSessionBanner() {
    if (!sessionBanner) return;
    sessionBanner.hidden = true;
    sessionBanner.classList.remove("show");
  }

  // Apply an incoming session state from the server.
  function applySessionState(active) {
    const becomingInactive = sessionActive && !active;
    const becomingActive = !sessionActive && active;
    sessionActive = !!active;

    if (!sessionActive) {
      // This tab is now muted: stop wake, stop/mute any playback, show banner.
      wakeWasOnBeforeMute = wakeMode;
      if (wakeMode) disableWakeMode(true);   // silent — banner explains the state
      stopAllPlayback();
      if (becomingInactive || true) showSessionBanner();
      setStatus("muted — another session active", "off");
      if (hudState === "speaking" || hudState === "listening") setHudState("idle");
    } else {
      // This tab is (now) the active session: clear banner, restore status.
      hideSessionBanner();
      if (becomingActive) {
        setStatus("active", "live");
        // If wake was on before we got muted, bring it back (best-effort; needs
        // a prior user gesture to grant the mic, which a take-over tap provides).
        if (wakeWasOnBeforeMute && !wakeMode) {
          wakeWasOnBeforeMute = false;
          enableWakeMode();
        }
      }
    }
  }

  // Hard-stop any in-flight TTS playback and the echo guard so a muted tab is
  // fully silent. Used when this tab loses the active session.
  function stopAllPlayback() {
    try {
      if (currentAudioSrc) {
        currentAudioSrc.onended = null;
        currentAudioSrc.stop(0);
      }
    } catch (e) { /* already stopped */ }
    currentAudioSrc = null;
    audioQueue = [];            // drop any queued streaming chunks
    queuePlaying = false;
    turnAudioComplete = false;
    pendingDecodes = 0;
    if (orb && orb.setAnalyser) orb.setAnalyser(null);
    isSpeaking = false;
    echoGuardUntil = 0;
    commandTurnActive = false;
  }

  // ----- Interim transcript display -----
  function showInterim(text) {
    if (text) {
      interimEl.innerHTML = escapeHtml(text) + '<span class="caret"></span>';
      interimEl.classList.add("show");
    } else {
      interimEl.classList.remove("show");
      interimEl.textContent = "";
    }
  }

  // ----- Approvals -----
  function renderApproval(id, tool, detail) {
    const card = document.createElement("div");
    card.className = "approval";
    card.dataset.id = id;
    card.innerHTML =
      '<div class="head">Authorization required</div>' +
      '<div class="tool">' + escapeHtml(tool) + "</div>" +
      '<div class="detail">' + escapeHtml(detail || "") + "</div>" +
      '<div class="btns">' +
      '<button class="approve">Approve</button>' +
      '<button class="deny">Deny</button>' +
      "</div>";
    card.querySelector(".approve").addEventListener("click", () => respondApproval(id, true, card));
    card.querySelector(".deny").addEventListener("click", () => respondApproval(id, false, card));
    approvalsEl.appendChild(card);
  }

  function respondApproval(id, approved, card) {
    send({ type: "approval_response", id: id, approved: approved });
    if (card && card.parentNode) card.parentNode.removeChild(card);
    addMsg("system", approved ? "authorization granted" : "authorization denied");
  }

  // ----- WebSocket -----
  let ws = null;
  let reconnectTimer = null;
  // Single-client (BUG 1): set true when the server closes THIS tab (code 4000)
  // because another device/tab took over. A dormant tab does NOT auto-reconnect
  // (no churn, no chance of it later capturing/playing) — the user taps the mic
  // to reclaim the session here.
  let dormant = false;

  function wsUrl() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return proto + "//" + location.host + "/ws";
  }

  function connect() {
    clearTimeout(reconnectTimer);
    setStatus("connecting", "off");
    ws = new WebSocket(wsUrl());
    ws.binaryType = "arraybuffer";

    ws.onopen = () => setStatus("online", "live");

    ws.onclose = (ev) => {
      // BUG 1: a flapping/reconnecting client must go SILENT and disarm its wake
      // mic immediately — it stays muted until the server re-grants active (which
      // now only happens when this tab takes a turn). Otherwise a reconnect could
      // resume audio/wake while another tab is the live session → double-talk.
      sessionActive = false;
      stopAllPlayback();
      disableWakeMode(true);   // silent — status message below explains the state
      // Stuck-on-thinking fix (FIX 3a): if the socket dropped mid-turn (HUD is in
      // thinking/listening/speaking), the reply can never arrive — reset the HUD
      // back to ready so a dropped turn never leaves the orb stuck on "thinking".
      resetTurnUI();
      // Single-client (BUG 1): the server closes a superseded tab with code 4000
      // when another device/tab takes over. Go DORMANT — do NOT auto-reconnect —
      // so this tab can never reconnect and later capture/play (double voice).
      // The user reclaims the session here by tapping the mic.
      if (ev && ev.code === 4000) {
        dormant = true;
        showSessionBanner();
        setStatus("active on another device — tap mic to use here", "off");
        return;   // no reconnect timer
      }
      setStatus("offline — reconnecting", "off");
      reconnectTimer = setTimeout(connect, 1500);
    };

    ws.onerror = () => { try { ws.close(); } catch (e) {} };

    ws.onmessage = (ev) => {
      if (ev.data instanceof ArrayBuffer) { playMp3(ev.data); return; }
      if (ev.data instanceof Blob) { ev.data.arrayBuffer().then(playMp3); return; }
      let m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      handleMessage(m);
    };
  }

  function handleMessage(m) {
    switch (m.type) {
      case "transcript":
        if (m.text) addMsg("user", m.text);
        break;
      case "reply": {
        // FIX 3b: a reply settled the turn — always clear the watchdog so a
        // normal reply can never trip the "didn't come back" notice.
        clearTurnWatchdog();
        const div = addMsg("jarvis", m.text || "");
        addActions(div, m.actions);
        // reply arrived; if no audio follows, drop back to idle shortly
        if (hudState === "thinking") setHudState("idle");
        break;
      }
      case "approval":
        renderApproval(m.id, m.tool, m.detail);
        break;
      case "session":
        // Single-active-session control (BUG 1): server tells us if THIS tab is
        // the live voice session. Inactive → mute + stop wake + banner.
        applySessionState(!!m.active);
        break;
      case "audio_incoming":
        if (!sessionActive) break;   // muted tab ignores the playback heads-up
        clearTurnWatchdog();   // audio (or a chunk) is arriving — the turn is alive
        turnAudioComplete = false;   // more audio is coming; not done until audio_end
        isSpeaking = true;   // echo guard ON ahead of the mp3 frame
        stopWakeRecognizer();   // free the mic before audio arrives — no self-hearing
        setStatus("speaking", "live");
        setHudState("speaking");
        break;
      case "audio_end":
        // Streaming voice: the server finished sending all chunks for this turn.
        // Finalize once the queue drains (or immediately if nothing's playing).
        turnAudioComplete = true;
        // Only finalize if nothing is playing/queued AND no chunk is still decoding.
        // If a decode is in flight, that chunk's finally-block finalizes instead.
        if (!queuePlaying && audioQueue.length === 0 && pendingDecodes === 0) {
          if (isSpeaking || hudState === "speaking" || hudState === "thinking") {
            finalizeSpeaking();
          }
        }
        break;
      case "status":
        if (m.text) {
          // FIX 3b: a server-side error settles (ends) the turn — clear the
          // watchdog and unstick the HUD so it doesn't sit on "thinking".
          if (/^error:/i.test(m.text) || m.text === "no speech detected") {
            resetTurnUI();
          } else if (turnWatchdogTimer) {
            // ONLY when a turn is actually in flight (watchdog armed by a send).
            // A progress ping (tool activity / heartbeat) keeps the turn alive;
            // re-arm so long ops don't trip "didn't come back". Crucially, do NOT
            // touch the HUD on connect/idle statuses like "ready"/"online".
            armTurnWatchdog();
            // In a tool gap (no audio playing/queued) show "working" not a silent orb.
            if (!queuePlaying && audioQueue.length === 0) {
              isSpeaking = false;
              if (orb && orb.setAnalyser) orb.setAnalyser(null);
              if (hudState !== "thinking") setHudState("thinking");
            }
          }
          setStatus(m.text, (m.text === "ready" || m.text === "online" || m.text === "connected") ? "live" : "");
        }
        break;
      case "job_notice": {
        // A background specialist finished while you were mid-conversation. Don't
        // speak over you — soft chime + a system chip. Jarvis relays the full
        // result on the next turn (the server already wove it into context).
        jobChime();
        const who = m.specialist || "a specialist";
        const sum = (m.summary || "").trim();
        addMsg("system", "✓ " + who + " finished" + (sum ? " — " + sum : ""));
        break;
      }
      case "pong":
        break;
      default:
        break;
    }
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

  // Send a binary audio frame (recorded mobile blob) to the server's
  // _handle_audio_blob → Whisper. This is ALWAYS user-initiated (mic tap /
  // recorder onstop), so it must work even while muted — that's how a muted tab
  // TAKES OVER. The server PROMOTES this socket on receiving the turn and sends
  // back {"session":true}, which unmutes us. No sessionActive guard here (BUG 1
  // stuck-muted fix): playback stays gated and wake stays disabled while muted,
  // so a user-initiated send is the only send a muted tab can make.
  function sendBlob(blob) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(blob);
      return true;
    }
    return false;
  }

  // ----- Turn watchdog (FIX 3b: stuck-on-thinking) -----
  // When a turn is sent the HUD goes THINKING. If the WS dies (mobile/Tailscale
  // latency, PWA backgrounding) the {"type":"reply"} can never arrive and the
  // orb is stuck on "thinking" forever. A ~45s watchdog resets the HUD to ready
  // and shows a brief notice if no reply (or error) comes back in time. The
  // timer is armed when a turn is sent and CLEARED whenever a reply/error/status
  // settles the turn (see clearTurnWatchdog calls in handleMessage / resetTurnUI).
  // 90s of TOTAL silence from the server = stuck. A long turn no longer trips
  // this because each tool the agent runs sends a "working…" status that re-arms
  // the watchdog (see the "status" case above), so an active investigation that
  // takes minutes stays alive as long as tools keep firing.
  const TURN_WATCHDOG_MS = 90000;
  let turnWatchdogTimer = null;

  function clearTurnWatchdog() {
    if (turnWatchdogTimer) { clearTimeout(turnWatchdogTimer); turnWatchdogTimer = null; }
  }

  function armTurnWatchdog() {
    clearTurnWatchdog();
    turnWatchdogTimer = setTimeout(() => {
      turnWatchdogTimer = null;
      // No reply came back in time — unstick the HUD and tell the user.
      resetTurnUI();
      addMsg("system", "that didn’t come back — try again.");
      setStatus("ready", "live");
    }, TURN_WATCHDOG_MS);
  }

  // Reset any in-progress turn UI back to a clean "ready" state. Safe to call
  // anytime; only nudges the HUD out of an in-flight state. Clears the watchdog.
  function resetTurnUI() {
    clearTurnWatchdog();
    showInterim("");
    if (hudState === "thinking" || hudState === "listening") {
      setHudState("idle");
    }
  }

  // User-initiated text turn (tap-to-talk recognition result OR typed input).
  // No sessionActive guard (BUG 1 stuck-muted fix): a muted tab MUST be able to
  // type/talk to take over. The server promotes this socket on the turn and
  // replies {"session":true} → applySessionState unmutes us.
  function sendUserText(text) {
    const t = (text || "").trim();
    if (!t) return;
    stopAllPlayback();   // barge-in: kill any leftover/streaming audio from the last turn
    send({ type: "text", text: t });
    setHudState("thinking");
    setStatus("thinking", "live");
    armTurnWatchdog();   // FIX 3b: don't let a dropped turn hang on "thinking"
  }

  // ----- Streaming STT (Web Speech API) -----
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  let recognition = null;
  let recognizing = false;
  let finalTranscript = "";
  let silenceTimer = null;
  // End-of-speech grace: after you stop talking, wait this long before finishing
  // the turn, so a pause to gather your thoughts never cuts you off. Each new word
  // resets it. (Web Speech's built-in end-of-speech is ~0.5s — far too eager.)
  const SILENCE_GRACE_MS = 4000;
  const speechSupported = !!SR;

  // Runtime decision: use recorder path only when on mobile AND the toggle says so
  // AND Web Speech is actually available. If Web Speech isn't supported, always fall
  // back to the recorder path so the app never silently breaks.
  const USE_RECORDER = IS_MOBILE && (!MOBILE_USE_WEBSPEECH || !speechSupported);

  function buildRecognition() {
    const r = new SR();
    r.lang = "en-US";
    r.continuous = true;   // stay listening across pauses; WE end the turn after SILENCE_GRACE_MS of quiet
    r.interimResults = true;
    r.maxAlternatives = 1;

    r.onstart = () => {
      clearTimeout(silenceTimer);
      recognizing = true;
      finalTranscript = "";
      micBtn.classList.add("recording");
      micBtn.innerHTML = "&#9673;";
      micLabel.textContent = "listening — tap to stop";
      setHudState("listening");
      setStatus("listening", "live");
      startMicMeter();
    };

    r.onresult = (event) => {
      let interim = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const res = event.results[i];
        if (res.isFinal) {
          finalTranscript += res[0].transcript;
        } else {
          interim += res[0].transcript;
        }
      }
      showInterim((finalTranscript + interim).trim());
      // Custom end-of-speech: each result (interim OR final) resets a 4s timer.
      // Only when you've been quiet for SILENCE_GRACE_MS do we stop → onend sends.
      // This is what lets you pause to think without getting cut off.
      clearTimeout(silenceTimer);
      silenceTimer = setTimeout(() => {
        if (recognizing && recognition) {
          try { recognition.stop(); } catch (e) {}
        }
      }, SILENCE_GRACE_MS);
    };

    r.onerror = (e) => {
      console.warn("[app] speech error", e.error);
      if (e.error === "no-speech") {
        setStatus("no speech detected", "live");
      } else if (e.error === "not-allowed" || e.error === "service-not-allowed") {
        setStatus("mic blocked — check permissions", "off");
      } else if (e.error === "aborted") {
        // user-initiated stop; stay quiet
      } else {
        setStatus("speech error: " + e.error, "");
      }
    };

    r.onend = () => {
      clearTimeout(silenceTimer);
      recognizing = false;
      micBtn.classList.remove("recording");
      micBtn.innerHTML = "&#9678;";
      micLabel.textContent = speechSupported ? "tap to talk" : "voice unavailable";
      showInterim("");
      stopMicMeter();

      const text = finalTranscript.trim();
      if (text) {
        sendUserText(text);
      } else {
        // nothing captured — back to idle unless audio is playing
        if (hudState === "listening") {
          setHudState("idle");
          setStatus("ready", "live");
        }
      }
      // Tap-to-talk turn is over — let wake mode resume listening (if it's on).
      scheduleWakeRestart(300);
    };

    return r;
  }

  function startRecognition() {
    if (!speechSupported) {
      setStatus("Web Speech API not supported — type instead", "");
      textInput && textInput.focus();
      return;
    }
    if (recognizing) return;
    ensureAudio(); // unlock audio output on the user gesture (for TTS playback)
    stopWakeRecognizer(); // tap-to-talk owns the mic now; wake stands down
    recognition = buildRecognition();
    try {
      recognition.start();
    } catch (e) {
      // .start() throws if called too quickly after a previous session
      console.warn("[app] recognition.start failed", e);
    }
  }

  function stopRecognition() {
    if (recognition && recognizing) {
      try { recognition.stop(); } catch (e) {}
    }
  }

  // =======================================================================
  // MOBILE voice input — MediaRecorder → server → Whisper
  // -----------------------------------------------------------------------
  // Android Chrome's Web Speech mic cuts out after a couple seconds and is
  // outright broken inside installed PWAs. So on mobile we capture the mic with
  // MediaRecorder and ship the recorded blob as a BINARY WebSocket frame. The
  // server (_handle_audio_blob) writes it, runs mlx-whisper, and replies with
  // {"type":"transcript"} then the reply + mp3 — all handled by existing code.
  // Tap-to-toggle: first tap records (state LISTENING + orb mic meter), second
  // tap stops, builds the blob, and sends it (state THINKING / "transcribing").
  // =======================================================================

  // Pick the first MediaRecorder MIME the browser actually supports. The server
  // saves whatever arrives as .webm; mlx-whisper (ffmpeg) sniffs the real
  // container, so webm/mp4/ogg all decode fine regardless of the extension.
  const MR_MIME_CANDIDATES = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/mp4",
    "audio/ogg;codecs=opus",
  ];
  function pickRecorderMime() {
    if (typeof MediaRecorder === "undefined" ||
        typeof MediaRecorder.isTypeSupported !== "function") {
      return null;
    }
    for (const m of MR_MIME_CANDIDATES) {
      if (MediaRecorder.isTypeSupported(m)) return m;
    }
    return null;
  }

  const RECORDER_SUPPORTED =
    typeof MediaRecorder !== "undefined" &&
    !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  const MIN_BLOB_BYTES = 1200;   // ~<0.3s of opus → almost certainly a stray tap

  let mediaRecorder = null;
  let recordedChunks = [];
  let recordStream = null;       // the getUserMedia stream backing the recorder
  let recordMime = null;
  let isRecording = false;
  let recordStartedAt = 0;

  // Fully release the mic so the OS recording indicator clears between captures.
  function releaseRecordStream() {
    if (recordStream) {
      try { recordStream.getTracks().forEach((t) => t.stop()); } catch (e) {}
      recordStream = null;
    }
  }

  async function startRecording() {
    if (isRecording) return;
    if (!RECORDER_SUPPORTED) {
      setStatus("recording unsupported — type instead", "off");
      textInput && textInput.focus();
      return;
    }
    recordMime = pickRecorderMime();
    if (!recordMime) {
      setStatus("no supported audio format — type instead", "off");
      textInput && textInput.focus();
      return;
    }
    ensureAudio(); // unlock audio output on this user gesture (for TTS playback)
    try {
      recordStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
    } catch (e) {
      console.warn("[app] mic getUserMedia failed", e);
      setStatus("mic blocked — check permissions", "off");
      return;
    }
    let rec;
    try {
      rec = new MediaRecorder(recordStream, { mimeType: recordMime });
    } catch (e) {
      // Some browsers reject the options form — fall back to the default.
      try { rec = new MediaRecorder(recordStream); }
      catch (e2) {
        console.error("[app] MediaRecorder init failed", e2);
        setStatus("recorder error — type instead", "off");
        releaseRecordStream();
        return;
      }
    }
    mediaRecorder = rec;
    recordedChunks = [];

    rec.ondataavailable = (ev) => {
      if (ev.data && ev.data.size > 0) recordedChunks.push(ev.data);
    };

    rec.onstop = () => {
      isRecording = false;
      micBtn.classList.remove("recording");
      micBtn.innerHTML = "&#9678;";
      micLabel.textContent = "tap to talk";
      stopMicMeter();
      showInterim("");

      const type = (recordedChunks[0] && recordedChunks[0].type) || recordMime || "audio/webm";
      const blob = new Blob(recordedChunks, { type });
      recordedChunks = [];
      releaseRecordStream();

      const tooShort = Date.now() - recordStartedAt < 300;
      // Guard: drop empty / very-short blobs so a stray tap doesn't send silence.
      if (blob.size < MIN_BLOB_BYTES || tooShort) {
        if (hudState === "listening") setHudState("idle");
        setStatus("nothing recorded", "live");
        return;
      }
      stopAllPlayback();   // barge-in: kill any leftover/streaming audio from the last turn
      if (!sendBlob(blob)) {
        if (hudState === "listening") setHudState("idle");
        setStatus(sessionActive ? "connection lost" : "muted — another session", "off");
        return;
      }
      // Sent — server now transcribes (mlx-whisper) then replies + speaks.
      setHudState("thinking");
      setStatus("🎙️ transcribing…", "live");
      armTurnWatchdog();   // FIX 3b: don't let a dropped turn hang on "thinking"
    };

    rec.onerror = (e) => {
      console.error("[app] MediaRecorder error", e);
      isRecording = false;
      micBtn.classList.remove("recording");
      micBtn.innerHTML = "&#9678;";
      micLabel.textContent = "tap to talk";
      stopMicMeter();
      releaseRecordStream();
      setStatus("recording error", "off");
      if (hudState === "listening") setHudState("idle");
    };

    try {
      rec.start(); // single blob on stop (no timeslice needed for short turns)
    } catch (e) {
      console.error("[app] recorder.start failed", e);
      releaseRecordStream();
      setStatus("recording error", "off");
      return;
    }
    isRecording = true;
    recordStartedAt = Date.now();
    micBtn.classList.add("recording");
    micBtn.innerHTML = "&#9673;";
    micLabel.textContent = "recording — tap to send";
    setHudState("listening");
    setStatus("listening", "live");
    startMicMeter(); // drive the orb from the mic AnalyserNode
  }

  function stopRecording() {
    if (!isRecording || !mediaRecorder) return;
    try { mediaRecorder.stop(); } catch (e) {
      // stop() can throw if the recorder is in a bad state — clean up manually.
      isRecording = false;
      micBtn.classList.remove("recording");
      micBtn.innerHTML = "&#9678;";
      micLabel.textContent = "tap to talk";
      stopMicMeter();
      releaseRecordStream();
      if (hudState === "listening") setHudState("idle");
    }
  }

  // ----- Mic button: platform-aware (mobile records, desktop streams) -----
  // Tap to toggle on both paths (cleaner than press-and-hold for a single tap
  // on touch, and matches the existing desktop streaming behavior).
  micBtn.addEventListener("click", (e) => {
    e.preventDefault();
    // Single-client (BUG 1): if this tab was superseded and went dormant, the
    // first mic tap reclaims the session here (reconnect) rather than recording.
    if (dormant) {
      dormant = false;
      hideSessionBanner();
      setStatus("connecting", "off");
      connect();
      return;
    }
    if (USE_RECORDER) {
      // Mobile: Web Speech is unreliable, so keep tap-to-record (tap → record → tap → send).
      if (isRecording) stopRecording();
      else startRecording();
    } else {
      // Desktop: hands-free — tap once to start the open-mic conversation, tap to stop.
      toggleLiveMode();
    }
  });

  // Surface the mic state label up-front based on which path is active.
  if (USE_RECORDER) {
    // MediaRecorder→Whisper path (mobile + toggle off, or Web Speech unavailable)
    if (RECORDER_SUPPORTED && pickRecorderMime()) {
      micLabel.textContent = "tap to talk";
      micBtn.title = "Tap to record, tap again to send";
    } else {
      micLabel.textContent = "voice unavailable — type below";
      micBtn.title = "Audio recording not supported in this browser";
    }
  } else if (!speechSupported) {
    // Web Speech path requested but not available
    micLabel.textContent = "voice unavailable — type below";
    micBtn.title = "Web Speech API not supported in this browser";
  }
  // else: Web Speech path — default label set in Web Speech onstart/onstop handlers

  // =======================================================================
  // Wake-word mode — hands-free "Jarvis…"
  // -----------------------------------------------------------------------
  // A SEPARATE continuous webkitSpeechRecognition runs in the background while
  // the toggle is ON. It listens for "jarvis" / "hey jarvis", then either grabs
  // the rest of that utterance as the command, or opens a short window to catch
  // the next final result. The command is sent via the SAME text-turn path that
  // tap-to-talk uses (sendUserText). Hard parts handled here:
  //   * auto-restart on Chrome's periodic onend (unless turned off / busy)
  //   * onerror backoff (no-speech / aborted / not-allowed)
  //   * echo guard — ignore everything while Jarvis is speaking (+300ms tail)
  //   * debounce — one "jarvis" fires exactly one command (no interim+final dupe)
  //   * coexistence with tap-to-talk — recognizers never run at the same time
  // =======================================================================
  const WAKE_WORDS = ["hey jarvis", "jarvis"];
  const WAKE_LS_KEY = "jarvis.wakeMode";
  const WAKE_CAPTURE_MS = 6000;        // window to catch the next utterance
  const WAKE_DEBOUNCE_MS = 1500;       // ignore repeat wakes within this window
  const WAKE_RESTART_BASE_MS = 350;    // base backoff between auto-restarts
  const WAKE_RESTART_MAX_MS = 5000;    // cap for error backoff

  let wakeMode = false;                // toggle state (persisted)
  let liveMode = false;                // open-mic hands-free convo (no wake word) — every utterance is sent
  const LIVE_LS_KEY = "jarvis.liveMode";
  let wakeRec = null;                  // the continuous recognizer instance
  let wakeRunning = false;             // is wakeRec currently active?
  let wakeStartGuard = false;          // prevents double .start()
  let wakeRestartTimer = null;
  let wakeBackoff = WAKE_RESTART_BASE_MS;
  let commandTurnActive = false;       // a command is being processed end-to-end
  let lastWakeAt = 0;                  // for debounce
  let capturing = false;              // inside the "next utterance" window
  let captureTimer = null;
  let wakeIntentionalStop = false;     // we asked it to stop (don't auto-restart)

  function setWakeUI(on, armed) {
    if (!wakeToggle) return;
    wakeToggle.dataset.wake = on ? "on" : "off";
    wakeToggle.setAttribute("aria-pressed", on ? "true" : "false");
    if (wakeStateEl) wakeStateEl.textContent = armed ? "LISTENING…" : (on ? "ON" : "OFF");
    wakeToggle.classList.toggle("armed", !!armed);
  }

  function clearCaptureWindow() {
    capturing = false;
    if (captureTimer) { clearTimeout(captureTimer); captureTimer = null; }
  }

  // Normalize a transcript for matching: lowercase, strip punctuation/extra space.
  function normalize(s) {
    return (s || "").toLowerCase().replace(/[^a-z0-9\s]/g, " ").replace(/\s+/g, " ").trim();
  }

  // If `text` contains a wake word, return the command part AFTER it (may be "").
  // Returns null if no wake word present.
  function extractAfterWake(text) {
    const n = normalize(text);
    if (!n) return null;
    for (const w of WAKE_WORDS) {
      const idx = n.indexOf(w);
      if (idx !== -1) {
        return n.slice(idx + w.length).trim();
      }
    }
    return null;
  }

  function isSubstantialCommand(cmd) {
    // Ignore empty / single filler tokens; require something real to send.
    const c = (cmd || "").trim();
    if (!c) return false;
    if (c.length < 2) return false;
    // a lone "jarvis" echo shouldn't count
    if (WAKE_WORDS.includes(c)) return false;
    return true;
  }

  // Short WebAudio beep as the activation cue (no asset needed).
  function wakeBeep() {
    try {
      ensureAudio();
      const o = audioCtx.createOscillator();
      const g = audioCtx.createGain();
      o.type = "sine";
      o.frequency.setValueAtTime(880, audioCtx.currentTime);
      o.frequency.exponentialRampToValueAtTime(1320, audioCtx.currentTime + 0.09);
      g.gain.setValueAtTime(0.0001, audioCtx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.18, audioCtx.currentTime + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + 0.16);
      o.connect(g); g.connect(audioCtx.destination);
      o.start();
      o.stop(audioCtx.currentTime + 0.18);
    } catch (e) { /* beep is cosmetic */ }
  }

  // Soft two-tone "ta-da" for a finished background job — deliberately lower and
  // gentler than wakeBeep so it reads as "result ready", not "I'm listening".
  function jobChime() {
    try {
      ensureAudio();
      const t = audioCtx.currentTime;
      [523, 784].forEach((freq, i) => {
        const o = audioCtx.createOscillator();
        const g = audioCtx.createGain();
        o.type = "sine";
        const start = t + i * 0.12;
        o.frequency.setValueAtTime(freq, start);
        g.gain.setValueAtTime(0.0001, start);
        g.gain.exponentialRampToValueAtTime(0.12, start + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, start + 0.18);
        o.connect(g); g.connect(audioCtx.destination);
        o.start(start);
        o.stop(start + 0.2);
      });
    } catch (e) { /* chime is cosmetic */ }
  }

  // Visual cue when the wake word is heard.
  function flashWakeArmed() {
    setWakeUI(true, true);
    setHudState("listening");
    setStatus("listening", "live");
    showInterim("listening…");
  }
  function clearWakeArmed() {
    setWakeUI(wakeMode, false);
    showInterim("");
  }

  // Fire a command captured via wake. Suspends the wake recognizer for the turn.
  function dispatchWakeCommand(cmd) {
    const text = (cmd || "").trim();
    if (!isSubstantialCommand(text)) return false;
    commandTurnActive = true;
    clearCaptureWindow();
    clearWakeArmed();
    wakeBeep();
    stopWakeRecognizer();           // free the mic; the command turn owns it
    // BUG: no local echo — sendUserText sends {"type":"text"}, and the server
    // replies with {"type":"transcript"} which renders this message. A local
    // addMsg here would double it on screen.
    sendUserText(text);             // SAME text-turn path as tap-to-talk
    // commandTurnActive is cleared when TTS playback ends (playMp3 onended),
    // which then schedules a wake restart. As a safety net, also clear/restart
    // after a max turn time in case no audio comes back.
    setTimeout(() => {
      if (commandTurnActive) {
        commandTurnActive = false;
        scheduleWakeRestart(300);
      }
    }, 20000);
    return true;
  }

  // Handle the wake recognizer's results (interim + final).
  function onWakeResult(event) {
    if (!wakeMode) return;
    if (echoGuardActive()) return;          // ignore Jarvis's own voice
    if (commandTurnActive) return;          // a command is already in flight

    let interim = "";
    let finals = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const res = event.results[i];
      if (res.isFinal) finals += res[0].transcript + " ";
      else interim += res[0].transcript + " ";
    }
    const combined = (finals + interim).trim();

    // --- LIVE (open-mic) mode: no wake word — every substantial final utterance
    // is sent straight to Jarvis. The echo guard + commandTurnActive checks above
    // keep it from hearing Jarvis's own voice or interrupting an in-flight turn.
    if (liveMode) {
      const finalText = finals.trim();
      if (finalText) {
        const cmd = normalize(finalText);
        if (isSubstantialCommand(cmd)) dispatchWakeCommand(cmd);
      } else if (interim) {
        showInterim("listening… " + normalize(interim));
      }
      return;
    }

    // --- Capture-window mode: we already heard "jarvis", now grab the command.
    if (capturing) {
      // Prefer a final result inside the window; strip a leading "jarvis" if the
      // user repeated it ("Jarvis… jarvis what's the weather").
      const finalText = finals.trim();
      if (finalText) {
        let cmd = finalText;
        const after = extractAfterWake(finalText);
        if (after !== null) cmd = after;      // they said jarvis again
        cmd = normalize(cmd);
        if (isSubstantialCommand(cmd)) {
          dispatchWakeCommand(cmd);
          return;
        }
        // final but nothing useful — keep the window open until it times out
      } else if (interim) {
        showInterim("listening… " + normalize(interim));
      }
      return;
    }

    // --- Idle mode: look for the wake word in either interim or final text.
    const after = extractAfterWake(combined);
    if (after === null) return;               // no wake word yet

    // Debounce: one wake → one command.
    const now = Date.now();
    if (now - lastWakeAt < WAKE_DEBOUNCE_MS) return;

    if (isSubstantialCommand(after)) {
      // Command rode in on the same utterance — fire it.
      lastWakeAt = now;
      dispatchWakeCommand(after);
    } else {
      // Heard "jarvis" with nothing substantial after — open the capture window
      // and wait for the next utterance. Only arm once per debounce window.
      lastWakeAt = now;
      capturing = true;
      flashWakeArmed();
      if (captureTimer) clearTimeout(captureTimer);
      captureTimer = setTimeout(() => {
        // window expired with no usable command — stand back down to listening
        clearCaptureWindow();
        if (!commandTurnActive) {
          clearWakeArmed();
          if (hudState === "listening") setHudState("idle");
          setStatus(wakeMode ? "wake ready" : "ready", "live");
        }
      }, WAKE_CAPTURE_MS);
    }
  }

  function buildWakeRecognizer() {
    const r = new SR();
    r.lang = "en-US";
    r.continuous = true;
    r.interimResults = true;
    r.maxAlternatives = 1;

    r.onstart = () => {
      wakeRunning = true;
      wakeStartGuard = false;
      wakeBackoff = WAKE_RESTART_BASE_MS;   // reset backoff on a clean start
    };

    r.onresult = onWakeResult;

    r.onerror = (e) => {
      const err = e && e.error;
      if (err === "not-allowed" || err === "service-not-allowed") {
        // Mic permission denied — turning wake on is pointless; flip it off.
        console.warn("[app] wake mic blocked", err);
        setStatus("mic blocked — wake off", "off");
        disableWakeMode(true);
        return;
      }
      if (err === "aborted") {
        // usually our own stop(); onend will decide whether to restart
        return;
      }
      if (err === "no-speech" || err === "network" || err === "audio-capture") {
        // transient — back off a bit and let onend restart
        wakeBackoff = Math.min(WAKE_RESTART_MAX_MS, wakeBackoff * 1.6);
        return;
      }
      // unknown — back off
      console.warn("[app] wake error", err);
      wakeBackoff = Math.min(WAKE_RESTART_MAX_MS, wakeBackoff * 1.6);
    };

    r.onend = () => {
      wakeRunning = false;
      // Auto-restart unless: wake is off, we stopped on purpose, tap-to-talk is
      // running, or a command turn is active (it'll restart when the turn ends).
      if (!wakeMode || wakeIntentionalStop || recognizing || commandTurnActive) {
        wakeIntentionalStop = false;
        return;
      }
      scheduleWakeRestart(wakeBackoff);
    };

    return r;
  }

  function startWakeRecognizer() {
    if (!speechSupported || !wakeMode) return;
    if (!sessionActive) return;                      // muted tab never arms wake (BUG 1)
    if (wakeRunning || wakeStartGuard) return;
    if (recognizing || commandTurnActive) return;   // don't fight tap-to-talk
    if (echoGuardActive()) { scheduleWakeRestart(ECHO_GUARD_TAIL_MS + 80); return; }
    wakeStartGuard = true;
    wakeIntentionalStop = false;
    wakeRec = buildWakeRecognizer();
    try {
      wakeRec.start();
    } catch (e) {
      // .start() throws if called too soon after a previous session — retry.
      wakeStartGuard = false;
      console.warn("[app] wake start failed, retrying", e);
      scheduleWakeRestart(500);
    }
  }

  function stopWakeRecognizer() {
    clearTimeout(wakeRestartTimer);
    wakeRestartTimer = null;
    if (wakeRec && wakeRunning) {
      wakeIntentionalStop = true;
      try { wakeRec.stop(); } catch (e) {}
    }
    wakeStartGuard = false;
  }

  function scheduleWakeRestart(delay) {
    if (!wakeMode) return;
    clearTimeout(wakeRestartTimer);
    wakeRestartTimer = setTimeout(() => {
      wakeRestartTimer = null;
      startWakeRecognizer();
    }, Math.max(0, delay || WAKE_RESTART_BASE_MS));
  }

  function enableWakeMode() {
    if (!speechSupported) {
      setStatus("Web Speech API not supported — wake unavailable", "");
      return;
    }
    wakeMode = true;
    try { localStorage.setItem(WAKE_LS_KEY, "on"); } catch (e) {}
    setWakeUI(true, false);
    ensureAudio();                 // unlock audio (we're inside a click gesture)
    setStatus("wake ready — say “Jarvis”", "live");
    startWakeRecognizer();
  }

  function disableWakeMode(silent) {
    wakeMode = false;
    try { localStorage.setItem(WAKE_LS_KEY, "off"); } catch (e) {}
    clearCaptureWindow();
    stopWakeRecognizer();
    setWakeUI(false, false);
    showInterim("");
    if (!silent) setStatus("ready", "live");
    if (hudState === "listening") setHudState("idle");
  }

  function toggleWakeMode() {
    if (wakeMode) disableWakeMode();
    else enableWakeMode();
  }

  // ----- LIVE (open-mic) hands-free conversation -----
  // Reuses the continuous recognizer but skips the wake word: tap the mic once and
  // just talk — each utterance is sent, and after Jarvis speaks the mic re-opens
  // automatically (the echo guard stops him from hearing himself).
  function enableLiveMode() {
    if (!speechSupported) { setStatus("Web Speech not supported in this browser", ""); return; }
    liveMode = true;
    wakeMode = true;                 // keeps the continuous recognizer running + auto-restarting
    try { localStorage.setItem(LIVE_LS_KEY, "on"); } catch (e) {}
    ensureAudio();                   // unlock audio output inside this click gesture
    micBtn.classList.add("recording");
    micLabel.textContent = "live — tap to stop";
    setStatus("listening — just talk", "live");
    startWakeRecognizer();
  }

  function disableLiveMode(silent) {
    liveMode = false;
    wakeMode = false;
    try {
      localStorage.setItem(LIVE_LS_KEY, "off");
      localStorage.setItem(WAKE_LS_KEY, "off");
    } catch (e) {}
    clearCaptureWindow();
    stopWakeRecognizer();
    micBtn.classList.remove("recording");
    micLabel.textContent = "tap to talk";
    showInterim("");
    if (!silent) setStatus("ready", "live");
    if (hudState === "listening") setHudState("idle");
  }

  function toggleLiveMode() {
    if (liveMode) disableLiveMode();
    else enableLiveMode();
  }

  if (wakeToggle) {
    if (IS_MOBILE) {
      // Wake word rides on continuous Web Speech — always hidden on mobile even
      // when MOBILE_USE_WEBSPEECH routes tap-to-talk through Web Speech. Continuous
      // wake mode is desktop-only; the mobile mic button is the only voice control.
      wakeMode = false;
      wakeToggle.disabled = true;
      wakeToggle.hidden = true;
      wakeToggle.style.display = "none";
      wakeToggle.title = "Wake word is desktop only";
      const wakeNoteEl = document.getElementById("wakeNote");
      if (wakeNoteEl) wakeNoteEl.style.display = "none";
      setWakeUI(false, false);
    } else if (!speechSupported) {
      wakeToggle.disabled = true;
      wakeToggle.title = "Web Speech API not supported in this browser";
      setWakeUI(false, false);
    } else {
      wakeToggle.addEventListener("click", (e) => {
        e.preventDefault();
        toggleWakeMode();
      });
      // Restore persisted choice (default OFF). We DON'T auto-start the recognizer
      // here — Chrome needs a user gesture to grant the mic, so if it was left ON
      // we show ON but (re)start lazily; the first click or interaction will arm.
      let saved = "off";
      try { saved = localStorage.getItem(WAKE_LS_KEY) || "off"; } catch (e) {}
      if (saved === "on") {
        wakeMode = true;
        setWakeUI(true, false);
        setStatus("wake on — tap anywhere / mic to arm", "live");
        // Attempt a start; if the browser blocks for lack of gesture, onerror
        // (not-allowed) will flip it off. A one-time gesture listener also arms it.
        startWakeRecognizer();
        const armOnce = () => {
          if (wakeMode && !wakeRunning) startWakeRecognizer();
          window.removeEventListener("pointerdown", armOnce);
          window.removeEventListener("keydown", armOnce);
        };
        window.addEventListener("pointerdown", armOnce);
        window.addEventListener("keydown", armOnce);
      } else {
        setWakeUI(false, false);
      }
    }
  }

  // Restore LIVE (open-mic) mode across reloads (default off). Browsers need a
  // user gesture to grant the mic, so if it was on we arm on the first interaction.
  if (speechSupported && !IS_MOBILE) {
    let savedLive = "off";
    try { savedLive = localStorage.getItem(LIVE_LS_KEY) || "off"; } catch (e) {}
    if (savedLive === "on") {
      liveMode = true;
      wakeMode = true;
      micBtn.classList.add("recording");
      micLabel.textContent = "live — tap to stop";
      setStatus("live — tap anywhere to start talking", "live");
      startWakeRecognizer();
      const armLiveOnce = () => {
        if (liveMode && !wakeRunning) { ensureAudio(); startWakeRecognizer(); }
        window.removeEventListener("pointerdown", armLiveOnce);
        window.removeEventListener("keydown", armLiveOnce);
      };
      window.addEventListener("pointerdown", armLiveOnce);
      window.addEventListener("keydown", armLiveOnce);
    }
  }

  // ----- Typed message fallback -----
  function sendTextFromInput() {
    const t = textInput.value.trim();
    if (!t) return;
    // BUG: don't locally echo — the server replies with {"type":"transcript"}
    // which renders this message, so a local addMsg here doubles it on screen.
    sendUserText(t);
    textInput.value = "";
  }
  textSend.addEventListener("click", sendTextFromInput);
  textInput.addEventListener("keydown", (e) => { if (e.key === "Enter") sendTextFromInput(); });

  // ----- Telemetry HUD ticks (faux + real core level) -----
  function pad2(n) { return n < 10 ? "0" + n : "" + n; }
  function tickTelemetry() {
    const lvl = orb.getLevel ? orb.getLevel() : 0;
    if (telCore) telCore.textContent = (lvl * 9.99).toFixed(2);
    if (telBar) telBar.style.width = Math.max(8, Math.min(100, lvl * 130)).toFixed(0) + "%";
    if (telPwr) {
      const p = 96 + Math.sin(Date.now() / 1400) * 1.6 + lvl * 1.5;
      telPwr.textContent = p.toFixed(1) + "%";
    }
    if (telClock) {
      const d = new Date();
      telClock.textContent = pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
    }
    requestAnimationFrame(tickTelemetry);
  }
  requestAnimationFrame(tickTelemetry);

  // ----- Keep-alive ping -----
  setInterval(() => send({ type: "ping" }), 25000);

  // ----- Boot -----
  connect();
})();
