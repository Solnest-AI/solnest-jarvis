# 🧠 Give JARVIS a Memory with Obsidian (Optional)

> **This is 100% optional.** JARVIS works great without it — you just won't have
> long‑term memory. If you don't want it, skip this whole page. Nothing breaks.

By default, JARVIS remembers everything **within** a conversation, but forgets it
all when you restart. Want it to remember things for good — your preferences,
names, notes, decisions — across every chat?

Just point it at a **folder**. That's it. No database, no extra software to keep
running. JARVIS saves memories as plain text notes in that folder and reads them
back when you ask.

**Good news: this Jarvis download already includes a ready-made `memory` folder**
for exactly this — you don't have to create one. We'll use **[Obsidian](https://obsidian.md)**
(a free notes app) to read and edit those memories, so they become real notes you
can open, search, and edit yourself.

---

## Set it up with Obsidian (recommended — three steps)

### Step 1 — Download + install Obsidian (free)

1. Go to **https://obsidian.md** and click the big **Download** button.
2. Run the installer like any normal app — open it, click through, done.
3. Open Obsidian.

### Step 2 — Open the Jarvis `memory` folder as a vault

In Obsidian, click **"Open folder as vault"** (it's on the first screen, or under
the vault switcher in the **bottom-left** corner). Browse to **this Jarvis folder**,
and pick the **`memory`** folder inside it. A "vault" is just a normal folder —
Obsidian doesn't change it, it just reads/writes notes there.

> That's the folder JARVIS already ships for your memories. Using it (instead of
> some random other folder) keeps recall clean — see **"Why the `memory` folder"** below.

### Step 3 — Point JARVIS at that `memory` folder

Tell JARVIS the path to the `memory` folder, which is `<this Jarvis folder>\memory`.
Two ways:

**During setup:** when JARVIS asks for a memory folder, paste that path.

**Or later, by hand:** open the **`.env`** file in the Jarvis folder with any text
editor (Notepad / TextEdit), find this line:

```
VAULT_DIR=
```

…and put the `memory` folder path right after the `=`:

```
# Windows example:
VAULT_DIR=C:\Users\You\Jarvis\memory

# Mac example:
VAULT_DIR=/Users/you/Jarvis/memory
```

Save the file and restart JARVIS (`run.bat` / `bash run.sh`).
**To turn memory off again**, just blank that line back to `VAULT_DIR=` and restart.

> **Getting the exact path:** on **Windows**, open the `memory` folder in **File
> Explorer**, click the **address bar** at the top, and copy the full path. On
> **Mac**, right‑click the folder in **Finder**, hold the **Option** key, then click
> **"Copy '<folder>' as Pathname"**.

> 💡 Don't want Obsidian? Any folder works — JARVIS just needs a folder path. But the
> built-in `memory` folder is the easy, clean choice.

### Why the `memory` folder (not the whole Jarvis folder)

JARVIS searches **every** `.md` file inside `VAULT_DIR` when it recalls. If you
pointed it at the whole Jarvis folder, it would also match the README and setup
docs — junk results. The dedicated `memory` folder keeps recall to just your real
memories.

---

## How to use it

- **Save a memory** — just tell JARVIS:
  > "remember that I prefer short answers"
  > "remember my dog's name is Cooper"
- **Recall** — just ask:
  > "what do you know about my preferences?"
  > "what's my dog's name?"
- **See your memories** — JARVIS writes them to a file called **`assistant-memory.md`**
  inside your folder. Open it in Obsidian (or any text editor): it's a simple
  bullet list with timestamps. You can read, edit, or delete entries yourself.

---

## Good to know

- 📝 **It's just markdown.** No database, nothing to install or keep running.
- 🔎 **JARVIS searches every `.md` file in the folder** — so any notes *you* keep
  in that vault become things it can find and use too, not just the ones it wrote.
- 🔒 **It all stays on your computer.** Your memories live in your folder and never
  leave your machine.
- 🚫 **Leave `VAULT_DIR` blank for no memory** — JARVIS just won't remember between
  chats. Everything else still works.
