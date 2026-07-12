"""
jarvis/delegate.py — hand heavy work to a headless Claude Code agent.

Jarvis keeps a lean, fast tool surface so it can REPLY quickly. Anything heavy —
writing code, or using one of the user's MCP integrations (web search, workspace
tools, etc.) — is delegated to a full Claude Code session via `claude -p`, which
inherits the entire toolset from ~/.claude.json. The agent runs autonomously and
returns its final text, which Jarvis speaks.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile

logger = logging.getLogger("jarvis.delegate")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [delegate] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

CLAUDE_BIN: str = os.environ.get("CLAUDE_BIN", "claude")
DELEGATE_MODEL: str = os.environ.get("DELEGATE_MODEL", "claude-sonnet-4-6")
DELEGATE_CWD: str = os.environ.get("DELEGATE_CWD", os.path.expanduser("~"))
DELEGATE_TIMEOUT: int = int(os.environ.get("DELEGATE_TIMEOUT", "300"))

_WRAP = (
    "You are a worker agent for JARVIS (the user's voice assistant). Do the "
    "task below using whatever tools you need. When finished, reply with a SHORT "
    "plain-text summary of what you did and the result — no markdown, no preamble — "
    "so JARVIS can speak it aloud.\n\nTASK:\n"
)


CLAUDE_JSON = os.path.expanduser("~/.claude.json")


def _scoped_mcp_config(names: list[str]) -> dict:
    """Build an mcp-config containing ONLY the named servers (pulled from ~/.claude.json)."""
    try:
        with open(CLAUDE_JSON, encoding="utf-8") as f:
            allsrv = json.load(f).get("mcpServers", {})
    except Exception:
        allsrv = {}
    return {"mcpServers": {n: allsrv[n] for n in names if n in allsrv}}


def run_claude_code(task: str, system_prompt: str | None = None,
                    mcp_servers: list[str] | None = None,
                    cwd: str | None = None, timeout: int | None = None) -> str:
    """Run a one-shot headless Claude Code agent on `task`; return its final text.

    `system_prompt` (optional) is appended to give the worker a specialist role.
    `mcp_servers` (optional): if a LIST, the worker loads ONLY those MCP servers
    (fast boot + scoped tools, via --strict-mcp-config). If None, it inherits the
    full ~/.claude.json toolset (used by Flux = everything)."""
    task = (task or "").strip()
    if not task:
        return "No task was provided."
    cmd = [
        CLAUDE_BIN,
        "-p", _WRAP + task,
        "--output-format", "text",
        "--permission-mode", "bypassPermissions",
        "--model", DELEGATE_MODEL,
    ]
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
    cfg_path = None
    if mcp_servers is not None:
        try:
            cfg = _scoped_mcp_config(mcp_servers)
            fd, cfg_path = tempfile.mkstemp(prefix="jarvis-mcp-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
            cmd += ["--mcp-config", cfg_path, "--strict-mcp-config"]
            logger.info("scoped MCP for worker: %s", ", ".join(cfg["mcpServers"]) or "(none)")
        except Exception as exc:
            logger.warning("could not scope MCP (%s); inheriting full toolset", exc)
            cfg_path = None
    logger.info("delegating to claude code: %r", task[:120])
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd or DELEGATE_CWD,
            capture_output=True,
            text=True,
            timeout=timeout or DELEGATE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        logger.error("delegate timed out after %ss", timeout or DELEGATE_TIMEOUT)
        return "That one took too long and timed out, sir."
    except Exception as exc:
        logger.error("delegate failed to launch: %s", exc)
        return f"Couldn't run that — delegation failed: {exc}"
    finally:
        if cfg_path:
            try:
                os.remove(cfg_path)
            except Exception:
                pass

    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and not out:
        err = (proc.stderr or "").strip()[:300]
        logger.error("delegate exit=%d err=%s", proc.returncode, err)
        return f"The delegated task errored: {err}" if err else "The delegated task failed."
    logger.info("delegate done (exit=%d, %d chars out)", proc.returncode, len(out))
    return out or "Done — but nothing came back to report."


def _kill_tree(proc) -> None:
    """SIGTERM the worker's whole process group (it was started with setsid)."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


async def run_claude_code_async(task: str, system_prompt: str | None = None,
                                mcp_servers: list[str] | None = None,
                                timeout: int | None = None,
                                on_pid=None) -> str:
    """Async, cancellable variant for BACKGROUND jobs. Spawns the worker in its own
    process group (setsid) so it (and any children) can be killed on timeout/cancel.
    `on_pid(pid)` is called with the worker PID for the orphan reaper."""
    task = (task or "").strip()
    if not task:
        return "No task was provided."
    cmd = [
        CLAUDE_BIN, "-p", _WRAP + task,
        "--output-format", "text",
        "--permission-mode", "bypassPermissions",
        "--model", DELEGATE_MODEL,
    ]
    if system_prompt:
        cmd += ["--append-system-prompt", system_prompt]
    cfg_path = None
    if mcp_servers is not None:
        try:
            cfg = _scoped_mcp_config(mcp_servers)
            fd, cfg_path = tempfile.mkstemp(prefix="jarvis-mcp-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
            cmd += ["--mcp-config", cfg_path, "--strict-mcp-config"]
        except Exception as exc:
            logger.warning("could not scope MCP (%s); inheriting full toolset", exc)
            cfg_path = None
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=DELEGATE_CWD,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,   # own process group → killable as a tree
        )
        if on_pid:
            try:
                on_pid(proc.pid)
            except Exception:
                pass
        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout or DELEGATE_TIMEOUT)
        except asyncio.TimeoutError:
            _kill_tree(proc)
            logger.error("async delegate timed out after %ss", timeout or DELEGATE_TIMEOUT)
            return "That background task took too long and timed out, sir."
        out = (out_b or b"").decode(errors="replace").strip()
        if proc.returncode != 0 and not out:
            err = (err_b or b"").decode(errors="replace").strip()[:300]
            return f"The background task errored: {err}" if err else "The background task failed."
        return out or "Done — but nothing came back to report."
    except asyncio.CancelledError:
        if proc:
            _kill_tree(proc)
        raise
    finally:
        if cfg_path:
            try:
                os.remove(cfg_path)
            except Exception:
                pass
