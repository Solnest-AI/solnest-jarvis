"""
Jarvis — FastAPI server + live voice web UI (Task 1.4).

Serves the web/ directory and exposes a single WebSocket endpoint (/ws) that
drives one live voice session per connection:

    browser mic  --(webm/opus binary frame)-->  /ws
                 transcribe (faster-whisper, in a thread)
                 ask()  (Claude Agent SDK)
                 synthesize (ElevenLabs mp3, in a thread)
    browser      <--(JSON transcript/reply/actions + mp3 binary frame)--

Approvals are wired per-turn: each Session builds its own approval handler and
passes it into ask(text, on_approval=...).  When the agent wants to run a
non-safe tool, the handler emits an {"type":"approval", ...} message to the
client and awaits the user's Approve/Deny response (with a timeout → deny).
This keeps two concurrent sockets (desk + phone) isolated.

Run:
    .venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8800
or:
    .venv/bin/python server.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState

from jarvis.agent import summarize
# Engine selection: JARVIS_ENGINE=fastlane → direct Anthropic API (metered, ~1–2s);
# anything else → the Claude Code SDK on your subscription (~4–5s, free).
from jarvis import agent as _agent_engine
if os.environ.get("JARVIS_ENGINE", "fastlane").strip().lower() != "sdk":
    from jarvis import fastlane as _engine
else:
    _engine = _agent_engine
ask = _engine.ask
warmup = _engine.warmup
from jarvis.voice_stt import transcribe
from jarvis.voice_tts import synthesize

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent          # Jarvis/ root
load_dotenv(BASE_DIR / ".env")

WEB_DIR = BASE_DIR / "web"
STATE_DIR = Path(os.environ.get("STATE_DIR", str(BASE_DIR / "state")))
STATE_DIR.mkdir(parents=True, exist_ok=True)

JARVIS_PORT = int(os.environ.get("JARVIS_PORT", "8800"))
APPROVAL_TIMEOUT = float(os.environ.get("APPROVAL_TIMEOUT", "300"))  # seconds
# Cap the SPOKEN reply length only (on-screen text stays full). A long answer
# would otherwise become a multi-MB mp3 read aloud in full. Default 700 chars.
TTS_MAX_CHARS = int(os.environ.get("TTS_MAX_CHARS", "700"))
# Short note appended when the spoken text is truncated.
_TTS_TRUNCATE_NOTE = " … the rest is on your screen."
# A background job result longer than this is shown in full on screen but only
# given a brief spoken lead-in (never read aloud in full → no multi-MB mp3).
JOB_SPOKEN_MAX = int(os.environ.get("JOB_SPOKEN_MAX", "360"))

# --------------------------------------------------------------------------- #
# Logging (stderr, INFO)
# --------------------------------------------------------------------------- #
logger = logging.getLogger("jarvis.server")
if not logger.handlers:
    _sh = logging.StreamHandler(sys.stderr)
    _sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_sh)
    logger.setLevel(logging.INFO)

# --------------------------------------------------------------------------- #
# FastAPI app + static assets
# --------------------------------------------------------------------------- #
app = FastAPI(title="Jarvis")

# Mount the web/ directory so app.js, orb.js (and anything else) load from /.
# html=False because GET / is handled explicitly below to return index.html.
if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

# Serve vendored libraries (Three.js) at /vendor/* so the orb never depends on a
# CDN. Mounted before the explicit routes below so /vendor/three.min.js → 200.
VENDOR_DIR = WEB_DIR / "vendor"
if VENDOR_DIR.is_dir():
    app.mount("/vendor", StaticFiles(directory=str(VENDOR_DIR)), name="vendor")


@app.get("/")
async def index():
    return FileResponse(str(WEB_DIR / "index.html"))


# Serve the top-level JS/CSS files at the paths index.html references.
@app.get("/app.js")
async def app_js():
    return FileResponse(str(WEB_DIR / "app.js"), media_type="application/javascript")


@app.get("/orb.js")
async def orb_js():
    return FileResponse(str(WEB_DIR / "orb.js"), media_type="application/javascript")


@app.get("/style.css")
async def style_css():
    return FileResponse(str(WEB_DIR / "style.css"), media_type="text/css")


# ── PWA assets ──────────────────────────────────────────────────────────────
# manifest — must be served from root scope with correct content-type
@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(
        str(WEB_DIR / "manifest.webmanifest"),
        media_type="application/manifest+json",
    )


# Service worker — must live at root scope so it can control all paths under /
@app.get("/sw.js")
async def sw():
    return FileResponse(
        str(WEB_DIR / "sw.js"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


# Icons
@app.get("/icons/{filename}")
async def icons(filename: str):
    icon_path = WEB_DIR / "icons" / filename
    if not icon_path.is_file():
        from fastapi.responses import Response
        return Response(status_code=404)
    return FileResponse(str(icon_path), media_type="image/png")


# Self-hosted fonts (DM Mono woff2 + fonts.css) — vendored locally, no CDN, so
# the HUD renders correctly offline/air-gapped. Mirrors the /icons route.
@app.get("/fonts/{filename}")
async def fonts(filename: str):
    from fastapi.responses import Response
    # Reject path traversal: only a bare filename under web/fonts is allowed.
    if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
        return Response(status_code=404)
    font_path = WEB_DIR / "fonts" / filename
    if not font_path.is_file():
        return Response(status_code=404)
    if filename.endswith(".woff2"):
        media_type = "font/woff2"
    elif filename.endswith(".css"):
        media_type = "text/css"
    else:
        media_type = "application/octet-stream"
    return FileResponse(str(font_path), media_type=media_type)


# --------------------------------------------------------------------------- #
# Spoken-text shaping (BUG 2): cap + clean the SPOKEN reply only.
# --------------------------------------------------------------------------- #
# Strip code blocks / markdown / URLs so the assistant never reads code or
# symbols aloud.
_RE_FENCED_CODE = re.compile(r"```.*?```", re.S)
_RE_INLINE_CODE = re.compile(r"`([^`]*)`")
_RE_URL = re.compile(r"https?://\S+")
_RE_MD_SYMBOLS = re.compile(r"[*_#>|]+")
_RE_NEWLINES = re.compile(r"[ \t]*\n[ \t]*")
_RE_WS = re.compile(r"\s+")
# Sentence boundary: ., !, or ? optionally followed by closing quotes/brackets.
_RE_SENTENCE_END = re.compile(r'[.!?]["\')\]]*\s')


def _clean_for_tts(text: str) -> str:
    """Strip code/markdown/URLs so the spoken version sounds natural."""
    text = _RE_FENCED_CODE.sub(" ", text)      # fenced code blocks — never read code
    text = _RE_INLINE_CODE.sub(r"\1", text)    # inline code → bare content
    text = _RE_URL.sub("", text)               # URLs
    text = _RE_MD_SYMBOLS.sub("", text)        # markdown symbols
    text = _RE_NEWLINES.sub(". ", text)        # newlines → sentence breaks
    text = _RE_WS.sub(" ", text).strip()
    return text


def _spoken_text(reply_text: str) -> str:
    """Build the SPOKEN version of a reply: cleaned + capped at TTS_MAX_CHARS.

    The on-screen text stays full; only what we synthesize is shortened. If the
    cleaned text exceeds the cap, truncate at the last sentence boundary at/under
    the cap (falling back to a hard character cut) and append a short note so the
    listener knows the full answer is on screen.
    """
    spoken = _clean_for_tts(reply_text or "")
    if len(spoken) <= TTS_MAX_CHARS:
        return spoken

    head = spoken[:TTS_MAX_CHARS]
    # Find the last sentence boundary within the cap.
    boundaries = list(_RE_SENTENCE_END.finditer(head))
    if boundaries:
        cut = head[: boundaries[-1].end()].rstrip()
    else:
        # No sentence end in range — hard-cut at the last whitespace if possible.
        space = head.rfind(" ")
        cut = (head[:space] if space > 0 else head).rstrip()

    cut = cut.rstrip(".!?,;: ")
    return cut + _TTS_TRUNCATE_NOTE


# --------------------------------------------------------------------------- #
# WebSocket session
# --------------------------------------------------------------------------- #
# Module-level registry of connected sockets + the single ACTIVE voice session
# (BUG 1). Only the active socket plays audio and drives a turn; handling a turn
# PROMOTES the sender to active. We never close other sockets (avoids reconnect
# ping-pong) — we just mark them inactive via a {"type":"session"} message.
CONNECTIONS: "list[Session]" = []
ACTIVE_SESSION: "Session | None" = None


def _socket_alive(session: "Session") -> bool:
    """True only if the underlying WebSocket is still fully connected.

    A client that flaps (e.g. a phone over Tailscale) can leave a half-open
    socket whose `finally` cleanup never ran — a zombie. Starlette flips
    application_state to DISCONNECTED on a real close, so we treat anything not
    CONNECTED on both ends as dead.
    """
    ws = session.ws
    try:
        return (
            ws.application_state == WebSocketState.CONNECTED
            and ws.client_state == WebSocketState.CONNECTED
        )
    except Exception:
        return False


def _prune_dead_connections() -> None:
    """Drop zombie sockets from CONNECTIONS and clear a dead ACTIVE_SESSION.

    Called before any active-session decision so promotion never lands on a
    dead socket and a live client is never left muted behind a zombie. This is
    the core anti-cross-talk guard: phone + computer coexist because stale
    sockets can't hold the active slot."""
    global ACTIVE_SESSION
    dead = [s for s in CONNECTIONS if not _socket_alive(s)]
    for s in dead:
        CONNECTIONS.remove(s)
        logger.info("pruned dead socket (%d live)", len(CONNECTIONS))
    if ACTIVE_SESSION is not None and ACTIVE_SESSION not in CONNECTIONS:
        ACTIVE_SESSION = None

# Global turn lock (BUG 1): a SINGLE module-level lock shared by all sockets so
# only ONE turn runs at a time across every connected client. Acquired in
# _handle_text_turn and _handle_audio_blob around the whole turn. This replaces
# the old per-Session turn_lock (which only serialized turns on one socket and
# let two sockets talk at once).
TURN_LOCK = asyncio.Lock()


async def _set_active_session(session: "Session") -> None:
    """Make `session` the single active voice session and CLOSE every other
    connection so exactly one client is ever live.

    Why close instead of mute: a single-user voice assistant should only have
    ONE HUD connected. The old behavior kept other tabs open but muted — which
    proved fragile. A stale or unresponsive tab that ignored the mute kept
    playing its audio while a new tab also spoke, causing the recurring "double
    voice". Physically closing the others removes that whole failure class: the
    speaker holds the only open socket, so a second client literally cannot play.

    No reconnect war: closing happens on a TURN (a user action), not on connect.
    A closed tab may auto-reconnect, but it comes back MUTED (ACTIVE_SESSION is
    already set, so the connect path keeps it inactive) and is only re-closed on
    the next turn. Close code 4000 = "superseded" so the client can choose to go
    dormant rather than fight for the slot.
    """
    global ACTIVE_SESSION
    _prune_dead_connections()   # never promote behind / broadcast to zombies
    ACTIVE_SESSION = session

    # Promote the speaker (the single surviving socket).
    try:
        await session.send_json({"type": "session", "active": True})
        session.is_active = True
        session._session_notified = True
    except Exception as exc:
        logger.warning("failed to notify active session: %s", exc)

    # Close every OTHER connection. Best-effort and idempotent: each closed
    # socket's own finally will also run _remove_connection, but since we remove
    # it here and ACTIVE_SESSION is `session` (not the closed one), that path
    # won't re-promote anything — no cascade.
    others = [s for s in list(CONNECTIONS) if s is not session]
    for s in others:
        try:
            await s.send_json({"type": "session", "active": False})
        except Exception:
            pass
        try:
            await s.ws.close(code=4000, reason="superseded by active session")
        except Exception:
            pass
        for fut in list(s.pending.values()):
            if not fut.done():
                fut.set_result(False)
        s.pending.clear()
        s.is_active = False
        if s in CONNECTIONS:
            CONNECTIONS.remove(s)
    if others:
        logger.info(
            "single-client: closed %d superseded socket(s) — 1 active",
            len(others),
        )


def _remove_connection(session: "Session") -> "Session | None":
    """Drop a socket on disconnect. If it was active, return the most recent
    remaining socket to promote (or None if no sockets remain)."""
    global ACTIVE_SESSION
    if session in CONNECTIONS:
        CONNECTIONS.remove(session)
    if ACTIVE_SESSION is session:
        ACTIVE_SESSION = None
        _prune_dead_connections()
        # Promote the most recent socket that is actually still alive.
        for s in reversed(CONNECTIONS):
            if _socket_alive(s):
                return s
    return None


class Session:
    """Per-connection state: a registry of pending approvals + active flags.

    Turn serialization is NOT per-session anymore: a single module-level
    TURN_LOCK (BUG 1) serializes turns across ALL sockets so two clients can
    never run a turn (and thus speak) at the same time.
    """

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.pending: dict[str, asyncio.Future] = {}
        # Per-socket approval handler, passed into each ask() call (per-turn).
        self.approval_handler = None
        # Single-active-session bookkeeping (BUG 1). is_active gates audio
        # playback + turn driving on the client; _session_notified ensures the
        # client always gets at least one session state message.
        self.is_active = False
        self._session_notified = False
        # Single outbound queue + ONE drain task → every send to this socket is
        # serialized. Without this, a live turn and a background result writing to
        # the same WebSocket interleave frames and corrupt the audio stream
        # (deterministic — flagged by the review panel). All sends enqueue here.
        self._send_q: "asyncio.Queue" = asyncio.Queue()
        self._sender_task: "asyncio.Task | None" = None

    def start_sender(self) -> None:
        if self._sender_task is None:
            self._sender_task = asyncio.create_task(self._sender_loop())

    async def _sender_loop(self) -> None:
        fails = 0
        try:
            while True:
                item = await self._send_q.get()
                if item is None:
                    return
                kind, data = item
                try:
                    if kind == "json":
                        await self.ws.send_json(data)
                    else:
                        await self.ws.send_bytes(data)
                    fails = 0
                except Exception as exc:
                    fails += 1
                    logger.warning("send failed (%d): %s", fails, exc)
                    if fails >= 3:
                        return   # socket is genuinely dead — stop draining
        except asyncio.CancelledError:
            pass

    def stop_sender(self) -> None:
        if self._sender_task and not self._sender_task.done():
            self._sender_task.cancel()
        self._sender_task = None

    async def send_json(self, payload: dict) -> None:
        await self._send_q.put(("json", payload))

    async def send_bytes(self, data: bytes) -> None:
        await self._send_q.put(("bytes", data))

    def make_approval_handler(self, loop: asyncio.AbstractEventLoop):
        """Return an async (tool_name, tool_input) -> bool bound to this socket."""

        async def handler(tool_name: str, tool_input: dict) -> bool:
            approval_id = uuid.uuid4().hex
            fut: asyncio.Future = loop.create_future()
            self.pending[approval_id] = fut
            try:
                await self.send_json(
                    {
                        "type": "approval",
                        "id": approval_id,
                        "tool": tool_name,
                        "detail": summarize(tool_name, tool_input or {}),
                    }
                )
            except Exception as exc:
                logger.error("failed to send approval prompt: %s", exc)
                self.pending.pop(approval_id, None)
                return False  # cannot ask -> default deny

            try:
                approved = await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT)
                return bool(approved)
            except asyncio.TimeoutError:
                logger.info("approval %s timed out -> deny", approval_id)
                return False
            except Exception as exc:
                logger.error("approval %s errored: %s", approval_id, exc)
                return False
            finally:
                self.pending.pop(approval_id, None)

        return handler

    def resolve_approval(self, approval_id: str, approved: bool) -> None:
        fut = self.pending.get(approval_id)
        if fut is not None and not fut.done():
            fut.set_result(approved)


def _humanize_tool(tool_name: str) -> str:
    """Map a raw tool name to a short, friendly 'working' status for the HUD."""
    t = (tool_name or "").lower()
    if "supabase" in t or "execute_sql" in t:
        return "querying your data…"
    if "recall_vault" in t or "memory" in t or "secondbrain" in t:
        return "searching your notes…"
    if "chrome" in t:
        return "using the browser…"
    if "calendar" in t:
        return "checking your calendar…"
    if "gmail" in t or "google_workspace" in t:
        return "checking email…"
    if "ghl" in t or "crm" in t:
        return "checking your CRM…"
    if "stripe" in t:
        return "checking payments…"
    if any(s in t for s in ("firecrawl", "brave", "exa", "tavily", "web_search", "websearch")):
        return "searching the web…"
    if "open_app" in t or "open_url" in t or "applescript" in t or "see_screen" in t or "jarvis-mac" in t:
        return "controlling your computer…"
    if "toolsearch" in t:
        return "finding the right tool…"
    return "working…"


async def _run_text_turn(session: Session, text: str) -> None:
    """Run one full agent turn from already-transcribed text.

    Lock-free core: the caller MUST already hold the module-level TURN_LOCK so
    the whole turn (ask + reply send + tts send) is serialized against EVERY
    other socket's turn (BUG 1) — only one turn, and thus one voice, at a time.

    Stuck-on-thinking fix: the TEXT reply is ALWAYS attempted to the SENDER
    socket — it's their turn, so showing them the answer is always correct, even
    if they were deactivated mid-turn (e.g. a phone WS that flapped). Gating the
    text reply was what left the phone on "thinking" forever when the session
    dropped. Only the AUDIO (audio_incoming + mp3 bytes) is gated on this session
    still being the single ACTIVE session, so a deactivated client never PLAYS
    audio over the active tab (that's what prevents double-talk — showing text is
    fine). Every send is wrapped in try/except so a dead/closed socket
    ("Cannot call send once a close message has been sent") can't crash the turn.
    """
    # Stream tool activity to the sender so a long, actively-working turn shows
    # progress ("querying your data…") AND keeps the client's turn watchdog
    # alive — that's what stops "didn't come back — try again" firing on a big
    # multi-query investigation that's actually running fine.
    async def _on_activity(tool_name: str) -> None:
        try:
            await session.send_json(
                {"type": "status", "text": _humanize_tool(tool_name)}
            )
        except Exception:
            pass

    # STREAMING TTS pipeline (low-latency voice): synthesize + send each sentence
    # the model writes, IN ORDER, without blocking the model stream. A single
    # consumer task pulls sentences off a queue, synthesizes each in a thread, and
    # ships ordered audio chunks — so the first words play ~1s after the user stops
    # talking instead of after the whole reply is generated + synthesized.
    audio_q: "asyncio.Queue" = asyncio.Queue()
    sent_count = 0

    async def _on_sentence(sentence: str) -> None:
        # Non-blocking: just enqueue. The consumer does the slow TTS so the model
        # keeps streaming the next sentence while this one is synthesized.
        await audio_q.put(sentence)

    async def _audio_consumer() -> None:
        nonlocal sent_count
        while True:
            seg = await audio_q.get()
            try:
                if seg is None:          # sentinel — stream finished
                    return
                spoken = _spoken_text(seg)
                if not spoken:
                    continue
                # AUDIO gate (anti-double-talk): only the single active session plays.
                if not (session is ACTIVE_SESSION and session.is_active):
                    continue
                try:
                    mp3 = await asyncio.to_thread(synthesize, spoken)
                except Exception as exc:
                    logger.error("stream synth failed: %s", exc)
                    mp3 = None
                if mp3 and session is ACTIVE_SESSION and session.is_active:
                    try:
                        await session.send_json({"type": "audio_incoming"})
                        await session.send_bytes(mp3)
                        sent_count += 1
                    except Exception as exc:
                        logger.warning("failed to send stream audio (dead socket?): %s", exc)
            finally:
                audio_q.task_done()

    consumer = asyncio.create_task(_audio_consumer())

    # Heartbeat: ping the client every 15s while the agent works so the turn
    # watchdog never trips during long operations (e.g. a dispatched specialist
    # cold-booting MCP + querying). The client re-arms its watchdog on any status.
    async def _keepalive():
        try:
            while True:
                await asyncio.sleep(15)
                try:
                    await session.send_json({"type": "status", "text": "working…"})
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    _ka = asyncio.create_task(_keepalive())

    # Agent reply — per-socket approval handler + activity status + per-sentence TTS.
    try:
        result = await ask(
            text,
            on_approval=session.approval_handler,
            on_activity=_on_activity,
            on_sentence=_on_sentence,
        )
    finally:
        _ka.cancel()

    # ALWAYS send the on-screen TEXT reply (full, uncapped) to the sender — even if
    # they were deactivated mid-turn. A dead socket is harmless (try/except).
    try:
        await session.send_json(
            {
                "type": "reply",
                "text": result.get("reply", ""),
                "actions": result.get("actions", []),
            }
        )
    except Exception as exc:
        logger.warning("failed to send reply (dead socket?): %s", exc)

    # Drain the audio pipeline (let the consumer finish synthesizing/sending).
    await audio_q.put(None)
    try:
        await consumer
    except Exception as exc:
        logger.warning("audio consumer error: %s", exc)

    # Fallback: if streaming produced NO audio (model didn't stream, or the whole
    # reply was shorter than one sentence), synthesize the full reply once.
    if sent_count == 0:
        reply_text = (result.get("reply") or "").strip()
        spoken = _spoken_text(reply_text)
        if spoken and session is ACTIVE_SESSION and session.is_active:
            try:
                mp3 = await asyncio.to_thread(synthesize, spoken)
            except Exception as exc:
                logger.error("synthesize failed: %s", exc)
                mp3 = None
            if mp3 and session is ACTIVE_SESSION and session.is_active:
                try:
                    await session.send_json({"type": "audio_incoming"})
                    await session.send_bytes(mp3)
                except Exception as exc:
                    logger.warning("failed to send audio (dead socket?): %s", exc)

    # Tell the client the turn's audio is complete so it finalizes playback:
    # drains its queue, returns the orb to idle, and restarts the wake recognizer.
    try:
        await session.send_json({"type": "audio_end"})
    except Exception:
        pass


async def _speak_to_session(session: Session, text: str, spoken_text: str | None = None) -> None:
    """Push a one-off spoken message (text reply + audio) to a session, OUTSIDE the
    normal turn loop — used to deliver a finished background-job result when the user
    is idle. Best-effort: a dead/inactive socket just no-ops (audio is gated on active).

    `text` is shown on screen (full). `spoken_text`, if given, is what's read aloud
    instead — so a long background analysis shows in full but is spoken briefly
    (no 2-minute monologue / multi-MB mp3).
    """
    text = (text or "").strip()
    if not text:
        return
    try:
        await session.send_json({"type": "reply", "text": text, "actions": []})
    except Exception:
        pass
    spoken = _spoken_text(spoken_text if spoken_text is not None else text)
    if spoken and session is ACTIVE_SESSION and session.is_active:
        try:
            mp3 = await asyncio.to_thread(synthesize, spoken)
        except Exception as exc:
            logger.error("job synth failed: %s", exc)
            mp3 = None
        if mp3 and session is ACTIVE_SESSION and session.is_active:
            try:
                await session.send_json({"type": "audio_incoming"})
                await session.send_bytes(mp3)
            except Exception as exc:
                logger.warning("failed to send job audio (dead socket?): %s", exc)
    try:
        await session.send_json({"type": "audio_end"})
    except Exception:
        pass


async def _deliver_job_result(job: dict) -> None:
    """Delivery callback for a finished background specialist job (registered with
    jarvis.jobs). HYBRID delivery:
      • If the user is IDLE (no turn running) and a live session is active → SPEAK it now.
      • If a turn is in flight (TURN_LOCK held) or nobody's active → just CHIME the
        client ({"type":"job_notice"}); the full result is already woven into the next
        turn's context (jobs.pending_results), so Jarvis relays it when he next speaks.
    This is what stops a background result from talking over an in-progress conversation.
    """
    _prune_dead_connections()
    session = ACTIVE_SESSION
    specialist = (job.get("specialist") or "a specialist").strip()
    result = str(job.get("result", "")).strip()
    if not result:
        return
    # Speak only if idle. acquire() on a FREE asyncio.Lock returns synchronously
    # (no await yield), so checking locked() then acquiring is race-free in this
    # single-threaded loop — and avoids the wait_for(lock.acquire()) leak footgun.
    got_lock = False
    if session is not None and session.is_active and _socket_alive(session) and not TURN_LOCK.locked():
        await TURN_LOCK.acquire()
        got_lock = True
    if got_lock:
        try:
            # Full result on screen; speak it ONLY if it's short. A long analysis
            # gets a brief spoken lead-in ("…it's on your screen") so it never
            # becomes a multi-minute monologue / multi-MB mp3 on the phone.
            display = f"{specialist.capitalize()} is back. {result}"
            if len(result) <= JOB_SPOKEN_MAX:
                await _speak_to_session(session, display)
            else:
                brief = f"{specialist.capitalize()}'s back with the breakdown — it's on your screen."
                await _speak_to_session(session, display, spoken_text=brief)
            job["spoken"] = True
            logger.info("spoke background result from %s (%d chars, brief=%s)",
                        specialist, len(result), len(result) > JOB_SPOKEN_MAX)
        finally:
            TURN_LOCK.release()
        return
    # Busy or nobody active → soft chime + chip; result relayed on the next turn.
    if session is not None:
        try:
            await session.send_json({
                "type": "job_notice",
                "specialist": specialist,
                "summary": result[:200],
            })
            logger.info("chimed background result from %s (busy)", specialist)
        except Exception:
            pass


async def _handle_text_turn(session: Session, text: str) -> None:
    """Run one full agent turn from already-transcribed text (text path).

    Handling a turn PROMOTES this socket to the single active session (BUG 1):
    switching tabs "just works" — talking/typing here takes over and mutes the
    others. Other sockets are marked inactive, never closed. This is the ONLY
    place promotion happens now (connecting no longer promotes). The whole turn
    runs under the module-level TURN_LOCK so only one turn runs at a time.
    """
    await _set_active_session(session)
    async with TURN_LOCK:
        await _run_text_turn(session, text)


async def _handle_audio_blob(session: Session, blob: bytes) -> None:
    """Transcribe a recorded audio blob, then run the full agent turn.

    The ENTIRE turn (transcribe + transcript send + ask + tts send) runs under
    the module-level TURN_LOCK so only one turn runs at a time across ALL sockets
    (BUG 1). _run_text_turn is the lock-free core (no double-acquire / deadlock).

    Handling a turn PROMOTES this socket to the single active session (BUG 1):
    talking here takes over and mutes the other tabs (which are not closed). This
    is the ONLY place promotion happens now (connecting no longer promotes).
    """
    await _set_active_session(session)
    async with TURN_LOCK:
        tmp_path = STATE_DIR / f"rec-{uuid.uuid4().hex}.webm"
        text = ""
        try:
            tmp_path.write_bytes(blob)
            await session.send_json({"type": "status", "text": "transcribing"})
            # transcribe is blocking (faster-whisper) -> always run in a thread.
            # The speech engine is optional: if faster-whisper isn't installed,
            # transcribe() raises ImportError. Catch it (and any transcription
            # failure) so the turn ends with a friendly status instead of crashing.
            try:
                text = await asyncio.to_thread(transcribe, str(tmp_path))
            except ImportError as exc:
                logger.error("speech engine not installed: %s", exc)
                await session.send_json({
                    "type": "status",
                    "text": "speech engine not installed — install faster-whisper to use voice input.",
                })
                return
            except Exception as exc:
                logger.error("transcription failed: %s", exc)
                await session.send_json({
                    "type": "status",
                    "text": "couldn't transcribe that — try again.",
                })
                return
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

        text = (text or "").strip()
        await session.send_json({"type": "transcript", "text": text})
        if not text:
            await session.send_json(
                {"type": "status", "text": "no speech detected"}
            )
            return
        await _run_text_turn(session, text)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    session = Session(ws)
    session.start_sender()   # start the single outbound drain before any sends
    loop = asyncio.get_running_loop()

    # Build the per-socket approval handler. It is passed into each ask() call
    # (per-turn) rather than set on the module global at connection time, so two
    # concurrent sockets (desk + phone) can't overwrite each other's handler.
    session.approval_handler = session.make_approval_handler(loop)

    # Single-active-session (BUG 1 + stuck-muted regression fix):
    # Register this socket, then decide its initial state by whether anyone is
    # already active:
    #   • If NO session is active (ACTIVE_SESSION is None), PROMOTE this socket so
    #     a solo / first client is live immediately (gets {"session":true}). This
    #     fixes "solo client opens and is stuck muted, can't take over."
    #   • If a session IS already active, keep this socket INACTIVE (gets
    #     {"session":false}) so a 2nd client doesn't steal the slot on connect.
    # A muted client can still take over later by talking/typing — the turn
    # handlers promote the sender (see _handle_text_turn / _handle_audio_blob).
    CONNECTIONS.append(session)
    _prune_dead_connections()   # clear any zombie holding the active slot first
    if ACTIVE_SESSION is None:
        # Solo / first client — promote immediately. _set_active_session sends
        # {"session":true} to this socket and {"session":false} to any others.
        await _set_active_session(session)
        logger.info(
            "ws connected (promoted to active — no other session); "
            "per-socket approval handler ready (%d live)",
            len(CONNECTIONS),
        )
    else:
        # Another session is already active — start this one muted. It can take
        # over by talking/typing (the turn handlers promote the sender).
        session.is_active = False
        try:
            await session.send_json({"type": "session", "active": False})
            session._session_notified = True
        except Exception as exc:
            logger.warning("failed to send initial inactive session state: %s", exc)
        logger.info(
            "ws connected (inactive — another session active); "
            "per-socket approval handler ready (%d live)",
            len(CONNECTIONS),
        )

    try:
        await session.send_json({"type": "status", "text": "ready"})
        while True:
            message = await ws.receive()

            if message.get("type") == "websocket.disconnect":
                break

            try:
                if "bytes" in message and message["bytes"] is not None:
                    # Binary frame = recorded audio blob (webm/opus).
                    await _handle_audio_blob(session, message["bytes"])

                elif "text" in message and message["text"] is not None:
                    data = _safe_json(message["text"])
                    if data is None:
                        continue
                    mtype = data.get("type")

                    if mtype == "text":
                        text = (data.get("text") or "").strip()
                        if text:
                            await session.send_json(
                                {"type": "transcript", "text": text}
                            )
                            await _handle_text_turn(session, text)

                    elif mtype == "approval_response":
                        session.resolve_approval(
                            str(data.get("id", "")), bool(data.get("approved"))
                        )

                    elif mtype == "ping":
                        await session.send_json({"type": "pong"})

            except WebSocketDisconnect:
                raise
            except Exception as exc:
                # One bad message must not kill the socket.
                logger.exception("error handling message: %s", exc)
                try:
                    await session.send_json(
                        {"type": "status", "text": f"error: {exc}"}
                    )
                except Exception:
                    pass

    except WebSocketDisconnect:
        logger.info("ws disconnected")
    except Exception as exc:
        logger.exception("ws fatal error: %s", exc)
    finally:
        # Fail any in-flight approvals so the agent turn unblocks (deny).
        for fut in list(session.pending.values()):
            if not fut.done():
                fut.set_result(False)
        session.pending.clear()
        session.stop_sender()   # tear down the outbound drain task
        # Single-active-session (BUG 1): drop this socket. If it was the active
        # one, the most recent remaining socket is promoted (or none remain).
        promote = _remove_connection(session)
        if promote is not None:
            try:
                await _set_active_session(promote)
            except Exception as exc:
                logger.warning("failed to promote remaining session: %s", exc)
        # No module-global handler to detach: it's passed per-turn into ask()
        # and cleared by ask() itself, so concurrent sockets stay isolated.
        logger.info("ws cleanup complete (%d live)", len(CONNECTIONS))


HEARTBEAT_INTERVAL_S = 15


async def _heartbeat_sweep() -> None:
    """Background loop: periodically prune zombie sockets so a flapping client
    (phone over Tailscale) can't keep the active slot and cause cross-talk. If
    the active session died with live sockets remaining, promote a live one."""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)
        try:
            before = len(CONNECTIONS)
            had_active = ACTIVE_SESSION is not None
            _prune_dead_connections()
            # Active socket died but others are alive → hand off to a live one.
            if had_active and ACTIVE_SESSION is None:
                for s in reversed(CONNECTIONS):
                    if _socket_alive(s):
                        await _set_active_session(s)
                        break
            if len(CONNECTIONS) != before:
                logger.info("heartbeat pruned %d → %d sockets", before, len(CONNECTIONS))
        except Exception as exc:
            logger.warning("heartbeat sweep error: %s", exc)


@app.on_event("startup")
async def _start_heartbeat() -> None:
    asyncio.create_task(_heartbeat_sweep())
    # Pre-warm the agent client + MCP subprocesses so the first voice turn after a
    # restart is fast (not a ~30s cold boot with "tools reconnecting").
    asyncio.create_task(warmup())
    # Background-job orchestration: kill any worker process-groups left over from a
    # previous run, invalidate in-flight epochs, and register the speak/chime
    # delivery callback so finished specialist jobs come back to the HUD.
    try:
        from jarvis import jobs
        jobs.reap_orphans()
        jobs.bump_epoch()
        jobs.set_delivery(_deliver_job_result)
        logger.info("background job delivery registered")
    except Exception as exc:
        logger.warning("could not init background jobs: %s", exc)


def _safe_json(raw: str):
    import json

    try:
        return json.loads(raw)
    except Exception:
        logger.warning("ignoring non-JSON text frame")
        return None


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=JARVIS_PORT)
