<#
  setup.ps1 - one-shot setup for the whole bug-hunting rig on a Windows VM.

  Run ONCE, from an ADMIN PowerShell, inside the extracted claude-bounty folder:
      powershell -ExecutionPolicy Bypass -File .\setup.ps1

  It will:
    1. check prerequisites (Python, and warn if Claude Code / Git aren't installed)
    2. create C:\bounty (the data root) and add it to Defender exclusions
    3. install the recon toolkit            (scripts\install-tools.ps1)
    4. install the bot's Python packages into a local venv
    5. drop METHODOLOGY.md into C:\bounty and wire it into C:\bounty\CLAUDE.md
    6. create .env from the template (you fill in the token + your user id)
    7. register a scheduled task 'claude-tg' that starts the bot when you log in

  Options:
    -Root C:\bounty     data root (default C:\bounty)
    -SkipTools          don't install the recon toolkit (step 3)
    -SkipSchedule       don't create the autostart task (step 7)

  Prereqs you install yourself first (the script tells you if they're missing):
    - Python 3.12+            winget install Python.Python.3.12
    - Claude Code            irm https://claude.ai/install.ps1 | iex   (then: claude  ->  /login)
    - Git for Windows        winget install Git.Git
#>
#Requires -RunAsAdministrator
param(
    [string]$Root = "C:\bounty",
    [switch]$SkipTools,
    [switch]$SkipSchedule
)
$ErrorActionPreference = "Stop"
$App = $PSScriptRoot

function Info($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "    ! $m" -ForegroundColor Yellow }
function Good($m) { Write-Host "    + $m" -ForegroundColor Green }

Info "Prerequisites"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py -or ($py.Source -like "*WindowsApps*")) {
    throw "Python 3.12+ not found on PATH. Install it (winget install Python.Python.3.12), reopen PowerShell, re-run."
}
Good "python: $($py.Source)"
if (Get-Command claude -ErrorAction SilentlyContinue) { Good "claude: $((Get-Command claude).Source)" }
else { Warn "Claude Code not on PATH. Install: irm https://claude.ai/install.ps1 | iex  then run 'claude' and /login BEFORE starting the bot." }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Warn "Git for Windows not found (recommended): winget install Git.Git" }

Info "Data root $Root"
New-Item -ItemType Directory -Force -Path (Join-Path $Root "targets") | Out-Null
try { Add-MpPreference -ExclusionPath $Root -ErrorAction Stop; Good "Defender exclusion added for $Root" }
catch { Warn "Couldn't add Defender exclusion ($($_.Exception.Message)). Add $Root by hand in Windows Security." }

if (-not $SkipTools) {
    Info "Recon toolkit (this is the long step)"
    & (Join-Path $App "scripts\install-tools.ps1") -Root $Root
} else { Warn "Skipping toolkit (-SkipTools)" }

Info "Bot Python environment"
Push-Location $App
if (-not (Test-Path ".venv\Scripts\python.exe")) { python -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install -q --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -q -r requirements.txt
Good "packages installed into $App\.venv"
Pop-Location

Info "Methodology"
Copy-Item (Join-Path $App "config\METHODOLOGY.md") (Join-Path $Root "METHODOLOGY.md") -Force
$claudeMd = Join-Path $Root "CLAUDE.md"
if (-not (Test-Path $claudeMd)) { Set-Content -Encoding UTF8 $claudeMd "# Bug bounty workstation`n`n@METHODOLOGY.md`n" }
elseif ((Get-Content $claudeMd -Raw) -notmatch '@METHODOLOGY\.md') {
    Add-Content -Encoding UTF8 $claudeMd "`n@METHODOLOGY.md`n"
}
Good "METHODOLOGY.md in $Root and referenced from CLAUDE.md"

Info ".env"
$envFile = Join-Path $App ".env"
if (-not (Test-Path $envFile)) {
    (Get-Content (Join-Path $App ".env.example")) -replace '^BASE_DIR=.*', "BASE_DIR=$Root" | Set-Content -Encoding UTF8 $envFile
    Good "created $envFile (fill in TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS)"
} else { Good ".env already exists, left as-is" }
icacls $envFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null

if (-not $SkipSchedule) {
    Info "Autostart task 'claude-tg'"
    $action   = New-ScheduledTaskAction -Execute (Join-Path $App "start-bot.bat") -WorkingDirectory $App
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
                -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName "claude-tg" -Action $action -Trigger $trigger -Settings $settings `
        -Description "Claude Code Telegram bridge" -Force | Out-Null
    Good "task registered (starts the bot at logon)"
}

Write-Host ""
Write-Host "Setup done. Finish with:" -ForegroundColor Cyan
Write-Host "  1) If you haven't: run 'claude' once and /login (Pro/Max). Leave the token/key blank in .env."
Write-Host "  2) notepad $envFile   -> TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS"
Write-Host "  3) Open Burp, enable the MCP tab (+ its auto-approve), start proxied Chrome."
Write-Host "  4) Start the bot:  Start-ScheduledTask claude-tg    (or double-click start-bot.bat to watch bot.log)"
Write-Host "  5) In Telegram: /start  then  /target <name>"
Write-Host ""
Write-Host "Leaving the VM: CLOSE the RDP window, do NOT sign out, or the bot and Chrome stop."
