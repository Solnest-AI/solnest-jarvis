# UPDATE — JARVIS (Claude reads this)

**You are Claude. The user pasted this repo's link and asked you to update their JARVIS. Do it FOR them, safely, in plain language.**

**The one rule that matters: this update must NOT wipe their `.env` or their memories.** JARVIS keeps its keys, its voice settings, and everything it remembers about the user *inside* its own folder. Follow the preserve list exactly.

---

## Step 0 — Find their install

JARVIS isn't installed to a fixed path — the user unzipped it somewhere (Desktop, Documents, wherever). Find it:

- Look for a folder containing both `server.py` and a `jarvis/` subfolder.
- Common spots: `~/Desktop/Jarvis`, `~/Documents/Jarvis`, `~/Jarvis`.
- If you can't find it, just ask: *"Where did you unzip JARVIS? Drag the folder in and I'll take it from there."*

If they've never installed it, don't "update" — point them at the Skool zip for a fresh install and stop.

## Step 1 — Check if they even need this

Compare `VERSION` in their folder to `VERSION` in this repo. Match? Say so and stop.

> You're already on the latest version (`X.Y.Z`) — nothing to update.

## Step 2 — PRESERVE THIS (do not skip)

Back these up to a temp folder first. These are **theirs**:

| Path (inside the JARVIS folder) | What it is |
|---|---|
| `.env` | **Their API keys**, voice ID, and settings. The whole config. |
| `state/` | Their session — what JARVIS is mid-conversation about. |
| `memory/` | **Everything JARVIS remembers about them.** Irreplaceable. |
| `logs/` | Their history. Harmless to keep. |
| `.venv/` | The Python environment. Keep it — rebuilding is slow. |

Two more that live **outside** the folder — do not touch either:
- `~/.jarvis/roster.json` — their specialist roster.
- Their Google credentials folder (wherever `CREDS_DIR` points).

`.env.example` and `.env.template` SHOULD both be overwritten. They are blank templates, not their config. Their real config is `.env`, which is on the preserve list above.

## Step 3 — Pull the latest

```bash
git clone --depth 1 https://github.com/Solnest-AI/solnest-jarvis.git /tmp/jarvis-update
```

No `git`? Download `https://github.com/Solnest-AI/solnest-jarvis/archive/refs/heads/main.zip`.

## Step 4 — Copy the new files over

Copy file over file. **Never delete the JARVIS folder first** — that's how you'd destroy their memories.

Update these:
- `jarvis/` (the Python package — all of it)
- `web/` (the interface)
- `server.py`, `configure.py`, `requirements.txt`
- `setup.sh`, `setup.bat`, `run.sh`, `run.bat`, `share.sh`, `share.bat`
- `README.md`, `CLAUDE.md`, `AGENTS.md`, `OBSIDIAN-MEMORY.md`, `KEYS.md`, `CHANGELOG.md`, `LICENSE`, `VERSION`
- `.env.example`, `.env.template`, `setup-keys.sh`, `setup-keys.ps1` (templates and helpers, never their `.env`)
- `persona.md` — **ask first.** If they've customized JARVIS's personality, overwriting it is rude. Ask: *"Did you edit JARVIS's personality? If so I'll leave it alone."* When in doubt, leave it.

If `requirements.txt` changed, re-run the dependency install into the existing `.venv`. Say that out loud — it takes a minute.

## Step 5 — Put their config and memories back

Restore everything from Step 2. Then verify before you say a word:

- Does `.env` still exist, and is it still **non-empty**?
- Is `memory/` still there, with their files in it?

If either is missing, restore from the backup. **Never report success until you've confirmed their memories survived.**

## Step 6 — Restart and confirm

Have them stop JARVIS (Ctrl+C in its terminal) and start it again with `run.sh` / `run.bat`. Then summarize what's new from `CHANGELOG.md` — two or three plain bullets.

> Updated to `X.Y.Z`. Your keys and everything JARVIS remembers about you came through untouched. Restart it and you're good.

## If something goes wrong

Restore the Step 2 backup. One friendly sentence, no raw errors. Their memories are the irreplaceable thing here — and you have a backup of them.
