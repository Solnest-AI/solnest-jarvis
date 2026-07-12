# setup-keys.ps1 — reads your .env and wires up your keys.
#
# Your keys go from the .env file straight into the tool. They are never printed,
# never sent to the AI, and never written into a chat.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".env")) {
  Write-Host "No .env file found in this folder." -ForegroundColor Red
  Write-Host "   Copy .env.template to .env, paste your keys into it, and run this again."
  exit 1
}

# Load .env without printing anything.
Get-Content ".env" | ForEach-Object {
  if ($_ -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$') {
    $v = $matches[2].Trim().Trim('"').Trim("'")
    if ($v) { Set-Item -Path ("Env:" + $matches[1]) -Value $v }
  }
}

$found = 0
function Need($name, $label) {
  if (-not (Test-Path ("Env:" + $name)) -or -not (Get-Item ("Env:" + $name)).Value) {
    Write-Host "  [ -- ] $label  ($name) blank, skipping" -ForegroundColor DarkGray
  } else { Write-Host "  [OK] $label" -ForegroundColor Green; $script:found = 1 }
}

Write-Host "Checking your .env..."

Need "ANTHROPIC_API_KEY" "JARVIS's brain"
Need "ELEVENLABS_API_KEY" "JARVIS's voice"

if ($found -eq 0) {
  Write-Host ""
  Write-Host "Nothing is filled in yet. Open the .env file, paste your key(s), save, and run this again." -ForegroundColor Red
  Write-Host "(See KEYS.md for where to get each one.)"
  exit 1
}
Write-Host ""

Write-Host ""; Write-Host "Done. Now FULLY QUIT AND REOPEN Claude Code so it picks up your keys."
