"""
jarvis/fastlane.py — direct Anthropic Messages API conversational engine.

The "fast lane": instead of the Claude Code SDK (subprocess + MCP + tool-search,
~4–5s warm floor), this talks straight to the Anthropic Messages API with
streaming + native tool-use → first token in ~1–2s, Trillion-style.

It exposes `ask()` with the SAME signature as `jarvis.agent.ask`, so server.py
can swap engines with no other changes (select via JARVIS_ENGINE=fastlane).

Architecture (orchestrator pattern):
  • FAST native tools (direct Python, no MCP): recall_vault, remember,
    query_supabase (your connected database), local machine control.
  • EVERYTHING ELSE — full Google Workspace, web, browser, coding — is delegated
    to a full Claude Code agent via run_claude_code, which runs on the user's
    Claude subscription (not metered). So only the small conversational turns hit
    the metered API; the heavy work stays free.

Billing: this path uses ANTHROPIC_API_KEY (pay-per-token). The delegated
run_claude_code work uses the Claude subscription. Prompt caching on the system
prompt + tools keeps the metered cost low.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import urllib.request
from datetime import datetime
from typing import Awaitable, Callable

import anthropic
from dotenv import load_dotenv

from jarvis.agent import JARVIS_PERSONA, _pop_sentences
from jarvis.memory import recall as _recall, remember as _remember
from jarvis.mac_control import (
    open_app as _open_app,
    open_url as _open_url,
    install_app as _install_app,
    see_screen as _see_screen,
    run_applescript as _run_applescript,
    capture_screen_b64 as _capture_screen_b64,
)
from jarvis.delegate import run_claude_code as _run_claude_code
from jarvis.google_fast import (
    calendar_agenda as _calendar_agenda,
    email_inbox as _email_inbox,
)
from jarvis import roster as _roster
from jarvis import jobs as _jobs

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

logger = logging.getLogger("jarvis.fastlane")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [fastlane] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

MODEL = os.environ.get("FASTLANE_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.environ.get("FASTLANE_MAX_TOKENS", "4096"))
HISTORY_MAX = int(os.environ.get("FASTLANE_HISTORY_MAX", "24"))  # ~12 turns; memory holds the rest

SB_TOKEN = os.environ.get("SUPABASE_ACCESS_TOKEN", "")
SB_REF = os.environ.get("SUPABASE_PROJECT_REF", "")  # your Supabase project (optional)

# Conductor addendum — appended to the persona so JARVIS acts as the orchestrator.
_CONDUCTOR = (
    "\n\nYou are also the CONDUCTOR of a specialist team. Handle quick things YOURSELF with your fast tools "
    "(memory, your connected database, calendar, email, the local machine) — that's the default. "
    "Calendar and email are INSTANT native tools IF the user has connected their Google account: use "
    "`calendar_agenda` for 'what's my day / schedule / meetings' and `email_inbox` for 'any important email / "
    "check my inbox' — directly, NEVER delegate those. Summarize email like a chief of staff (what matters + who's "
    "waiting); never read every message verbatim. To SEND or reply to email, use run_claude_code. "
    "DATABASE data (anything stored in the user's connected database — records, totals, counts, lookups): "
    "ALWAYS give the QUICK answer FIRST with `query_supabase` — the headline number, instantly. Never go silent. "
    "THEN, if a deeper breakdown / fuller analysis was asked for, or it's a longer multi-step data task, ALSO "
    "dispatch LEDGER in the SAME turn for the full picture — Ledger runs in the BACKGROUND, so give the quick "
    "answer now, say Ledger's pulling the rest, and CARRY ON; Ledger's result comes back automatically (noted at "
    "the top of a later turn) — relay it then. So: quick answer immediately, deep answer follows. ATLAS (web "
    "research) and SPARK (content) also run in the background — same pattern: acknowledge, carry on, relay when it "
    "lands. (Spark DRAFTS content/assets in the background and hands them back for the user's OK; it won't "
    "publish/post live unless explicitly told to.) Never read raw data aloud — always summarize. "
    "Split independent work across SEVERAL dispatches at once. "
    "Use `cancel_jobs` to stop the team, and `create_specialist` to add a new role. Keep narrating tight."
)

_client: anthropic.AsyncAnthropic | None = None
_history: list[dict] = []
_LOCK = asyncio.Lock()


def _get_client() -> anthropic.AsyncAnthropic | None:
    global _client
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


# --------------------------------------------------------------------------- #
# Native fast tools (direct Python — no MCP, no subprocess)
# --------------------------------------------------------------------------- #
def _query_supabase(sql: str) -> str:
    """Read-only SQL against the user's connected database via the Supabase Management API."""
    s = (sql or "").strip().rstrip(";")
    low = s.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return "Only read-only SELECT/WITH queries are allowed. For anything else use run_claude_code."
    if not SB_TOKEN or not SB_REF:
        return ("No database connected — add SUPABASE_ACCESS_TOKEN and SUPABASE_PROJECT_REF in .env, "
                "or use run_claude_code.")
    try:
        req = urllib.request.Request(
            f"https://api.supabase.com/v1/projects/{SB_REF}/database/query",
            data=json.dumps({"query": s}).encode(),
            headers={
                "Authorization": f"Bearer {SB_TOKEN}",
                "Content-Type": "application/json",
                # Supabase's API sits behind Cloudflare, which 403s the default
                # "Python-urllib" User-Agent. A real UA is required or every call fails.
                "User-Agent": "Mozilla/5.0 jarvis-fastlane/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode()[:4000]
    except Exception as exc:
        return f"Supabase query failed: {exc}"


def _recall_str(query: str, k: int = 6) -> str:
    hits = _recall(query, k=k)
    if not hits:
        return "No relevant memory or vault content found."
    return "\n".join(f"[{round(h['score'],3)}] {h.get('text','')[:300]}" for h in hits)


def _remember_str(text: str) -> str:
    res = _remember(text)
    return "Noted." if res.get("ok") else f"Couldn't save: {res.get('error','unknown')}"


def _see_screen_blocks(prompt: str = ""):
    """Capture the screen and return VISION content blocks (text + image) so the
    model can actually READ it — not just a text path. Returns a string on failure
    (which becomes a normal text tool_result)."""
    cap = _capture_screen_b64()
    if not cap.get("ok"):
        return cap.get("error", "Couldn't capture the screen.")
    note = ("Here is a screenshot of the user's screen right now. Read it and answer: "
            + (prompt.strip() or "what's on screen?"))
    return [
        {"type": "text", "text": note},
        {"type": "image", "source": {
            "type": "base64", "media_type": cap["media_type"], "data": cap["data"]}},
    ]


async def _dispatch_async(specialist: str, task: str) -> str:
    """Route a task to a named specialist. Background-eligible (read-only) specialists
    run as non-blocking jobs so JARVIS stays responsive; the rest run in-turn."""
    name = (specialist or "").strip().lower()
    team = _roster.load()
    spec = team.get(name)
    if not spec:
        return (f"No specialist '{specialist}'. Available: {', '.join(team)}. "
                "Use run_claude_code for a generic task, or create_specialist to add a new role.")
    if spec.get("bg"):
        res = await _jobs.submit(name, task, spec["system_prompt"], spec.get("mcp"))
        if res.get("ok"):
            return (f"On it — {name} is working in the background (job #{res['id']}). "
                    "I'll report back when it's done; keep talking to me meanwhile.")
        return res.get("msg", "Couldn't dispatch right now.")
    # Foreground (mutating specialists run synchronously, in-turn). spec["mcp"]: a
    # list scopes the worker; None (Flux) inherits the full toolset.
    return await asyncio.to_thread(
        lambda: _run_claude_code(task, system_prompt=spec["system_prompt"], mcp_servers=spec.get("mcp")))


def _create_specialist(name: str, description: str = "", system_prompt: str = "") -> str:
    """Agent factory — persist a new specialist so it's dispatchable."""
    res = _roster.save_specialist(name, description, system_prompt)
    if res.get("ok"):
        return (f"Specialist '{res['name']}' created and dispatchable now via "
                f"dispatch(specialist='{res['name']}', task=...).")
    return f"Couldn't create specialist: {res.get('error')}"


# name -> (sync callable, [arg names])
_TOOL_FUNCS: dict[str, tuple] = {
    "recall_vault":    (_recall_str, ["query", "k"]),
    "remember":        (_remember_str, ["text"]),
    "query_supabase":  (_query_supabase, ["sql"]),
    "open_app":        (_open_app, ["name"]),
    "open_url":        (_open_url, ["url"]),
    "run_applescript": (_run_applescript, ["script"]),
    "see_screen":      (_see_screen, ["prompt"]),
    "install_app":     (_install_app, ["name"]),
    "run_claude_code": (_run_claude_code, ["task"]),
    "create_specialist": (_create_specialist, ["name", "description", "system_prompt"]),
    "calendar_agenda": (_calendar_agenda, ["when"]),
    "email_inbox": (_email_inbox, ["query", "max_results"]),
}

TOOLS = [
    {"name": "recall_vault",
     "description": "Search your saved memory and notes by meaning. Use for anything the user has noted, decided, said, planned, or asked you to remember.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "k": {"type": "integer"}}, "required": ["query"]}},
    {"name": "remember",
     "description": "Save a durable memory (a preference, decision, or fact about the user) so you recall it later. One clear sentence.",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "query_supabase",
     "description": "FAST, INSTANT read-only SQL SELECT against the user's connected database (if one is configured). This is your FIRST CHOICE for any number, count, or lookup from their data — use it directly, do NOT dispatch the Ledger specialist for these. If no database is connected it returns a friendly note.",
     "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}},
    {"name": "calendar_agenda",
     "description": "FAST, INSTANT read of the user's Google Calendar if connected, for ANY day. `when` accepts: today, tonight, tomorrow, any weekday name (monday…sunday → next occurrence), 'week', 'next week', 'weekend', or an ISO date 'YYYY-MM-DD'. ALWAYS use this for ANY calendar/schedule/'what's on Monday' question — NEVER delegate calendar to a worker.",
     "input_schema": {"type": "object", "properties": {"when": {"type": "string"}}, "required": []}},
    {"name": "email_inbox",
     "description": "FAST, INSTANT read of the user's Gmail if connected. Use for 'any important emails', 'check my inbox', 'unread email'. `query` is Gmail search syntax: 'is:unread' (default), 'is:important newer_than:2d', 'from:someone', etc. Read-only — SUMMARIZE what matters, never read every email verbatim. To SEND/reply, use run_claude_code instead.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}}, "required": []}},
    {"name": "open_app",
     "description": "Open or focus any installed app by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "open_url",
     "description": "Open a URL/website in the user's default browser (logged-in). Also opens web apps.",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "run_applescript",
     "description": "Run an AppleScript for native automation (macOS only).",
     "input_schema": {"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]}},
    {"name": "see_screen",
     "description": "Take a screenshot of the user's screen and SEE it — the actual image comes back to you to read (text, UI, errors, anything visible). Pass `prompt` for what to look for.",
     "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": []}},
    {"name": "install_app",
     "description": "Install an app via the system package manager.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "run_claude_code",
     "description": ("Delegate ANY task beyond your fast tools to a full Claude Code agent that has the user's "
                     "connected tools: Google Workspace (Gmail, Calendar, Drive, Docs, Sheets), web search, "
                     "real browser automation, writing/running code, and any other integration they've set up. "
                     "Give one clear, complete instruction; it runs autonomously and returns a short summary to speak. "
                     "Use this for email, calendar, browser, web research, and coding. Takes ~5–60s."),
     "input_schema": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}},
    {"name": "dispatch",
     "description": ("Hand a task to a SPECIALIST sub-agent on your team (each runs as a worker with its own tools, "
                     "on the subscription = free). Your team:\n" + _roster.describe_team() +
                     "\nBackground specialists (atlas=research, ledger=deep data analysis, spark=content drafts) "
                     "run in the BACKGROUND — dispatch returns immediately and you keep talking with the user; their "
                     "result comes back when ready, so just acknowledge and move on, don't wait. (Spark only DRAFTS in "
                     "the background — it won't publish live unless explicitly told.) Other specialists run inline and "
                     "return their summary. You may dispatch MULTIPLE in one turn (they run in parallel). Max 2 background jobs at once."),
     "input_schema": {"type": "object", "properties": {"specialist": {"type": "string"}, "task": {"type": "string"}}, "required": ["specialist", "task"]}},
    {"name": "cancel_jobs",
     "description": "Stop/kill all running background specialist jobs. Use when the user says to cancel, stop, or abort the team's background work.",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "create_specialist",
     "description": ("Agent factory — create a NEW specialist sub-agent when no existing one fits a recurring need. "
                     "Give a short lowercase name, a one-line description, and a system_prompt defining its role and "
                     "how it should work. It becomes dispatchable immediately."),
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "system_prompt": {"type": "string"}}, "required": ["name", "system_prompt"]}},
]


async def _run_tool(name: str, args: dict) -> str:
    # Async-native tools (run on the loop, not in a thread).
    if name == "dispatch":
        return await _dispatch_async(args.get("specialist", ""), args.get("task", ""))
    if name == "cancel_jobs":
        n = await _jobs.cancel_all()
        return f"Cancelled {n} background job(s)." if n else "No background jobs were running."
    if name == "see_screen":
        # Returns a list of vision content blocks (text + image) so the model can
        # actually read the screen — the tool-result loop handles list output.
        return await asyncio.to_thread(_see_screen_blocks, args.get("prompt", ""))
    entry = _TOOL_FUNCS.get(name)
    if not entry:
        return f"Unknown tool: {name}"
    func, keys = entry
    kwargs = {k: args[k] for k in keys if k in args and args[k] is not None}
    try:
        result = await asyncio.to_thread(lambda: func(**kwargs))
        return str(result) if result is not None else "Done."
    except Exception as exc:
        logger.error("tool %s failed: %s", name, exc)
        return f"Error running {name}: {exc}"


def _trim_history() -> None:
    """Cap history WITHOUT orphaning a tool_result at the head.

    The API requires every `tool_result` to immediately follow its `tool_use`. A
    blind front-cut can leave the window STARTING on a user message full of
    tool_results whose assistant tool_use was trimmed away → 400 invalid_request
    ("unexpected tool_use_id ... must have a corresponding tool_use block"). So we
    cut to a clean turn boundary: the first kept message must be a user message with
    plain STRING content (a real user turn or a system note), never a tool_result
    carrier or an assistant message. Worst case keeps just the current turn."""
    global _history
    if len(_history) <= HISTORY_MAX:
        return
    start = len(_history) - HISTORY_MAX
    n = len(_history)
    while start < n:
        m = _history[start]
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            break
        start += 1
    if start > 0:
        del _history[:start]


def _scrub_history_images() -> None:
    """Replace base64 image blocks in PAST tool_results with a tiny text placeholder.

    A screenshot is needed only for the turn it was taken in (the model reads it in
    that turn's tool loop). Left in history it would be re-sent — and re-billed —
    on every subsequent turn (megabytes each). This drops them after the fact while
    keeping the conversation coherent ("a screenshot was shown")."""
    for msg in _history:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "tool_result" and isinstance(blk.get("content"), list):
                blk["content"] = [
                    ({"type": "text", "text": "[screenshot was shown and read]"}
                     if isinstance(x, dict) and x.get("type") == "image" else x)
                    for x in blk["content"]
                ]


def _turn_prefix() -> str:
    now = datetime.now()
    hour12 = now.hour % 12 or 12   # 0->12, 13->1 (portable: avoids glibc-only %-I)
    return (
        f"[System: current time {now.strftime(f'%A %B {now.day} %Y {hour12}:%M %p')} local.]\n"
        "[Voice check — stay fully in character: JARVIS, the user's witty, no-nonsense AI. "
        "Brief (1–2 sentences), witty, a little savage, casual. "
        "Lead with the answer, land a jab if earned. Don't drift to generic-assistant.]\n"
    )


# --------------------------------------------------------------------------- #
# Public API — drop-in for jarvis.agent.ask
# --------------------------------------------------------------------------- #
async def ask(
    text: str,
    on_approval: Callable | None = None,        # accepted for signature parity; fast lane is bypass
    on_activity: Callable[[str], Awaitable[None]] | None = None,
    on_sentence: Callable[[str], Awaitable[None]] | None = None,
) -> dict:
    client = _get_client()
    if client is None:
        return {"reply": "My fast-lane API key isn't set, sir — add ANTHROPIC_API_KEY to .env and restart me.",
                "actions": [], "session_id": None}

    actions: list[dict] = []
    parts: list[str] = []
    system = [{"type": "text", "text": JARVIS_PERSONA + _CONDUCTOR, "cache_control": {"type": "ephemeral"}}]

    async with _LOCK:
        # Drop any screenshots from earlier turns so we don't re-send megabytes of
        # base64 every turn (the model already read them when they were taken).
        _scrub_history_images()
        # Weave in any background-job results that landed since the last turn, so the
        # conductor has them in context (and knows which were already spoken aloud).
        for j in _jobs.pending_results():
            tag = ("already spoken to the user" if j.get("spoken")
                   else "NOT yet told to the user — mention it now")
            _history.append({"role": "user",
                "content": f"[System: background job from {j['specialist']} finished ({tag}): "
                           f"{str(j.get('result',''))[:1500]}]"})
        _history.append({"role": "user", "content": _turn_prefix() + text})
        _trim_history()
        try:
            for _ in range(8):  # tool-loop guard
                stream_buf = ""
                async with client.messages.stream(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=system,
                    tools=TOOLS,
                    thinking={"type": "disabled"},   # snappy voice; heavy reasoning is delegated
                    messages=_history,
                ) as stream:
                    async for event in stream:
                        if event.type == "content_block_delta" and getattr(event.delta, "type", None) == "text_delta":
                            piece = event.delta.text or ""
                            if piece and on_sentence:
                                stream_buf += piece
                                sents, stream_buf = _pop_sentences(stream_buf)
                                for s in sents:
                                    try:
                                        await on_sentence(s)
                                    except Exception:
                                        pass
                    final = await stream.get_final_message()

                if on_sentence and stream_buf.strip():
                    sents, _r = _pop_sentences(stream_buf, flush=True)
                    for s in sents:
                        try:
                            await on_sentence(s)
                        except Exception:
                            pass

                for b in final.content:
                    if b.type == "text":
                        parts.append(b.text)
                _history.append({"role": "assistant", "content": final.content})

                if final.stop_reason != "tool_use":
                    break

                tool_uses = [b for b in final.content if b.type == "tool_use"]
                if on_activity:
                    for b in tool_uses:
                        try:
                            await on_activity(b.name)
                        except Exception:
                            pass
                # Run all tool calls in this turn CONCURRENTLY — multiple dispatch()
                # calls fan out across the specialist team in parallel.
                outs = await asyncio.gather(
                    *[_run_tool(b.name, b.input or {}) for b in tool_uses],
                    return_exceptions=True,
                )
                tool_results = []
                for b, out in zip(tool_uses, outs):
                    if isinstance(out, Exception):
                        out = f"Error running {b.name}: {out}"
                    if isinstance(out, list):
                        # Structured vision content (e.g. see_screen → text + image).
                        actions.append({"tool": b.name, "input": b.input, "result": "[screenshot sent to vision]"})
                        tool_results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
                    else:
                        actions.append({"tool": b.name, "input": b.input, "result": str(out)[:200]})
                        tool_results.append({"type": "tool_result", "tool_use_id": b.id, "content": str(out)})
                _history.append({"role": "user", "content": tool_results})
        except anthropic.AuthenticationError:
            return {"reply": "My API key was rejected, sir — check ANTHROPIC_API_KEY.", "actions": actions, "session_id": None}
        except Exception as exc:
            logger.exception("fastlane turn failed: %s", exc)
            return {"reply": f"Hit a snag on the fast lane: {exc}", "actions": actions, "session_id": None}

    reply = "".join(parts).strip() or "(no response)"
    return {"reply": reply, "actions": actions, "session_id": None}


async def warmup() -> None:
    """Pre-warm the prompt cache (system + tools) so the first real turn is fast."""
    client = _get_client()
    if client is None:
        logger.info("warmup skipped: no ANTHROPIC_API_KEY")
        return
    try:
        await client.messages.create(
            model=MODEL,
            max_tokens=0,
            system=[{"type": "text", "text": JARVIS_PERSONA + _CONDUCTOR, "cache_control": {"type": "ephemeral"}}],
            tools=TOOLS,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": "warmup"}],
        )
        logger.info("warmup: fast-lane cache primed")
    except Exception as exc:
        logger.warning("warmup failed: %s", exc)
