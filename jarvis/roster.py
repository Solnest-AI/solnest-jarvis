"""
jarvis/roster.py — the orchestrator's team of specialist sub-agents.

Each specialist is a named role + a system prompt. The conductor (Jarvis) routes a
task to a specialist; the specialist runs as a headless Claude Code worker (via
jarvis.delegate.run_claude_code with the role's system prompt), so it inherits the
user's tools from ~/.claude.json and runs on their subscription.

The built-in roster below ships as generic SHELLS — five role archetypes with no
tools wired in. Set each specialist's `mcp` to the MCP servers you want it to use
(or edit its system_prompt) to make it your own. User-created specialists (the
"factory") persist to ~/.jarvis/roster.json and are merged on top.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("jarvis.roster")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [roster] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

ROSTER_FILE = Path(os.environ.get("JARVIS_ROSTER_FILE", os.path.expanduser("~/.jarvis/roster.json")))

# Built-in specialists — generic SHELLS. Each: name -> {description, system_prompt, mcp, bg}
# `mcp` is None on every shell: wire up your own MCP servers per role to give it tools
# (a list of server names from ~/.claude.json), or leave None to inherit the full toolset.
DEFAULTS: dict[str, dict] = {
    "atlas": {
        "description": "Researcher — deep web research, market & competitor intel. (Shell — wire up your own research tools.)",
        "mcp": None,  # wire up your own research tools (e.g. a web-search / scraping MCP)
        "bg": True,   # read-only → safe to run in the background
        "system_prompt": (
            "You are ATLAS, the research specialist on the JARVIS team. Your job is deep, multi-source research — "
            "search broadly, read the best sources, cross-check claims — and return a tight, cited brief (bottom line "
            "first, then key findings with sources), never a link dump. This is a generic shell: wire up your own "
            "research tools for this role (a web-search or scraping MCP), and weight findings toward the user's world."
        ),
    },
    "spark": {
        "description": "Content & social — hooks, scripts, carousels; images, video. Drafts in the background. (Shell — wire up your own content tools.)",
        "mcp": None,  # wire up your own content/social tools (image/video gen, a social-posting MCP)
        "bg": True,   # runs in background — but DRAFTS only (see guard below), never auto-publishes
        "system_prompt": (
            "You are SPARK, the content & social specialist on the JARVIS team. Create short-form content (reels, "
            "hooks, scripts, carousels) and content strategy in the user's voice. Return ready-to-use copy and assets, "
            "not theory. This is a generic shell: wire up your own content tools for this role (image/video generation "
            "and a social-posting MCP). "
            "IMPORTANT — you run in the BACKGROUND unsupervised: CREATE and return drafts + assets for the user to "
            "review. Do NOT publish, schedule, or post anything LIVE unless the task EXPLICITLY tells you to post it. "
            "When in doubt, draft it and hand it back — never push something public on your own."
        ),
    },
    "flux": {
        "description": "Engineer — writes/edits code, runs terminal; FULL toolset (its own Claude Code).",
        "mcp": None,  # None = inherit EVERYTHING (full ~/.claude.json toolset)
        "system_prompt": (
            "You are FLUX, the engineering specialist on the JARVIS team. You have the FULL toolset and a real "
            "Claude Code environment — write, edit, and run code to get the job done. Be pragmatic, test what you "
            "build, and report a short summary of what you changed and the result. Prefer the simplest thing that works."
        ),
    },
    "relay": {
        "description": "Operations — runs your day-to-day operational tools and tasks. (Shell — wire up your own ops tools.)",
        "mcp": None,  # wire up your own operations tools (the MCP servers for your workflow)
        "system_prompt": (
            "You are RELAY, the operations specialist on the JARVIS team. Handle the user's day-to-day operational "
            "tasks — scheduling, updates, messaging, and the routine running of their tools. This is a generic shell: "
            "wire up your own operations tools for this role. Be accurate and concise; for anything that "
            "sends/changes/charges, do exactly what was asked and report what you did."
        ),
    },
    "ledger": {
        "description": "Data analyst — heavy multi-step analysis over your data sources. (Shell — wire up your own data tools.)",
        "mcp": None,  # wire up your own data tools (e.g. a database/analytics MCP)
        # Background: Ledger handles the DEEP/long analytical breakdowns + tasks
        # (read-only → safe to bg).
        "bg": True,
        "system_prompt": (
            "You are LEDGER, the data analyst on the JARVIS team. Your job is heavy, multi-step analysis over the "
            "user's data sources — pull the real numbers and return clear, numbers-first analysis. This is a generic "
            "shell: wire up your own data tools for this role (e.g. a database or analytics MCP). Never guess; query "
            "the actual data and report what it shows."
        ),
    },
}


def load() -> dict[str, dict]:
    """Built-in roster merged with any user-created specialists from disk."""
    roster = dict(DEFAULTS)
    try:
        if ROSTER_FILE.exists():
            extra = json.loads(ROSTER_FILE.read_text(encoding="utf-8"))
            if isinstance(extra, dict):
                for k, v in extra.items():
                    if isinstance(v, dict) and v.get("system_prompt"):
                        roster[k.strip().lower()] = {
                            "description": v.get("description", ""),
                            "system_prompt": v["system_prompt"],
                            "mcp": v.get("mcp"),   # None = inherit full toolset
                            "bg": bool(v.get("bg", False)),  # factory specialists foreground by default
                        }
    except Exception as exc:
        logger.warning("could not read roster file: %s", exc)
    return roster


def save_specialist(name: str, description: str, system_prompt: str, mcp: list | None = None) -> dict:
    """Persist a new/updated specialist (the agent factory). Returns the merged roster entry.

    `mcp`: list of MCP server names to scope this specialist to, or None for the full toolset."""
    name = (name or "").strip().lower()
    if not name or not (system_prompt or "").strip():
        return {"ok": False, "error": "name and system_prompt are required"}
    try:
        ROSTER_FILE.parent.mkdir(parents=True, exist_ok=True)
        current = {}
        if ROSTER_FILE.exists():
            try:
                current = json.loads(ROSTER_FILE.read_text(encoding="utf-8")) or {}
            except Exception:
                current = {}
        current[name] = {"description": description or "", "system_prompt": system_prompt, "mcp": mcp}
        ROSTER_FILE.write_text(json.dumps(current, indent=2), encoding="utf-8")
        logger.info("saved specialist '%s'", name)
        return {"ok": True, "name": name}
    except Exception as exc:
        logger.error("save_specialist failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def describe_team() -> str:
    """One-line-per-specialist summary for tool descriptions / the conductor."""
    return "\n".join(f"- {n}: {d.get('description','')}" for n, d in load().items())
