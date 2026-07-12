# CLAUDE.md — Setting up JARVIS

**Follow `AGENTS.md` in this folder.** It is the single source of truth for setting JARVIS up.
When the user says **"set this up"**, **"get this running"**, **"start Jarvis"**, or anything
similar, open `AGENTS.md` and walk them through that runbook — **one question at a time**,
waiting for each answer, in plain non-technical language, using the commands that match their
OS (Windows vs Mac).

## The one Claude Code nuance

The **recommended brain is the Anthropic API key** (`JARVIS_ENGINE=fastlane`) — it replies
**instantly, about 2 seconds**. Present that first at **Step B**.

The Claude Code nuance is just this: because you're Claude Code, the user **already has Claude
Code installed** (the `claude` command on PATH), so the **free** option (`JARVIS_ENGINE=sdk`)
*will* work out of the box for them if they prefer no cost. But it's the **slower** path — it
runs each reply through the Claude Code app, so it takes several seconds, not 2. So offer it as
the no-cost fallback, not the default:

> "I'd go with an **Anthropic API key** — that's the instant one, replies in about 2 seconds.
> Or, since you already have Claude Code, you can run it **free** through that instead — it
> works, it's just slower (several seconds per reply). Which do you want?"

If they pick **API key** (recommended): set `JARVIS_ENGINE=fastlane` and their key. No extra
install — the `anthropic` library is already in the base `requirements.txt`.

If they pick **Free**: remember the two things from `AGENTS.md` Step F — set `JARVIS_ENGINE=sdk`
**and** `pip install "claude-agent-sdk>=0.2.87"` into the venv (it's commented out of the base
`requirements.txt`, so it won't install on its own). The `claude` command is already on PATH
for you, so the free brain will work.

## Don't forget (full detail is in AGENTS.md)

- **Never run `setup.bat` / `setup.sh` / `configure.py`** — they block on typed input and will
  hang you. Do the equivalent steps yourself.
- **Keys go only in `.env`** (gitignored). Never echo a key back. Never overwrite an existing
  `.env` without asking.
- Write `.env` by copying `.env.example` and changing only the answered `KEY=` lines.
- Start the server in the background, wait for `Application startup complete`, then send them
  to **http://localhost:8800** in **Chrome**. Day-to-day they start with `run.bat` / `run.sh`.
