"""
jarvis/jobs.py — background job manager for the orchestrator's specialists.

Lets the conductor fire a read-only specialist (Atlas/Ledger) into the background
so the user can keep talking. Bakes in the review panel's safety rails:
  • concurrency cap (max 2 running) — rejects the 3rd loudly (no fork-bomb)
  • per-job timeout (120s) — handled in delegate.run_claude_code_async
  • epoch tagging — results from a superseded epoch (after restart) are dropped
  • orphan reaper — PIDs persisted; leftover worker process-groups killed on boot
  • kill switch — cancel_all() cancels tasks + SIGTERMs worker process groups

Delivery is decoupled: on completion a job is appended to an "undelivered" list
(woven into conversation history on the next turn) AND a registered async delivery
callback is invoked (server speaks it if idle, else chimes). Only read-only
specialists run here; mutating ones (relay/spark/flux) stay foreground.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from jarvis.delegate import run_claude_code_async

logger = logging.getLogger("jarvis.jobs")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [jobs] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

MAX_CONCURRENT = int(os.environ.get("JARVIS_MAX_JOBS", "2"))
JOB_TIMEOUT = int(os.environ.get("JARVIS_JOB_TIMEOUT", "120"))
PID_FILE = os.path.expanduser("~/.jarvis/worker-pids.txt")

_EPOCH = 0
_NEXT_ID = 0
_RUNNING: dict[int, asyncio.Task] = {}
_PIDS: dict[int, int] = {}
_UNDELIVERED: list[dict] = []
_DELIVER = None   # async callback(job_dict) set by the server


def set_delivery(cb) -> None:
    global _DELIVER
    _DELIVER = cb


def active_count() -> int:
    return len(_RUNNING)


def bump_epoch() -> None:
    """Invalidate in-flight jobs (call on startup and on a 'new conversation')."""
    global _EPOCH
    _EPOCH += 1


def _write_pids() -> None:
    try:
        os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
        with open(PID_FILE, "w") as f:
            f.write("\n".join(str(p) for p in _PIDS.values()))
    except Exception:
        pass


def reap_orphans() -> None:
    """Kill worker process-groups left over from a previous run (call on startup)."""
    try:
        if not os.path.exists(PID_FILE):
            return
        for line in open(PID_FILE):
            pid = line.strip()
            if not pid.isdigit():
                continue
            try:
                os.killpg(int(pid), signal.SIGTERM)
            except Exception:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except Exception:
                    pass
        open(PID_FILE, "w").close()
        logger.info("reaped orphan worker PIDs from previous run")
    except Exception as exc:
        logger.warning("reap_orphans failed: %s", exc)


async def submit(specialist: str, task: str, system_prompt: str,
                 mcp_servers: list | None) -> dict:
    """Launch a background specialist job. Returns {ok, id} or {ok:False, msg}."""
    global _NEXT_ID
    if active_count() >= MAX_CONCURRENT:
        return {"ok": False, "msg": f"I've already got {active_count()} specialists running — let those finish first."}
    _NEXT_ID += 1
    jid = _NEXT_ID
    epoch = _EPOCH

    async def _run():
        try:
            def _pid(p):
                _PIDS[jid] = p
                _write_pids()
            try:
                result = await run_claude_code_async(
                    task, system_prompt=system_prompt, mcp_servers=mcp_servers,
                    timeout=JOB_TIMEOUT, on_pid=_pid)
            except asyncio.CancelledError:
                logger.info("job #%d (%s) cancelled", jid, specialist)
                return
            except Exception as exc:
                result = f"(error: {exc})"
            job = {"id": jid, "specialist": specialist, "result": result,
                   "epoch": epoch, "spoken": False}
            if epoch != _EPOCH:
                logger.info("job #%d (%s) is from a stale epoch — dropping result", jid, specialist)
                return
            _UNDELIVERED.append(job)
            logger.info("job #%d (%s) done (%d chars)", jid, specialist, len(result))
            if _DELIVER:
                try:
                    await _DELIVER(job)
                except Exception as exc:
                    logger.warning("delivery callback failed: %s", exc)
        finally:
            _RUNNING.pop(jid, None)
            _PIDS.pop(jid, None)
            _write_pids()

    _RUNNING[jid] = asyncio.create_task(_run())
    logger.info("job #%d submitted -> %s", jid, specialist)
    return {"ok": True, "id": jid}


def pending_results() -> list[dict]:
    """Drain completed-but-not-yet-woven results (for next-turn history injection)."""
    global _UNDELIVERED
    out = _UNDELIVERED[:]
    _UNDELIVERED = []
    return out


def status() -> str:
    n = active_count()
    return f"{n} background job(s) running" if n else "no background jobs running"


async def cancel_all() -> int:
    """Kill switch — cancel all running jobs + SIGTERM their worker process groups."""
    n = len(_RUNNING)
    for t in list(_RUNNING.values()):
        t.cancel()
    for pid in list(_PIDS.values()):
        try:
            os.killpg(pid, signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
    _RUNNING.clear()
    _PIDS.clear()
    _write_pids()
    logger.info("cancel_all: cancelled %d job(s)", n)
    return n
