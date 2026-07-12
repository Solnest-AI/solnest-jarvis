# AGENTS.md — How to set up JARVIS for a non-technical person

**This file is instructions for YOU, the AI agent** (Claude Code, Cursor, Codex, or any
agent tool with this folder open). It is the single source of truth for setup. `CLAUDE.md`
points here.

When the user says **"set this up"**, **"get this running"**, **"start Jarvis"**, or
anything similar, follow this runbook. The person on the other end is **non-technical** —
treat them like a friend who has never used a terminal. Be warm, plain, and patient.

---

## The golden rules

1. **One question at a time. Wait for each answer before moving on.** Never dump all the
   questions at once. Never assume an answer.
2. **You are setting JARVIS up on THIS computer.** Detect the OS first (Windows vs Mac) and
   use the matching commands throughout. Never mix them.
3. **Do NOT run `setup.bat`, `setup.sh`, or `configure.py`.** Those are the interactive path
   for people who are NOT using an agent — they block waiting for typed input and **will hang
   you**. You do the equivalent yourself with the steps below.
4. **Keys go ONLY in `.env`** (it's gitignored). Never echo a key back to the user, never
   print it, never put it in any other file, never commit it.
5. **Never clobber an existing `.env`.** If `.env` already exists, do NOT overwrite it — ask
   the user first whether they want to reconfigure (and if yes, that they're okay deleting the
   old one). Otherwise keep their existing settings.
6. **Plain language only.** No jargon. If you must use a technical word, explain it in five
   words or fewer.

---

## Detect the OS first

Figure out whether this is **Windows** or **Mac/Linux** before you run anything. Throughout
this runbook, commands are given as **Windows** / **Mac**. Pick one set and stick with it.

---

## Step A — Find Python (3.10 or newer) — SEARCH, don't just check PATH

Python is often **installed but not on PATH** (a very common Windows case — e.g. Python
3.14 installed at `C:\Users\<user>\AppData\Local\Python\pythoncore-3.14-64\python.exe`,
registered in the registry, yet `py` / `python` / `python3` all fail). **A PATH-only check
wrongly concludes "no Python."** So SEARCH properly before you ever tell the user to install
anything. Go in this order and STOP at the first Python 3.10+ you find:

### A1. Try PATH first (Windows)

```powershell
py -3 --version        # if this fails, try the next:
python --version
```

If either prints **3.10 or higher** → done. You'll use `py -3` (or `python`) for the rest.
Skip to Step B.

**Mac:** `python3 --version` — if it prints 3.10+, done, skip to Step B.

### A2. If PATH failed, search the Windows registry + common install dirs

Installed Pythons register themselves. Search the registry (the `(default)` value of each
`InstallPath` key is the **install directory**; `python.exe` lives inside it):

```powershell
# Per-user and machine-wide registrations. The (default) value = the install dir.
Get-ItemProperty 'HKCU:\SOFTWARE\Python\PythonCore\*\InstallPath' -EA SilentlyContinue |
  ForEach-Object { Join-Path $_.'(default)' 'python.exe' }
Get-ItemProperty 'HKLM:\SOFTWARE\Python\PythonCore\*\InstallPath' -EA SilentlyContinue |
  ForEach-Object { Join-Path $_.'(default)' 'python.exe' }
```

Also check the usual install locations directly:

```powershell
# Common per-user, store-style, and machine-wide install dirs.
Get-ChildItem `
  "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", `
  "$env:LOCALAPPDATA\Python\pythoncore-3.*-64\python.exe", `
  "C:\Python3*\python.exe" -EA SilentlyContinue | Select-Object -Expand FullName
```

Confirm any candidate is 3.10+ by running it with its full path:

```powershell
& "C:\full\path\to\python.exe" --version
```

### A3. If a Python was found this way (installed but off PATH) → USE IT BY FULL PATH

Remember that **full path** and use it for **every** later command instead of `py`/`python`.
The key one is creating the environment in Step F1:

```powershell
& "C:\full\path\to\python.exe" -m venv .venv
```

After the venv exists, the `.venv\Scripts\...` commands in Step F work normally (they don't
need PATH). Make a note to yourself which Python path you're using so Step F1 uses it too.

### A4. ONLY if no Python 3.10+ exists anywhere → install it

If — and only if — none of A1–A3 found a Python 3.10+, then ask the user to install one:

> "You'll need Python first — it's free. Grab it from **https://www.python.org/downloads/**.
> I'd grab **Python 3.12 or 3.13** — they're rock-solid. (The newest release sometimes
> doesn't have all the prebuilt packages yet, so I skip the bleeding edge.) During install,
> **tick the box that says 'Add python.exe to PATH'**. Install it, then tell me and we'll
> keep going."

If **`winget`** is available (Windows), you may offer to install it for them:

```powershell
winget install -e --id Python.Python.3.12
```

**Mac:** point them to the same page (**https://www.python.org/downloads/**) for the macOS
installer, or `brew install python@3.12` if they have Homebrew.

After install, re-run Step A from the top to pick it up.

**Stop here until a Python 3.10+ is found or installed.** Don't continue without it.

---

## Step B — Ask: JARVIS's brain (REQUIRED)

This is the only required question. Ask it plainly and wait:

> "First question: what should power JARVIS's thinking? Two options:
>
> **1. Anthropic API key** — *recommended.* This is the **instant** one: JARVIS replies in
> about **2 seconds**. You paste a key from **console.anthropic.com** (it starts with
> `sk-ant-`), add a little credit, and it costs a fraction of a cent per chat. Nothing else to
> install.
>
> **2. Free (your Claude Code subscription)** — no API key, no cost. The trade-off: it's
> **slower — not instant.** It runs through the Claude Code app on this computer, so each
> reply takes several seconds.
>
> Which one — the instant API key, or the free option?"

### If they choose API KEY (recommended)
- Tell them where to get it:
  > "Go to **console.anthropic.com → Settings → API Keys**, create a key (it starts with
  > `sk-ant-`), add a little credit, and paste it here."
- This sets `JARVIS_ENGINE=fastlane` and `ANTHROPIC_API_KEY=<their key>`.
- **No extra package is needed** — the `anthropic` library is already in the base install.
- Never repeat the key back to them. Just confirm: "Got it, saved."

### If they choose FREE (Claude Code subscription)
- Set expectations honestly: this is the **slower** path — **not instant**. It calls the
  Claude Code app under the hood, so replies take several seconds each (vs. ~2s for the API key).
- This sets `JARVIS_ENGINE=sdk` in `.env` (the value must be **exactly** `sdk`).
- **The free brain needs an extra Python package that the base install does NOT include**
  (`claude-agent-sdk` is commented out in `requirements.txt`). You MUST install it into the
  venv in Step F.
- **The free brain also needs the `claude` command (Claude Code) installed and on PATH** —
  the `claude-agent-sdk` package is a wrapper that calls it. Verify in Step F. If you're an
  agent running *inside* Claude Code, it's already there. If you're running in Cursor / Codex /
  another tool, it may not be — handle that in Step F.
- Leave `ANTHROPIC_API_KEY` blank.

---

## Step C — Ask: premium voice (OPTIONAL)

> "Next, optional: how should JARVIS sound?
>
> **Premium voice** — a realistic JARVIS voice. You paste a free key from **elevenlabs.io**
> (Profile → API Key).
>
> **Free voice** — your browser's built-in voice, no key needed.
>
> Either way JARVIS still shows everything as text on screen. Want premium, or free?"

- **Premium:** set `ELEVENLABS_API_KEY=<their key>`. **Leave `ELEVENLABS_VOICE_ID` at its
  default** (that default is the JARVIS voice — don't change it).
- **Free:** leave `ELEVENLABS_API_KEY` blank.

---

## Step D — Ask: long-term memory / Obsidian (OPTIONAL)

> "Do you want JARVIS to **remember things across chats** — your preferences, names, notes,
> decisions? It saves them as plain notes you can read in a free app called Obsidian. Yes or no?"

### If YES — walk them through Obsidian end to end

This Jarvis folder already ships a ready-made **`memory`** folder for exactly this — you do
NOT have to make one or hunt for a path. JARVIS will save memories into `memory` as a file
called `assistant-memory.md`, and Obsidian lets the user read and edit them. Walk them through
it in three friendly steps:

**1. Download + install Obsidian (free).**
> "First, grab **Obsidian** — it's a free notes app. Go to **https://obsidian.md**, click the
> big download button, and install it like any normal app (open the installer, click through,
> done). Tell me when it's open."

**2. Open the Jarvis `memory` folder as a vault.**
> "In Obsidian, click **'Open folder as vault'** (it's on the first screen, or under the vault
> switcher in the bottom-left). Then browse to **this Jarvis folder** and pick the **`memory`**
> folder inside it. That's it — Obsidian is now pointed at JARVIS's memory."

**3. You set the path.** The folder is `<this-Jarvis-folder>\memory`. Set:
- `VAULT_DIR=<full path to this Jarvis folder>\memory`
  (e.g. `VAULT_DIR=C:\Users\You\Jarvis\memory` on Windows, or
  `VAULT_DIR=/Users/you/Jarvis/memory` on Mac).

> **Why the dedicated `memory` folder (and not the whole Jarvis folder):** JARVIS searches
> *every* `.md` file under `VAULT_DIR` when it recalls. If you pointed it at the whole Jarvis
> folder, recall would match the README/CLAUDE/AGENTS docs too — junk. The `memory` subfolder
> keeps recall clean.

**No extra package is needed** — memory is just plain `.md` files. Then tell them:
> "Now just tell JARVIS **'remember that…'** and it saves a note in your `memory` folder that
> you can open and see in Obsidian. Ask **'what do you know about…'** to pull it back."

For the full friendly guide, point them to **`OBSIDIAN-MEMORY.md`** in this folder.

### If NO
Leave `VAULT_DIR` blank. Everything else still works; JARVIS just won't remember between chats.

---

## Step E — Ask: their name (OPTIONAL)

> "Last question: what should JARVIS call you? (You can skip this.)"

Set `USER_NAME=<their name>` if given, otherwise leave it blank.

---

## Step F — Build it (no more questions — you do this)

Now do the work. Use the command set matching the OS.

### F1. Create the private environment

Use the **same Python you found in Step A**:

- **Windows, Python was on PATH:** `py -3 -m venv .venv` (or `python -m venv .venv`).
- **Windows, Python was found OFF PATH in Step A (by full path):** use that full path —
  `& "C:\full\path\to\python.exe" -m venv .venv`. **Do NOT** use `py`/`python` here; they'll
  fail for the same reason Step A's PATH check did.
- **Mac:** `python3 -m venv .venv`

After the venv exists, the `.venv\Scripts\...` (Windows) / `.venv/bin/...` (Mac) commands below
work regardless of how Python was found — they don't depend on PATH.

### F2. Install dependencies

- **Windows:** `.venv\Scripts\python -m pip install --upgrade pip` then
  `.venv\Scripts\pip install -r requirements.txt`
- **Mac:** `.venv/bin/pip install --upgrade pip` then
  `.venv/bin/pip install -r requirements.txt`

### F3. (ONLY if they picked the FREE brain in Step B) Install the free-brain package + verify Claude Code

- **Windows:** `.venv\Scripts\pip install "claude-agent-sdk>=0.2.87"`
- **Mac:** `.venv/bin/pip install "claude-agent-sdk>=0.2.87"`

Then verify the `claude` command is available (the free brain calls it):

- **Windows:** `where claude`
- **Mac:** `which claude`

If `claude` is **found** → good, continue. If it is **NOT found**, tell the user plainly:
> "The free brain needs **Claude Code** installed on this computer (it's the `claude`
> command). If you have it, make sure it's on your PATH. Otherwise, the easiest path is to
> switch to the **Anthropic API key** option — want to do that instead?"
If they switch, redo Step B as the API-key branch (set `JARVIS_ENGINE=fastlane` and the key)
and skip the `claude-agent-sdk` install.

### F4. Write the `.env` file

**Do this by copying `.env.example` and changing only the lines for the answers above** — do
NOT run `configure.py`. Keep every other line exactly as-is (comments, `FASTLANE_MODEL`, the
default `ELEVENLABS_VOICE_ID`, `JARVIS_PORT=8800`, etc.).

Override only these keys based on the answers (match the full `KEY=` so you don't hit a
similarly-named line):

| Setting | Set to |
|---|---|
| `JARVIS_ENGINE=` | `fastlane` (API key — recommended) **or** `sdk` (free brain) |
| `ANTHROPIC_API_KEY=` | their key (API branch) or blank (free branch) |
| `ELEVENLABS_API_KEY=` | their key, or blank |
| `VAULT_DIR=` | the `memory` folder path (`<this Jarvis folder>\memory`), or blank |
| `USER_NAME=` | their name, or blank |

Leave `ELEVENLABS_VOICE_ID=` at the default value already in `.env.example`.

**Before writing: if `.env` already exists, stop and ask** (golden rule #5). Only write `.env`
when there isn't one, or the user explicitly approved replacing it.

---

## Step G — Start it and hand off

Launch the server **in the background** so this first session gets them talking:

- **Windows:** `.venv\Scripts\python server.py`
- **Mac:** `.venv/bin/python server.py`

Wait until it's actually ready before sending them to the browser — watch the output for
**`Application startup complete`** (or confirm the port is listening). The default address is
**http://localhost:8800**. (If the port was changed in `.env`, use that port instead.)

Then tell them:
> "JARVIS is running. Open **Google Chrome** and go to **http://localhost:8800**. Tap the mic
> (click **Allow** when Chrome asks for the microphone) and just talk — JARVIS answers out
> loud. You can also type."

**How to stop / restart later** (tell them this so they're not stuck):
> "From now on, you don't need me to start it. To **start** JARVIS, double-click **`run.bat`**
> (Windows) or run **`bash run.sh`** (Mac). To **stop** it, close that window. If you ever want
> to change your settings, edit the `.env` file in a text editor, or delete it and ask me to
> set it up again."

---

## Quick reference — env vars (from `.env.example`)

| Var | Meaning | Default |
|---|---|---|
| `JARVIS_ENGINE` | `fastlane` = API key, `sdk` = free Claude Code | `fastlane` |
| `ANTHROPIC_API_KEY` | Anthropic key (API branch only) | blank |
| `FASTLANE_MODEL` | model for the API branch | `claude-sonnet-4-6` (leave as-is) |
| `ELEVENLABS_API_KEY` | premium voice key | blank |
| `ELEVENLABS_VOICE_ID` | the JARVIS voice | preset (leave as-is) |
| `VAULT_DIR` | memory folder path (the shipped `memory` folder) | blank |
| `USER_NAME` | what JARVIS calls the user | blank |
| `JARVIS_PORT` | web port | `8800` |

## Reminders
- The manual path (`setup.bat` / `setup.sh` / `configure.py` / `run.bat` / `run.sh`) still
  works for people NOT using an agent — leave it alone.
- **Python may be installed but off PATH** — SEARCH (registry + common dirs) before saying
  "no Python," and if found off PATH, create the venv with that **full** `python.exe` path.
- **API brain (recommended, instant ~2s)** → `JARVIS_ENGINE=fastlane` + key. No extra install.
- Free brain (slower, not instant) → `JARVIS_ENGINE=sdk` **and** `pip install claude-agent-sdk`
  **and** `claude` on PATH. Missing any one = the free brain won't work.
