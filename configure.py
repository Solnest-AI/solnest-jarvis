"""
configure.py — friendly, interactive first-time setup for JARVIS.

Run automatically by setup.sh / setup.bat (right after dependencies install).
It asks a few plain-English questions and writes your private .env file FOR you —
no hand-editing required. Standard library only; works on Mac + Windows,
Python 3.10+. All prompts are ASCII so Windows consoles never show mojibake.

If a .env already exists, this does nothing (so it can never wipe your real keys).
"""
from __future__ import annotations

from pathlib import Path

BASE = Path(__file__).resolve().parent
ENV = BASE / ".env"
EXAMPLE = BASE / ".env.example"


def ask(prompt: str) -> str:
    """Prompt the user and return a trimmed answer."""
    return input(prompt).strip()


def choose_brain() -> tuple[str, str]:
    """Ask which brain to use. Returns (engine, api_key).

    engine is the value written to JARVIS_ENGINE:
      'fastlane' for the Anthropic API key path, 'sdk' for the free Claude Code path.
    """
    print("")
    print("STEP 1 of 4 - Pick JARVIS's brain (required).")
    print("")
    print("  api  = Anthropic API key. RECOMMENDED. Fastest (about 2s), smoothest.")
    print("         Costs a fraction of a cent per chat.")
    print("  cli  = Free. Uses Claude Code (the 'claude' command). Slower.")
    print("")

    while True:
        choice = ask("Type 'api' (recommended) or 'cli': ").lower()
        if choice == "api":
            return ask_for_key()
        if choice == "cli":
            print("Got it - using the free Claude Code brain.")
            return "sdk", ""
        print("Please type exactly 'api' or 'cli'.")


def ask_for_key() -> tuple[str, str]:
    """Loop until we get a key, or the user backs out to the free 'cli' option."""
    while True:
        answer = input(
            "Paste your Anthropic API key (starts with sk-ant-), "
            "or type 'cli' to use the free option instead: "
        ).strip()
        # Check the 'cli' escape hatch BEFORE treating input as a key,
        # so typing 'cli' never gets saved as the key.
        if answer.lower() == "cli":
            print("No problem - switching to the free Claude Code brain.")
            return "sdk", ""
        if not answer:
            print("That looked empty. Paste your key, or type 'cli' for the free option.")
            continue
        print("Great - saving your API key.")
        return "fastlane", answer


def choose_elevenlabs() -> str:
    """Optional ElevenLabs key for a premium voice. Empty = free browser voice."""
    print("")
    print("STEP 2 of 4 - Premium voice (optional).")
    print("An ElevenLabs API key gives JARVIS a realistic premium voice.")
    print("Leave it blank to use the free voice in your browser.")
    return ask("ElevenLabs API key (optional - press Enter to skip): ")


def choose_memory() -> str:
    """Optional memory folder. Empty = no long-term memory."""
    print("")
    print("STEP 3 of 4 - Long-term memory (optional).")
    print("Point me at a folder (an Obsidian vault works great) and I'll remember")
    print("things there. Leave it blank for no memory.")
    return ask("Folder path for memory (optional - press Enter to skip): ")


def choose_name() -> str:
    """Optional: what JARVIS should call you. Empty is fine."""
    print("")
    print("STEP 4 of 4 - Your name (optional).")
    print("What should JARVIS call you? Leave it blank to skip.")
    return ask("Your name (optional - press Enter to skip): ")


def build_env(engine: str, api_key: str, eleven_key: str, memory_dir: str, user_name: str) -> str:
    """
    Build the .env contents. Start from .env.example as the template and override
    ONLY the chosen settings; keep every other line (comments, MODEL, the default
    JARVIS voice id, etc.).

    Falls back to a minimal valid .env if .env.example is missing.
    """
    overrides = {
        "JARVIS_ENGINE=": f"JARVIS_ENGINE={engine}",
        "ANTHROPIC_API_KEY=": f"ANTHROPIC_API_KEY={api_key}",
        "ELEVENLABS_API_KEY=": f"ELEVENLABS_API_KEY={eleven_key}",
        "VAULT_DIR=": f"VAULT_DIR={memory_dir}",
        "USER_NAME=": f"USER_NAME={user_name}",
    }

    if EXAMPLE.exists():
        # Read with utf-8 — .env.example contains non-ASCII comment characters.
        template = EXAMPLE.read_text(encoding="utf-8")
        out_lines = []
        for line in template.splitlines():
            replaced = False
            for prefix, new_value in overrides.items():
                # Match the full "KEY=" (including the '=') so e.g.
                # ELEVENLABS_API_KEY= never collides with ELEVENLABS_VOICE_ID= or
                # ELEVENLABS_MODEL=.
                if line.startswith(prefix):
                    out_lines.append(new_value)
                    replaced = True
                    break
            if not replaced:
                out_lines.append(line)
        return "\n".join(out_lines) + "\n"

    # Fallback: minimal valid .env if the template went missing.
    return (
        f"ANTHROPIC_API_KEY={api_key}\n"
        f"JARVIS_ENGINE={engine}\n"
        "FASTLANE_MODEL=claude-sonnet-4-6\n"
        f"ELEVENLABS_API_KEY={eleven_key}\n"
        "ELEVENLABS_VOICE_ID=gJx1vCzNCD1EQHT212Ls\n"
        "ELEVENLABS_MODEL=eleven_turbo_v2_5\n"
        f"USER_NAME={user_name}\n"
        "JARVIS_PORT=8800\n"
        "PERMISSION_MODE=bypassPermissions\n"
        "WHISPER_MODEL=base\n"
        "CLAUDE_BIN=claude\n"
        f"VAULT_DIR={memory_dir}\n"
    )


def main() -> int:
    if ENV.exists():
        print("")
        print("You already have a .env file, so your existing settings are being kept.")
        print("To reconfigure: delete .env and run setup again.")
        return 0

    print("")
    print("Let's set up JARVIS. Just a few quick questions -")
    print("your answers get saved to a private .env file for you.")

    engine, api_key = choose_brain()
    eleven_key = choose_elevenlabs()
    memory_dir = choose_memory()
    user_name = choose_name()

    contents = build_env(engine, api_key, eleven_key, memory_dir, user_name)
    # newline="\n" keeps the file LF-clean on Windows (no CRLF translation).
    ENV.write_text(contents, encoding="utf-8", newline="\n")

    brain_label = (
        "Anthropic API key (fast, recommended)"
        if engine == "fastlane"
        else "Claude Code / free (the 'claude' command)"
    )
    voice_label = "premium ElevenLabs voice" if eleven_key else "free browser voice"
    memory_label = f"on - {memory_dir}" if memory_dir else "off"
    name_label = user_name if user_name else "(not set)"

    print("")
    print("All set! Here's what you chose:")
    print(f"  Brain:  {brain_label}")
    print(f"  Voice:  {voice_label}")
    print(f"  Memory: {memory_label}")
    print(f"  Name:   {name_label}")
    print("")
    print(f"Saved to: {ENV}")
    print("")
    print("Next step:")
    print("  Windows: double-click run.bat")
    print("  Mac:     bash run.sh")
    print("Then open http://localhost:8800 in Chrome.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
