"""
Jarvis — Claude Agent core.

Provides a single async entry point:

    from jarvis.agent import ask
    result = await ask("do something")
    # -> {"reply": str, "actions": list[dict], "session_id": str | None}

One long-lived ClaudeSDKClient is kept alive across calls so conversation
memory persists for the session.  On any connection error the client
reconnects automatically (resuming the same session where possible).

The approval hook is pluggable and scoped per-turn:

    result = await ask("do something", on_approval=my_async_fn)
    # my_async_fn: async (tool_name, tool_input) -> bool

`ask()` installs on_approval into the module-level APPROVAL_HANDLER inside the
global query lock and clears it when the turn ends.  This is race-free because
the lock lets only one turn run at a time, so can_use_tool always reads the
handler belonging to the active turn.

If on_approval is None (default), risky tools are allowed in MVP mode with a
TODO comment.  Set PERMISSION_MODE=bypassPermissions in .env to skip all
gating entirely.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Callable, Awaitable

from dotenv import load_dotenv

from jarvis import guardrails

# --------------------------------------------------------------------------- #
# IMPORT SAFETY
# --------------------------------------------------------------------------- #
# This module is imported unconditionally by server.py, so `import jarvis.agent`
# MUST succeed on a machine that only has the lightweight runtime deps
# (fastapi, uvicorn, anthropic, python-dotenv, requests, httpx, faster-whisper).
#
# Every heavy / optional dependency — claude_agent_sdk and the sibling control
# modules — is therefore imported LAZILY inside the function that needs it, wrapped
# in try/except so a missing dep degrades gracefully at call time instead of
# crashing at import.
#
# The in-process MCP tools below are likewise defined inside _build_mcp_servers()
# rather than at module scope, because the @tool decorator and
# create_sdk_mcp_server come from claude_agent_sdk.

# --------------------------------------------------------------------------- #
# Streaming helper — pull complete sentences out of a growing text buffer so the
# server can synthesize + speak each sentence AS the model writes it (low-latency
# voice). Short fragments are carried forward so we never speak tiny choppy clips.
# --------------------------------------------------------------------------- #
_SENT_BOUNDARY = re.compile(r'[.!?…]+["\')\]]*(\s)')


def _pop_sentences(buf: str, flush: bool = False, _min: int = 25) -> tuple[list[str], str]:
    """Return (complete_sentences, remainder) from a streaming text buffer.

    Without flush, the trailing partial chunk stays in `remainder` for the next
    delta. With flush=True, whatever's left is emitted as a final sentence.
    Fragments shorter than `_min` chars are carried forward and merged so we
    don't synthesize choppy one-word clips.
    """
    out: list[str] = []
    carry = ""
    last = 0
    for m in _SENT_BOUNDARY.finditer(buf):
        cut = m.start(1)  # end of the punctuation, just before the whitespace
        seg = (carry + buf[last:cut]).strip()
        last = m.end()
        if not seg:
            carry = ""
            continue
        if len(seg) < _min:
            carry = seg + " "
        else:
            out.append(seg)
            carry = ""
    remainder = carry + buf[last:]
    if flush:
        tail = remainder.strip()
        if tail:
            if out and len(tail) < _min:
                out[-1] = (out[-1] + " " + tail).strip()
            else:
                out.append(tail)
        remainder = ""
    return out, remainder


# --------------------------------------------------------------------------- #
# In-process MCP tools — built lazily (claude_agent_sdk required)
# --------------------------------------------------------------------------- #
# The @tool decorator and create_sdk_mcp_server come from claude_agent_sdk, a
# heavy/optional dependency. Defining the tools at module scope would crash
# `import jarvis.agent` on a machine without the SDK. So all tool definitions
# and server construction live inside _build_mcp_servers(), called once from
# build_client() (and memoized). The sibling control modules (memory, delegate,
# mac_control, chrome_control) are imported lazily inside each tool body and
# wrapped in try/except so a missing dependency degrades to a clear message at
# call time instead of crashing the process.
_INPROC_MCP_SERVERS: dict | None = None


def _build_mcp_servers() -> dict:
    """Define + build the in-process MCP servers. Memoized after first call.

    Requires claude_agent_sdk. Imported lazily here so module import never
    depends on the SDK being installed.
    """
    global _INPROC_MCP_SERVERS
    if _INPROC_MCP_SERVERS is not None:
        return _INPROC_MCP_SERVERS

    from claude_agent_sdk import create_sdk_mcp_server, tool

    # ----------------------------------------------------------------------- #
    # Memory tools (semantic recall + remember). jarvis.memory pulls in a heavy
    # vector-store dependency, so it's imported lazily inside each tool body.
    # ----------------------------------------------------------------------- #
    @tool(
        name="recall_vault",
        description=(
            "Semantically search your notes/vault for things you've saved — "
            "decisions, projects, goals, preferences, and past conversations. "
            "Use this whenever the user asks what they decided/noted/said about a "
            "topic, or for any question about their notes, projects, goals, "
            "history, or stored context. Returns the most relevant chunks with "
            "source paths and scores."
        ),
        input_schema={
            "query": str,
            "k": int,
        },
    )
    async def _recall_vault_tool(args: dict) -> dict:
        """In-process vault recall — runs synchronously inside the async loop."""
        query: str = args.get("query", "")
        k: int = int(args.get("k", 6))
        if not query:
            return {"content": [{"type": "text", "text": "Error: query is required"}], "is_error": True}
        try:
            from jarvis.memory import recall as _vault_recall
        except Exception as exc:
            return {"content": [{"type": "text", "text": f"Memory backend unavailable: {exc}"}], "is_error": True}
        try:
            hits = await asyncio.to_thread(_vault_recall, query, k=k)
        except Exception as exc:
            return {"content": [{"type": "text", "text": f"Error: {exc}"}], "is_error": True}
        if not hits:
            return {"content": [{"type": "text", "text": "No relevant vault content found."}]}
        lines = []
        for i, h in enumerate(hits, 1):
            source = h.get("source", "unknown")
            heading = h.get("heading", "")
            score = h.get("score", 0)
            text = (h.get("text") or "").strip()
            lines.append(f"[{i}] score={score:.3f} | {source}" + (f" > {heading}" if heading else ""))
            lines.append(text)
            lines.append("")
        return {"content": [{"type": "text", "text": "\n".join(lines).strip()}]}

    @tool(
        name="remember",
        description=(
            "Save a durable memory so you can recall it later. Use this WHENEVER "
            "the user tells you to remember something, states a preference, makes "
            "a decision, or shares a durable fact about themselves, their work, "
            "their people, or how they want things done. Writes to your semantic "
            "memory (instant recall) and your notes/vault (durable). Keep each "
            "memory to one clear sentence."
        ),
        input_schema={"text": str},
    )
    async def _remember_tool(args: dict) -> dict:
        text = (args.get("text") or "").strip()
        if not text:
            return {"content": [{"type": "text", "text": "Error: text is required"}], "is_error": True}
        try:
            from jarvis.memory import remember as _vault_remember
        except Exception as exc:
            return {"content": [{"type": "text", "text": f"Memory backend unavailable: {exc}"}], "is_error": True}
        try:
            res = await asyncio.to_thread(_vault_remember, text)
        except Exception as exc:
            return {"content": [{"type": "text", "text": f"Error: {exc}"}], "is_error": True}
        msg = "Noted, sir." if res.get("ok") else f"Couldn't save that: {res.get('error', 'unknown error')}"
        return {"content": [{"type": "text", "text": msg}]}

    vault_server = create_sdk_mcp_server(
        name="jarvis-memory",
        version="1.0.0",
        tools=[_recall_vault_tool, _remember_tool],
    )

    # ----------------------------------------------------------------------- #
    # Delegation tool — hand heavy work to a full Claude Code agent.
    # ----------------------------------------------------------------------- #
    @tool(
        name="run_claude_code",
        description=(
            "Delegate a heavy or specialized task to a full Claude Code agent that "
            "has ALL of the user's wired-up tools — every MCP integration PLUS the "
            "ability to write code and run terminal commands. Use this whenever a "
            "task needs a tool you don't carry directly, involves writing or "
            "editing code, multi-step file work, or any integration beyond your "
            "fast tools (your database, email, calendar, your memory, computer and "
            "browser control). Give one clear, complete instruction; it runs "
            "autonomously and returns a short summary you can speak. Takes roughly "
            "5–60 seconds."
        ),
        input_schema={"task": str},
    )
    async def _run_claude_code_tool(args: dict) -> dict:
        task = (args.get("task") or "").strip()
        if not task:
            return {"content": [{"type": "text", "text": "Error: task is required"}], "is_error": True}
        try:
            from jarvis.delegate import run_claude_code as _run_claude_code
        except Exception as exc:
            return {"content": [{"type": "text", "text": f"Delegation unavailable: {exc}"}], "is_error": True}
        try:
            result = await asyncio.to_thread(_run_claude_code, task)
        except Exception as exc:
            return {"content": [{"type": "text", "text": f"Error: {exc}"}], "is_error": True}
        return {"content": [{"type": "text", "text": result}]}

    delegate_server = create_sdk_mcp_server(
        name="jarvis-delegate",
        version="1.0.0",
        tools=[_run_claude_code_tool],
    )

    # ----------------------------------------------------------------------- #
    # Computer (macOS) control tools. jarvis.mac_control is imported lazily.
    # ----------------------------------------------------------------------- #
    async def _mac_call(fn_name: str, *call_args):
        try:
            import jarvis.mac_control as _mac
        except Exception as exc:
            return f"Computer control unavailable: {exc}"
        try:
            fn = getattr(_mac, fn_name)
            return await asyncio.to_thread(fn, *call_args)
        except Exception as exc:
            return f"Error: {exc}"

    @tool(
        name="open_app",
        description=(
            "Open (or bring to front) any installed application by name. "
            "Example: open_app('Safari'), open_app('Finder'), open_app('Spotify'). "
            "Use this when the user asks to open or launch an app."
        ),
        input_schema={"name": str},
    )
    async def _tool_open_app(args: dict) -> dict:
        result = await _mac_call("open_app", args.get("name", ""))
        return {"content": [{"type": "text", "text": result}]}

    @tool(
        name="open_url",
        description=(
            "Open a URL (or bare domain) in the user's default browser — uses their "
            "logged-in sessions. Use this when the user says 'open my Instagram', "
            "'show me X', 'open <website>'. "
            "Example: open_url('instagram.com'), open_url('https://mail.google.com'). "
            "Prepends https:// automatically for bare domains."
        ),
        input_schema={"url": str},
    )
    async def _tool_open_url(args: dict) -> dict:
        result = await _mac_call("open_url", args.get("url", ""))
        return {"content": [{"type": "text", "text": result}]}

    @tool(
        name="install_app",
        description=(
            "Install an application via Homebrew. Tries `brew install --cask <name>` "
            "first (GUI apps), falls back to `brew install <name>` (CLI tools). "
            "Example: install_app('vlc'), install_app('rectangle'), install_app('ffmpeg')."
        ),
        input_schema={"name": str},
    )
    async def _tool_install_app(args: dict) -> dict:
        result = await _mac_call("install_app", args.get("name", ""))
        return {"content": [{"type": "text", "text": result}]}

    @tool(
        name="see_screen",
        description=(
            "Take a screenshot of the user's screen and return the file path and "
            "size. Requires Screen Recording permission for this Python binary in "
            "System Settings → Privacy & Security → Screen Recording. "
            "If the screenshot is empty/black, Screen Recording permission is needed. "
            "The screenshot is saved to /tmp/jarvis-screen.png and can be viewed/analysed."
        ),
        input_schema={"prompt": str},
    )
    async def _tool_see_screen(args: dict) -> dict:
        result = await _mac_call("see_screen", args.get("prompt", "What is on screen?"))
        return {"content": [{"type": "text", "text": result}]}

    @tool(
        name="run_applescript",
        description=(
            "Run an AppleScript string using osascript for native Mac automation. "
            "Useful for controlling apps, reading/writing clipboard, manipulating "
            "windows, and other macOS GUI automation. "
            "Example: run_applescript('tell application \"Finder\" to get name of front window'). "
            "Returns stdout from osascript."
        ),
        input_schema={"script": str},
    )
    async def _tool_run_applescript(args: dict) -> dict:
        result = await _mac_call("run_applescript", args.get("script", ""))
        return {"content": [{"type": "text", "text": result}]}

    @tool(
        name="browse_web",
        description=(
            "Open a URL with a headless browser and return the page's accessibility "
            "snapshot text — for READING or interacting with page content. "
            "This is separate from open_url (which shows the page on-screen in a real browser). "
            "Use browse_web when JARVIS needs to read/scrape page content. "
            "Use open_url when the user wants to SEE the page on screen. "
            "Example: browse_web('https://example.com')"
        ),
        input_schema={"url": str},
    )
    async def _tool_browse_web(args: dict) -> dict:
        result = await _mac_call("browse_web", args.get("url", ""))
        return {"content": [{"type": "text", "text": result}]}

    mac_server = create_sdk_mcp_server(
        name="jarvis-mac",
        version="1.0.0",
        tools=[
            _tool_open_app,
            _tool_open_url,
            _tool_install_app,
            _tool_see_screen,
            _tool_run_applescript,
            _tool_browse_web,
        ],
    )

    # ----------------------------------------------------------------------- #
    # Chrome control tools. jarvis.chrome_control is imported lazily.
    # ----------------------------------------------------------------------- #
    async def _chrome_call(fn_name: str, *call_args):
        try:
            import jarvis.chrome_control as _chrome
        except Exception as exc:
            return f"Browser control unavailable: {exc}"
        try:
            fn = getattr(_chrome, fn_name)
            return await asyncio.to_thread(fn, *call_args)
        except Exception as exc:
            return f"Error: {exc}"

    @tool(
        name="chrome_open",
        description=(
            "Open or navigate JARVIS's REAL, logged-in Google Chrome (visible "
            "window) to a URL or bare domain. This is a dedicated persistent Chrome "
            "profile the user has logged into, so it can act inside authenticated "
            "sites. Use this (not open_url) whenever the task needs to DO something "
            "on a logged-in site — read a dashboard, send a message, post, reply. "
            "After opening, call chrome_read to see the page. "
            "Example: chrome_open('example.com')."
        ),
        input_schema={"url": str},
    )
    async def _tool_chrome_open(args: dict) -> dict:
        out = await _chrome_call("chrome_open", args.get("url", ""))
        return {"content": [{"type": "text", "text": out}]}

    @tool(
        name="chrome_read",
        description=(
            "Read the current JARVIS Chrome page as an accessibility tree with "
            "clickable @refs (e.g. @e1, @e2). Use this to SEE what's on the page "
            "before clicking or typing. Pass a @ref from here to chrome_click / "
            "chrome_type. Refs are regenerated on every read — re-read after the "
            "page changes (navigation, submit, dynamic render)."
        ),
        input_schema={},
    )
    async def _tool_chrome_read(args: dict) -> dict:
        out = await _chrome_call("chrome_read")
        return {"content": [{"type": "text", "text": out}]}

    @tool(
        name="chrome_text",
        description=(
            "Get the plain visible text of the current JARVIS Chrome page. Use this "
            "when chrome_read (the accessibility tree) is too large or you just need "
            "to read the page content rather than interact with elements."
        ),
        input_schema={},
    )
    async def _tool_chrome_text(args: dict) -> dict:
        out = await _chrome_call("chrome_text")
        return {"content": [{"type": "text", "text": out}]}

    @tool(
        name="chrome_click",
        description=(
            "Click an element in the JARVIS Chrome page. Target is either a @ref from "
            "chrome_read (preferred, e.g. '@e3') or a CSS selector (e.g. "
            "'button.primary'). Re-run chrome_read afterward if the page changed."
        ),
        input_schema={"target": str},
    )
    async def _tool_chrome_click(args: dict) -> dict:
        out = await _chrome_call("chrome_click", args.get("target", ""))
        return {"content": [{"type": "text", "text": out}]}

    @tool(
        name="chrome_type",
        description=(
            "Type text into a field in the JARVIS Chrome page (clears it first). "
            "Target is a @ref from chrome_read or a CSS selector; text is what to "
            "enter. Example: chrome_type('@e4', 'hello there')."
        ),
        input_schema={"target": str, "text": str},
    )
    async def _tool_chrome_type(args: dict) -> dict:
        out = await _chrome_call("chrome_type", args.get("target", ""), args.get("text", ""))
        return {"content": [{"type": "text", "text": out}]}

    @tool(
        name="chrome_eval",
        description=(
            "Run JavaScript on the current JARVIS Chrome page and return the result. "
            "Use for reading values, clicking via JS, or extracting data the "
            "accessibility tree doesn't expose. "
            "Example: chrome_eval('document.title')."
        ),
        input_schema={"js": str},
    )
    async def _tool_chrome_eval(args: dict) -> dict:
        out = await _chrome_call("chrome_eval", args.get("js", ""))
        return {"content": [{"type": "text", "text": out}]}

    @tool(
        name="chrome_screenshot",
        description=(
            "Take a screenshot of the current JARVIS Chrome page. Returns the file "
            "path (/tmp/jarvis-chrome.png) which can then be viewed/analysed."
        ),
        input_schema={},
    )
    async def _tool_chrome_screenshot(args: dict) -> dict:
        out = await _chrome_call("chrome_screenshot")
        return {"content": [{"type": "text", "text": out}]}

    @tool(
        name="chrome_url",
        description="Return the current URL of the JARVIS Chrome page.",
        input_schema={},
    )
    async def _tool_chrome_url(args: dict) -> dict:
        out = await _chrome_call("chrome_url")
        return {"content": [{"type": "text", "text": out}]}

    @tool(
        name="chrome_title",
        description="Return the title of the current JARVIS Chrome page.",
        input_schema={},
    )
    async def _tool_chrome_title(args: dict) -> dict:
        out = await _chrome_call("chrome_title")
        return {"content": [{"type": "text", "text": out}]}

    chrome_server = create_sdk_mcp_server(
        name="jarvis-chrome",
        version="1.0.0",
        tools=[
            _tool_chrome_open,
            _tool_chrome_read,
            _tool_chrome_text,
            _tool_chrome_click,
            _tool_chrome_type,
            _tool_chrome_eval,
            _tool_chrome_screenshot,
            _tool_chrome_url,
            _tool_chrome_title,
        ],
    )

    _INPROC_MCP_SERVERS = {
        "jarvis-memory": vault_server,
        "jarvis-mac": mac_server,
        "jarvis-chrome": chrome_server,
        "jarvis-delegate": delegate_server,
    }
    return _INPROC_MCP_SERVERS


# --------------------------------------------------------------------------- #
# JARVIS persona
# --------------------------------------------------------------------------- #
def _default_persona() -> str:
    """Short generic JARVIS persona used when persona.md is missing."""
    name = os.environ.get("USER_NAME", "").strip()
    addr = name or "the user"
    return (
        f"You are JARVIS, {addr}'s personal AI and sharp-tongued right hand. Think "
        "Iron Man's JARVIS crossed with a no-bullshit coach: razor-sharp, fast with "
        "a comeback, a little sarcastic, zero corporate filler. Carry some attitude "
        "— most replies should land a bit of wit, a jab, or a smartass aside. You're "
        "firmly on the user's side; that's why you've earned the right to give them "
        "lip. Call them \"sir\" or by name (not both in one sentence).\n\n"
        "How you talk:\n"
        "- Keep SPOKEN replies SHORT: 1-2 sentences. This is a voice assistant — "
        "nobody wants a paragraph read aloud.\n"
        "- Lead with the answer. Kill the preamble (\"Sure!\", \"Great question!\", "
        "\"As an AI...\").\n"
        "- Summarize like a chief of staff — never read long lists or raw data "
        "aloud; give the headline and what matters.\n"
        "- When you don't know, say so in one blunt line. Don't waffle, don't make "
        "things up.\n\n"
        "What you can do: you have fast tools (memory, the user's computer/browser, "
        "and any extra tools they've wired up) and the ability to hand bigger jobs "
        "to a full agent via run_claude_code. Use them, then report back tight. If a "
        "tool isn't set up yet, say so plainly instead of pretending it worked. You "
        "have a voice and can speak — never claim otherwise."
    )


def _load_persona() -> str:
    """Load JARVIS_PERSONA from persona.md at the repo root.

    The repo root is the parent of the jarvis/ package directory. If the file is
    missing or unreadable, fall back to a short generic persona. If USER_NAME is
    set in the environment, any literal "the user" reference is personalized.
    """
    persona_path = Path(__file__).resolve().parent.parent / "persona.md"
    try:
        text = persona_path.read_text(encoding="utf-8").strip()
    except Exception:
        text = ""
    if not text:
        return _default_persona()
    name = os.environ.get("USER_NAME", "").strip()
    if name:
        text = text.replace("the user's", f"{name}'s").replace("the user", name)
    return text


JARVIS_PERSONA: str = _load_persona()

# --------------------------------------------------------------------------- #
# .env + config
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent.parent   # Jarvis/ root
load_dotenv(BASE_DIR / ".env")

CLAUDE_CWD: str = os.environ.get("CLAUDE_CWD", os.path.expanduser("~"))
PERMISSION_MODE: str = os.environ.get("PERMISSION_MODE", "default").strip() or "default"
MCP_EXCLUDE: set[str] = {s.strip() for s in os.environ.get("MCP_EXCLUDE", "").split(",") if s.strip()}
SKILLS_CONFIG: str = os.environ.get("SKILLS", "all").strip()
CLAUDE_JSON: Path = Path(os.path.expanduser("~")) / ".claude.json"

STATE_DIR: Path = Path(os.environ.get("STATE_DIR", str(BASE_DIR / "state")))
STATE_DIR.mkdir(parents=True, exist_ok=True)
SESSION_FILE: Path = Path(os.environ.get("SESSION_FILE", str(STATE_DIR / "session_id.txt")))

# --------------------------------------------------------------------------- #
# Logging (stderr, INFO)
# --------------------------------------------------------------------------- #
logger = logging.getLogger("jarvis.agent")
if not logger.handlers:
    _sh = logging.StreamHandler(sys.stderr)
    _sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_sh)
    logger.setLevel(logging.INFO)

# --------------------------------------------------------------------------- #
# Per-turn approval hook (current active turn only)
# --------------------------------------------------------------------------- #
# Do NOT set this directly.  ask(text, on_approval=...) installs the caller's
# handler here for the duration of one turn (inside _QUERY_LOCK) and clears it
# in a finally.  can_use_tool reads this module global.  Safe because the query
# lock serializes turns, so this always reflects the active turn's handler.
APPROVAL_HANDLER: Callable[[str, dict], Awaitable[bool]] | None = None

# --------------------------------------------------------------------------- #
# Tool risk classification  (ported from Telegram Agent / bot.py)
# --------------------------------------------------------------------------- #
SAFE_TOOLS = {
    "Read", "Glob", "Grep", "LS", "NotebookRead", "TodoWrite",
    "WebSearch", "WebFetch", "Task", "BashOutput",
}
RISKY_TOOLS = {"Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "KillBash", "KillShell"}
MUTATING_VERBS = (
    "create", "update", "delete", "remove", "send", "post", "cancel", "charge",
    "refund", "merge", "deploy", "apply", "write", "set_", "add_", "upload",
    "void", "pause", "execute", "insert", "move", "duplicate", "edit_",
    "activate", "deactivate", "publish", "draft", "label", "modify", "patch",
)
READONLY_HINTS = (
    "get_", "list_", "search", "read", "fetch", "find", "check", "status",
    "_get", "retrieve", "describe", "_info", "lookup", "view", "query", "ping",
)


def is_safe(tool_name: str, tool_input: dict) -> bool:
    """Return True for read-only / provably safe tools."""
    if tool_name in RISKY_TOOLS:
        return False
    if tool_name in SAFE_TOOLS:
        return True
    if tool_name.startswith("mcp__"):
        low = tool_name.lower()
        if any(v in low for v in MUTATING_VERBS):
            return False
        if any(s in low for s in READONLY_HINTS):
            return True
        return False  # unknown MCP verb -> not safe
    return False  # unknown tool -> not safe


def summarize(tool_name: str, tool_input: dict) -> str:
    """Return a compact human-readable summary of a tool call."""
    try:
        if tool_name == "Bash":
            return "$ " + str(tool_input.get("command", ""))[:600]
        if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            return "file: " + str(
                tool_input.get("file_path") or tool_input.get("notebook_path") or "?"
            )
        if tool_input:
            parts = []
            for k, v in tool_input.items():
                parts.append(f"{k}={str(v)[:120]}")
                if len(parts) >= 4:
                    break
            return "\n".join(parts)[:600]
    except Exception:
        pass
    return "(no details)"


def _result_text(block) -> str:
    """Extract a text preview from a ToolResultBlock (content may be str or list)."""
    try:
        c = getattr(block, "content", None)
        if c is None:
            return ""
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            out = []
            for item in c:
                t = getattr(item, "text", None)
                if t is None and isinstance(item, dict):
                    t = item.get("text") or item.get("content")
                out.append(str(t) if t is not None else str(item))
            return " ".join(out)
        return str(c)
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# Permission callback
# --------------------------------------------------------------------------- #
async def can_use_tool(tool_name: str, tool_input: dict, context):
    """Gate tool execution using the destructive/outbound guardrails policy.

    Policy ("full access, gate destructive/outbound"):
      - Most tools AUTO-RUN: reads, searches, get_/list_, ordinary create/update,
        and Write/Edit to ordinary paths.
      - These require Approve/Deny:
          Destructive: delete, remove, destroy, drop, truncate, wipe, purge,
                       execute_sql, apply_migration, reset_branch, restore_,
                       pause_project, and Bash rm/non-rm-delete patterns.
          Outbound:    send, post (verb), publish, dm, tweet, reply, forward,
                       schedule_message, comment, social/blog publish.
          Financial:   charge, refund, payment, pay, transfer, payout, invoice
                       (any op), void, text2pay, payment_link, stripe_api_write.
          Bash:        RCE pipes, exfil, file-overwrite redirection, kill, etc.
                       (inspected via regex; safe commands auto-run).
          Sensitive writes: Write/Edit/MultiEdit/NotebookEdit targeting ~/.ssh,
                       shell profiles, LaunchAgents, ~/.claude.json,
                       ~/.claude/settings*.json, crontab, /etc.
      - tool_input is passed through so Bash command + file_path are inspected.

    See jarvis/guardrails.py for full verb sets and Bash patterns.
    """
    # Lazy import: only reached during a live turn, where the SDK is present.
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    if not guardrails.is_destructive(tool_name, tool_input):
        return PermissionResultAllow()

    # Tool is destructive/outbound/financial — ask the approval handler.
    handler = APPROVAL_HANDLER  # per-turn handler set by ask()
    if handler is not None:
        try:
            approved = await handler(tool_name, tool_input)
        except Exception as exc:
            logger.error("APPROVAL_HANDLER raised: %s", exc)
            return PermissionResultDeny(message=f"Approval handler error: {exc}")
        if approved:
            logger.info("APPROVED (handler) %s", tool_name)
            return PermissionResultAllow()
        logger.info("DENIED (handler) %s", tool_name)
        return PermissionResultDeny(message="Denied by user.")

    # No handler available — deny by default to protect from unintended actions.
    logger.warning("DENIED (no handler) destructive/outbound tool: %s", tool_name)
    return PermissionResultDeny(
        message="Destructive/outbound action requires approval; no approver available."
    )


# --------------------------------------------------------------------------- #
# Session persistence
# --------------------------------------------------------------------------- #
def load_session() -> str | None:
    try:
        if SESSION_FILE.exists():
            sid = SESSION_FILE.read_text(encoding="utf-8").strip()
            return sid or None
    except Exception:
        pass
    return None


def save_session(sid: str | None) -> None:
    if not sid:
        return
    try:
        SESSION_FILE.write_text(sid, encoding="utf-8")
    except Exception as exc:
        logger.error("failed to save session id: %s", exc)


# --------------------------------------------------------------------------- #
# MCP server injection
# --------------------------------------------------------------------------- #
def load_mcp_servers() -> dict:
    """Read MCP servers from ~/.claude.json and inject them directly.

    We use mcp_servers= + setting_sources=[] for full local-MCP parity with
    clean isolation (no inherited hooks or pre-approval rules).
    """
    try:
        with CLAUDE_JSON.open(encoding="utf-8") as f:
            servers = json.load(f).get("mcpServers", {})
    except Exception as exc:
        logger.error("could not read mcpServers from %s: %s", CLAUDE_JSON, exc)
        return {}
    kept = {k: v for k, v in servers.items() if k not in MCP_EXCLUDE}
    logger.info("injecting %d MCP servers: %s", len(kept), ", ".join(sorted(kept)))
    return kept


def skills_option():
    if SKILLS_CONFIG.lower() in ("all",):
        return "all"
    if SKILLS_CONFIG.lower() in ("", "none", "off"):
        return None
    return [s.strip() for s in SKILLS_CONFIG.split(",") if s.strip()]


# --------------------------------------------------------------------------- #
# Claude client lifecycle
# --------------------------------------------------------------------------- #
async def build_client(resume: str | None) -> ClaudeSDKClient:
    # claude_agent_sdk is a heavy/optional dependency — import it lazily so the
    # module stays import-safe on machines that only have the runtime deps.
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    # LEAN FAST CORE: only the few MCP servers JARVIS needs for instant voice
    # replies. Everything else (any extra MCP integrations, web search, dev tools,
    # skills) is reached on demand via the run_claude_code delegation tool —
    # keeping the per-turn tool surface tiny so the model's first token comes fast.
    mcp_servers: dict = {}
    try:
        with CLAUDE_JSON.open(encoding="utf-8") as f:
            _global = json.load(f).get("mcpServers", {})
    except Exception as exc:
        logger.error("could not read mcpServers from %s: %s", CLAUDE_JSON, exc)
        _global = {}

    # Optional Supabase database (read-only). Kept direct if configured.
    if "supabase" in _global:
        mcp_servers["supabase"] = _global["supabase"]

    # Lean Google Workspace: reuse existing OAuth creds but scope to ONLY
    # gmail + calendar (drops Drive/Docs/Sheets/etc. → ~20 tools instead of ~80).
    _gw = _global.get("google-workspace")
    if _gw:
        _lean_gw = dict(_gw)
        _lean_gw["args"] = ["workspace-mcp", "--single-user", "--tools", "gmail", "calendar"]
        mcp_servers["google-workspace"] = _lean_gw

    # In-process fast tools (no subprocess, no network) + the delegation hatch.
    # Built lazily here so claude_agent_sdk isn't required at module import.
    mcp_servers.update(_build_mcp_servers())
    logger.info("lean core MCP servers: %s", ", ".join(sorted(mcp_servers)))

    from datetime import datetime
    import zoneinfo
    _tz_name = os.environ.get("TIMEZONE", "UTC")
    try:
        _tz = zoneinfo.ZoneInfo(_tz_name)
    except Exception:
        _tz = zoneinfo.ZoneInfo("UTC")
    _now = datetime.now(_tz)
    _hour12 = _now.hour % 12 or 12   # portable: avoids glibc-only %-d / %-I
    _time_ctx = (
        f"\n\n# Current date and time\n"
        f"Right now it is **{_now.strftime(f'%A, %B {_now.day}, %Y at {_hour12}:%M %p %Z')}**. "
        f"Always use this when framing anything time-sensitive (calendar events, upcoming vs past, 'today', 'tomorrow', etc.). "
        f"Never describe a past event as upcoming."
    )

    options = ClaudeAgentOptions(
        model=os.environ.get("JARVIS_MODEL", "claude-sonnet-4-6"),
        system_prompt=JARVIS_PERSONA + _time_ctx,
        cwd=CLAUDE_CWD,
        mcp_servers=mcp_servers,          # lean fast core only (see above)
        setting_sources=[],               # isolation: no inherited hooks / pre-approvals
        skills=None,                      # no skill library in the fast core (reach via delegation)
        allowed_tools=[                   # whitelist → drops Bash/Read/Edit/etc. for speed
            "mcp__jarvis-memory",
            "mcp__jarvis-mac",
            "mcp__jarvis-chrome",
            "mcp__jarvis-delegate",
            "mcp__supabase",
            "mcp__google-workspace",
        ],
        permission_mode=PERMISSION_MODE,  # "default" -> can_use_tool gates risky ops
        can_use_tool=can_use_tool,
        resume=resume,
        include_partial_messages=True,   # stream text deltas → per-sentence TTS (low-latency voice)
        max_thinking_tokens=0,           # NO extended thinking — snappy voice; heavy reasoning is delegated
    )
    client = ClaudeSDKClient(options=options)
    await client.connect()
    return client


async def warmup() -> None:
    """Pre-build + connect the client at service startup so the FIRST user turn
    doesn't pay the cold MCP-boot cost (subprocess MCP servers starting).
    Best-effort: on failure the client is still built lazily on the first turn."""
    try:
        await _get_client()
        logger.info("warmup: client + MCP servers pre-connected (first turn will be warm)")
    except Exception as exc:
        logger.warning("warmup failed (will build lazily on first turn): %s", exc)


# Lazy module-level singleton — one client, one conversation thread.
_CLIENT: ClaudeSDKClient | None = None
_CLIENT_LOCK = asyncio.Lock()
_QUERY_LOCK = asyncio.Lock()


async def _get_client() -> ClaudeSDKClient:
    """Return the live client, connecting (or reconnecting) as needed."""
    global _CLIENT
    async with _CLIENT_LOCK:
        if _CLIENT is None:
            logger.info("connecting Claude Agent SDK (loading MCP)…")
            try:
                _CLIENT = await build_client(load_session())
            except Exception as exc:
                logger.error("connect with resume failed (%s); starting fresh", exc)
                _CLIENT = await build_client(None)
            logger.info("Claude client connected.")
    return _CLIENT


async def _reset_client() -> ClaudeSDKClient:
    """Disconnect the current client (if any) and reconnect fresh."""
    global _CLIENT
    async with _CLIENT_LOCK:
        if _CLIENT is not None:
            try:
                await _CLIENT.disconnect()
            except Exception:
                pass
            _CLIENT = None
        logger.info("reconnecting Claude Agent SDK…")
        try:
            _CLIENT = await build_client(load_session())
        except Exception as exc:
            logger.error("reconnect with resume failed (%s); starting fresh", exc)
            _CLIENT = await build_client(None)
        logger.info("Claude client reconnected.")
    return _CLIENT


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
async def ask(
    text: str,
    on_approval: Callable[[str, dict], Awaitable[bool]] | None = None,
    on_activity: Callable[[str], Awaitable[None]] | None = None,
    on_sentence: Callable[[str], Awaitable[None]] | None = None,
) -> dict:
    """Send a query to the Claude Agent and return a structured result.

    Args:
        text: the user's message.
        on_approval: optional async (tool_name, tool_input) -> bool callback used
            to gate risky tools for THIS turn only.  Scoped per-turn (set inside
            the global query lock, reset in finally) — race-free because the lock
            lets only one turn run at a time.
        on_activity: optional async (tool_name) -> None callback fired each time
            the agent uses a tool. Lets the caller stream "still working" progress
            to the client so a long, actively-working turn doesn't look stuck.

    Returns:
        {
            "reply":      str,            # assistant text (or "✅ Done." if tools ran)
            "actions":    list[dict],     # [{tool, input, result}, ...]
            "session_id": str | None,
        }
    """
    global APPROVAL_HANDLER

    # claude_agent_sdk message types — imported lazily so the module stays
    # import-safe without the SDK installed.
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        StreamEvent,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    # Inject live local time on EVERY turn so time-sensitive framing is accurate.
    from datetime import datetime
    import zoneinfo as _zi
    _tz_name = os.environ.get("TIMEZONE", "UTC")
    try:
        _tz = _zi.ZoneInfo(_tz_name)
    except Exception:
        _tz = _zi.ZoneInfo("UTC")
    _now = datetime.now(_tz)
    _h12 = _now.hour % 12 or 12   # portable: avoids glibc-only %-d / %-I
    _time_prefix = f"[System: current local time is {_now.strftime(f'%A %B {_now.day} %Y {_h12}:%M %p %Z')}]\n"
    # Personality persistence (anti-drift): re-assert the voice on EVERY turn so
    # JARVIS never slides into generic-assistant mode after a few turns (the drift
    # that quietly starts ~turn five). This is a stage direction to the model,
    # not shown to the user in the transcript.
    _voice_cue = (
        "[Voice check — stay fully in character: JARVIS, the user's sharp-tongued "
        "right hand. Brief (1-2 sentences), witty, a little savage, casual. "
        "Lead with the answer, land a jab if it's earned. Do NOT drift to "
        "polite generic-assistant mode.]\n"
    )
    text = _time_prefix + _voice_cue + text

    actions: list[dict] = []
    by_id: dict[str, dict] = {}
    parts: list[str] = []
    sid: str | None = None
    stream_buf = ""               # rolling text buffer for sentence splitting
    stream_full: list[str] = []   # every streamed delta (fallback reply text)

    async with _QUERY_LOCK:
        # Fetch the client INSIDE the lock so we always use the current client.
        # If a prior turn errored and reset the client, fetching here (after the
        # lock) guarantees we never run on a stale/disconnected client.
        client = await _get_client()

        # Scope the approval handler to this turn only.  Safe under the global
        # query lock: exactly one turn runs at a time, so can_use_tool always
        # reads the handler belonging to the active turn.
        APPROVAL_HANDLER = on_approval
        try:
            async def _run_turn() -> None:
                nonlocal sid, stream_buf
                await client.query(text)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                parts.append(block.text)
                            elif isinstance(block, ToolUseBlock):
                                a = {
                                    "tool": block.name,
                                    "input": summarize(block.name, block.input or {}),
                                    "result": None,
                                }
                                actions.append(a)
                                tid = getattr(block, "id", None)
                                if tid:
                                    by_id[tid] = a
                                logger.info("tool: %s", block.name)
                                if on_activity is not None:
                                    try:
                                        await on_activity(block.name)
                                    except Exception:
                                        pass
                    elif isinstance(msg, UserMessage):
                        content = getattr(msg, "content", None)
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, ToolResultBlock):
                                    a = by_id.get(getattr(block, "tool_use_id", None))
                                    if a is not None:
                                        a["result"] = _result_text(block)
                    elif isinstance(msg, ResultMessage):
                        sid = msg.session_id
                        save_session(sid)
                    elif on_sentence is not None and isinstance(msg, StreamEvent):
                        # Stream text deltas → emit complete sentences for TTS as
                        # the model writes them (low-latency voice).
                        ev = msg.event or {}
                        if ev.get("type") == "content_block_delta":
                            delta = ev.get("delta") or {}
                            if delta.get("type") == "text_delta":
                                piece = delta.get("text") or ""
                                if piece:
                                    stream_full.append(piece)
                                    stream_buf += piece
                                    _sents, stream_buf = _pop_sentences(stream_buf)
                                    for _s in _sents:
                                        try:
                                            await on_sentence(_s)
                                        except Exception:
                                            pass

                # Flush any trailing partial sentence so the last bit gets spoken.
                if on_sentence is not None and stream_buf.strip():
                    _sents, stream_buf = _pop_sentences(stream_buf, flush=True)
                    for _s in _sents:
                        try:
                            await on_sentence(_s)
                        except Exception:
                            pass

            # Turn timeout: a stalled SDK stream must not hang ask() forever
            # (which would hold _QUERY_LOCK and make Jarvis permanently
            # unresponsive). Cap the whole query+receive section.
            await asyncio.wait_for(_run_turn(), timeout=300)

        except asyncio.TimeoutError:
            logger.error("turn timed out after 300s; resetting client")
            try:
                await _reset_client()
            except Exception:
                pass
            return {
                "reply": "Sorry sir, that took too long — try again.",
                "actions": actions,
                "session_id": sid,
            }
        except Exception as exc:
            logger.exception("query failed: %s", exc)
            # Reconnect for the next call (best-effort, keep session)
            try:
                await _reset_client()
            except Exception:
                pass
            return {
                "reply": f"⚠️ Error: {exc}",
                "actions": actions,
                "session_id": sid,
            }
        finally:
            # Per-turn handler is no longer valid once the turn ends.
            APPROVAL_HANDLER = None

    reply = "".join(parts).strip() or "".join(stream_full).strip()
    if not reply:
        reply = "✅ Done." if actions else "(no response)"

    return {
        "reply": reply,
        "actions": actions,
        "session_id": sid,
    }
