"""
jarvis/memory.py — folder-only markdown long-term memory. No database, no
embeddings, no servers. Works with zero infrastructure.

Point VAULT_DIR (in your .env) at any notes folder — an Obsidian vault works
great — and JARVIS can both SAVE and RECALL over plain `.md` files in it.

Memory is OPTIONAL. If VAULT_DIR isn't set (or the folder is missing), every
public function degrades gracefully: recall returns [], remember reports that
memory isn't configured. Nothing here ever raises.

recall(query, k=6) -> list[dict]
  Keyword-searches every `.md` file under VAULT_DIR (recursively) and returns the
  top-k matching snippets. Each hit is a dict with the SAME keys the rest of
  JARVIS expects: text, source, heading, score, store. Returns [] when memory
  isn't configured/available.

remember(text, tags=None, kind="note") -> dict
  Appends a timestamped markdown bullet to a memory note inside VAULT_DIR
  (creating it with a small header the first time). Returns a result dict
  ({ok, vault, error}); reports that memory isn't configured rather than raising.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

# dotenv is a hard dependency of JARVIS, but guard the load anyway so memory can
# never be the thing that crashes import. (fastlane/agent already load .env on
# their own import; this is just belt-and-suspenders.)
try:
    from dotenv import load_dotenv

    BASE_DIR = Path(__file__).resolve().parent.parent
    load_dotenv(BASE_DIR / ".env")
except Exception:  # pragma: no cover - dotenv missing or unreadable .env
    pass

# The fixed filename JARVIS writes its own memories into, inside VAULT_DIR.
MEMORY_NOTE_NAME = "assistant-memory.md"

# How many words a snippet may contain before recall trims it (keeps tool
# results readable and prevents one giant line from drowning the context).
_SNIPPET_MAX_CHARS = 240

# --------------------------------------------------------------------------- #
# Logging (stderr only — never stdout, never prints secrets)
# --------------------------------------------------------------------------- #
logger = logging.getLogger("jarvis.memory")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [memory] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


# --------------------------------------------------------------------------- #
# Config helper — resolved at CALL TIME (not import time) so the env var can be
# set before or after this module is imported, and so a folder that appears
# later is picked up without a restart.
# --------------------------------------------------------------------------- #
def _vault_dir() -> Path | None:
    """Return the configured vault directory as a Path, or None if unusable.

    None means "memory not configured": VAULT_DIR unset/blank, or it doesn't
    point at an existing directory.
    """
    raw = (os.environ.get("VAULT_DIR") or "").strip()
    if not raw:
        return None
    try:
        p = Path(raw).expanduser()
    except Exception:
        return None
    return p if p.is_dir() else None


# --------------------------------------------------------------------------- #
# Public API — recall (read)
# --------------------------------------------------------------------------- #
def recall(query: str, k: int = 6) -> list[dict[str, Any]]:
    """Keyword-search every `.md` file under VAULT_DIR and return the top-k hits.

    Each hit is a dict with keys: text, source, heading, score, store — the exact
    shape JARVIS's callers expect (score is always a float and always present).
    Returns [] (never raises) if memory isn't configured or nothing matches.
    """
    query = (query or "").strip()
    d = _vault_dir()
    if d is None:
        logger.info("recall: memory not configured (VAULT_DIR unset/missing)")
        return []
    if not query:
        return []

    # Build the search terms: words of length > 2 (so "to"/"a" don't dominate).
    # If the query is all short words, fall back to the whole lowercased query.
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2]
    if not terms:
        terms = [query.lower()]

    logger.info("recall: query=%r k=%d terms=%s", query[:80], k, terms)

    hits: list[dict[str, Any]] = []
    try:
        md_files = sorted(d.rglob("*.md"))
    except Exception as exc:  # e.g. a permission error walking the tree
        logger.error("recall: failed to list vault: %s", exc)
        return []

    for md in md_files:
        try:
            content = md.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue  # unreadable file — skip, never raise
        heading = ""
        for line in content.splitlines():
            stripped = line.strip()
            # Track the nearest markdown heading as a breadcrumb for snippets.
            if stripped.startswith("#"):
                heading = stripped.lstrip("#").strip()
                continue
            if not stripped:
                continue
            low = stripped.lower()
            score = sum(low.count(t) for t in terms)
            if score:
                hits.append({
                    "text":    stripped[:_SNIPPET_MAX_CHARS],
                    "source":  md.name,
                    "heading": heading,
                    "score":   float(score),  # callers do round()/:.3f — must be numeric
                    "store":   "vault",
                })

    # Highest score first; trim to k. Stable sort keeps document order on ties.
    hits.sort(key=lambda h: h["score"], reverse=True)
    results = hits[:k]
    for r in results:
        logger.info("  hit score=%.3f [%s] %s", r["score"], r["source"], r["text"][:50])
    logger.info("recall: returned %d hits", len(results))
    return results


# --------------------------------------------------------------------------- #
# Public API — remember (write)
# --------------------------------------------------------------------------- #
def remember(text: str, tags: list[str] | None = None, kind: str = "note") -> dict[str, Any]:
    """Append a timestamped markdown bullet to the memory note inside VAULT_DIR.

    Creates the note (with a small header) the first time. Returns a result dict
    with the shape JARVIS's callers expect: {ok, vault, error?}. Never raises;
    if VAULT_DIR isn't configured, returns ok=False with an explanatory error.

    `tags` and `kind` are accepted for API compatibility; tags are appended to the
    bullet as `#hashtags` when provided.
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "vault": False, "error": "empty memory"}

    d = _vault_dir()
    if d is None:
        return {"ok": False, "vault": False, "error": "memory not configured"}

    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")  # portable — no %- codes
    note = d / MEMORY_NOTE_NAME
    try:
        if not note.exists():
            created = ts[:10]
            note.write_text(
                "---\n"
                "type: source\n"
                f"created: {created}\n"
                f"updated: {created}\n"
                "---\n\n"
                "# Assistant Memory\n\n"
                "Things the assistant was told to remember, auto-appended at "
                "runtime.\n\n",
                encoding="utf-8",
            )
        tagstr = ("  " + " ".join(f"#{t}" for t in tags)) if tags else ""
        with note.open("a", encoding="utf-8") as f:
            f.write(f"- **{ts}** — {text}{tagstr}\n")
        logger.info("remember: vault=True text=%r", text[:80])
        return {"ok": True, "vault": True}
    except Exception as exc:
        logger.error("remember: vault append failed: %s", exc)
        return {"ok": False, "vault": False, "error": str(exc)}
