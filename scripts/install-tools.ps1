<#
  install-tools.ps1 - bug bounty toolkit for Windows (Azure VM / Windows 10-11 / Server 2022+)

  What it does
    1. Adds C:\bounty to the Microsoft Defender exclusion list (first, so nothing gets quarantined mid-install)
    2. Installs base software with winget: Git, Python 3.12, Go, Chrome, Nmap
    3. Downloads the latest Windows releases of recon/scanning tools from GitHub into C:\bounty\tools\bin
    4. Builds Go-only tools (tomnomnom & friends) into the same bin folder
    5. Installs Python tools into their own venv with small .cmd launchers in bin
    6. Clones SecLists, gf patterns and nuclei templates
    7. Adds C:\bounty\tools\bin to the system PATH and writes C:\bounty\CLAUDE.md so Claude Code knows what's installed

  Run it from an ADMIN PowerShell window:
    powershell -ExecutionPolicy Bypass -File .\install-tools.ps1
  Options:
    -Root D:\hunt        install somewhere else (default C:\bounty)
    -SkipSecLists        skip the ~1.5 GB SecLists clone
    -SkipWinget          skip base software (if you installed it yourself)
  Re-running is safe: it updates tools to the latest release.
  Optional: set $env:GITHUB_TOKEN first to avoid GitHub's 60-requests/hour limit.
#>
#Requires -RunAsAdministrator
param(
    [string]$Root = "C:\bounty",
    [switch]$SkipSecLists,
    [switch]$SkipWinget
)

# "Continue" on purpose: in Windows PowerShell 5.1, native tools writing progress to stderr (git, go)
# would otherwise abort the script. Cmdlets that must succeed use -ErrorAction Stop inside try/catch.
$ErrorActionPreference = "Continue"
$ProgressPreference    = "SilentlyContinue"   # Invoke-WebRequest is 10x faster without the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Tools  = Join-Path $Root "tools"
$Bin    = Join-Path $Tools "bin"
$Tmp    = Join-Path $Tools "_download"
$Lists  = Join-Path $Tools "wordlists"
$PyVenv = Join-Path $Tools "pyvenv"
$GoHome = Join-Path $Tools "go"
foreach ($d in $Root, $Tools, $Bin, $Tmp, $Lists, $GoHome) { New-Item -ItemType Directory -Force -Path $d | Out-Null }

$ok = New-Object System.Collections.Generic.List[string]
$failed = New-Object System.Collections.Generic.List[string]
function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Good($name) { $ok.Add($name); Write-Host "   [ok] $name" -ForegroundColor Green }
function Bad($name, $err) { $failed.Add("$name  ($err)"); Write-Host "   [!!] $name : $err" -ForegroundColor Yellow }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User") + ";" + $Bin
}

# ------------------------- 1. Defender exclusion -------------------------
Step "Defender: excluding $Root"
try {
    Add-MpPreference -ExclusionPath $Root -ErrorAction Stop
    # Go compiles hacking tools in its build cache; keep that inside the excluded folder too
    $env:GOCACHE = Join-Path $GoHome "cache"
    $env:GOPATH  = $GoHome
    $env:GOBIN   = $Bin
    if ((Get-MpPreference).ExclusionPath -contains $Root) { Good "Defender exclusion $Root" }
    else { Bad "Defender exclusion" "not visible in Get-MpPreference (Tamper Protection or policy may block it)" }
} catch {
    Bad "Defender exclusion" $_.Exception.Message
    Write-Host "   Add it by hand: Windows Security > Virus & threat protection > Manage settings > Exclusions" -ForegroundColor Yellow
}

# ------------------------- 2. Base software (winget) -------------------------
if (-not $SkipWinget) {
    Step "Base software via winget"
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $pkgs = [ordered]@{
            "Git.Git"              = "Git for Windows (gives Claude Code a bash shell)"
            "Python.Python.3.12"   = "Python 3.12"
            "GoLang.Go"            = "Go (for tools without Windows releases)"
            "Google.Chrome"        = "Google Chrome"
            "Insecure.Nmap"        = "Nmap (its Npcap installer may pop up - click through it)"
        }
        foreach ($id in $pkgs.Keys) {
            Write-Host "   installing $($pkgs[$id])..."
            winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
            # 0 = installed, -1978335189 = already installed / no newer version
            if ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq -1978335189) { Good $id }
            else { Bad $id "winget exit code $LASTEXITCODE" }
        }
    } else {
        Bad "winget" "not found (normal on Windows Server 2022). Install Git, Python and Go by hand, then re-run with -SkipWinget"
    }
}
Refresh-Path

# ------------------------- 3. GitHub release binaries -------------------------
Step "Downloading Windows releases from GitHub into $Bin"
$ghHeaders = @{ "User-Agent" = "bounty-tools-installer"; "Accept" = "application/vnd.github+json" }
if ($env:GITHUB_TOKEN) { $ghHeaders["Authorization"] = "Bearer $($env:GITHUB_TOKEN)" }

function Install-Release {
    param([string]$Repo, [string]$Pattern = '(?i)windows[_-](amd64|x86_64|x64).*\.(zip|tar\.gz|exe)$', [string]$RenameTo = "")
    $name = $Repo.Split("/")[1]
    try {
        $rel   = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest" -Headers $ghHeaders -ErrorAction Stop
        $asset = $rel.assets | Where-Object { $_.name -match $Pattern } | Select-Object -First 1
        if (-not $asset) { throw "no Windows asset in release $($rel.tag_name)" }
        $file = Join-Path $Tmp $asset.name
        Invoke-WebRequest $asset.browser_download_url -OutFile $file -Headers @{ "User-Agent" = "bounty-tools-installer" } -ErrorAction Stop
        $out = Join-Path $Tmp ($Repo -replace "/", "_")
        if (Test-Path $out) { Remove-Item $out -Recurse -Force }
        New-Item -ItemType Directory -Path $out | Out-Null
        if ($file -match '\.zip$')        { Expand-Archive -Path $file -DestinationPath $out -Force -ErrorAction Stop }
        elseif ($file -match '\.tar\.gz$') { tar -xzf $file -C $out; if ($LASTEXITCODE -ne 0) { throw "tar failed" } }
        else                               { Copy-Item $file $out }
        $exes = Get-ChildItem $out -Recurse -Filter *.exe
        if (-not $exes) { throw "archive had no .exe" }
        foreach ($e in $exes) {
            $target = if ($RenameTo -and $exes.Count -eq 1) { Join-Path $Bin "$RenameTo.exe" } else { Join-Path $Bin $e.Name }
            Copy-Item $e.FullName $target -Force -ErrorAction Stop
            Unblock-File $target
        }
        Good "$name $($rel.tag_name)"
    } catch { Bad $name $_.Exception.Message }
}

# ProjectDiscovery suite
Install-Release "projectdiscovery/subfinder"
Install-Release "projectdiscovery/httpx"
Install-Release "projectdiscovery/nuclei"
Install-Release "projectdiscovery/katana"
Install-Release "projectdiscovery/dnsx"
Install-Release "projectdiscovery/alterx"
Install-Release "projectdiscovery/interactsh" '(?i)^interactsh-client.*windows_amd64\.zip$'
# Discovery, fuzzing, crawling
Install-Release "owasp-amass/amass"
Install-Release "ffuf/ffuf"
Install-Release "lc/gau"
Install-Release "hahwul/dalfox"
Install-Release "trufflesecurity/trufflehog"
Install-Release "sensepost/gowitness" '(?i)windows-amd64\.exe$' "gowitness"
Install-Release "jqlang/jq" '(?i)^jq-windows-amd64\.exe$' "jq"

# ------------------------- 4. Go-built tools -------------------------
Step "Building Go tools"
$go = (Get-Command go -ErrorAction SilentlyContinue).Source
if (-not $go -and (Test-Path "C:\Program Files\Go\bin\go.exe")) { $go = "C:\Program Files\Go\bin\go.exe" }
if ($go) {
    $goTools = @(
        "github.com/tomnomnom/waybackurls@latest",
        "github.com/tomnomnom/assetfinder@latest",
        "github.com/tomnomnom/anew@latest",
        "github.com/tomnomnom/qsreplace@latest",
        "github.com/tomnomnom/unfurl@latest",
        "github.com/tomnomnom/gf@latest",
        "github.com/hakluke/hakrawler@latest",
        "github.com/BishopFox/jsluice/cmd/jsluice@latest"
    )
    foreach ($t in $goTools) {
        $short = ($t -split "@")[0].Split("/")[-1]
        Write-Host "   go install $short..."
        & $go install $t 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { Good $short } else { Bad $short "go install failed" }
    }
} else { Bad "go tools" "Go not found - install Go and re-run" }

# ------------------------- 5. Python tools -------------------------
Step "Python tools (venv at $PyVenv)"
$py = $null
if (Get-Command py -ErrorAction SilentlyContinue) { $py = @("py", "-3") }
elseif ((Get-Command python -ErrorAction SilentlyContinue) -and -not ((Get-Command python).Source -like "*WindowsApps*")) { $py = @("python") }
if ($py) {
    try {
        if (-not (Test-Path (Join-Path $PyVenv "Scripts\python.exe"))) {
            $pyExe = $py[0]; $pyArgs = @($py | Select-Object -Skip 1)
            & $pyExe @pyArgs -m venv $PyVenv
            if (-not (Test-Path (Join-Path $PyVenv "Scripts\python.exe"))) { throw "venv creation failed" }
        }
        $vpy = Join-Path $PyVenv "Scripts\python.exe"
        & $vpy -m pip install -q --upgrade pip 2>&1 | Out-Null
        # package name -> command it provides
        $pyTools = [ordered]@{ "sqlmap" = "sqlmap"; "arjun" = "arjun"; "uro" = "uro"; "waymore" = "waymore"; "xnLinkFinder" = "xnLinkFinder" }
        foreach ($pkg in $pyTools.Keys) {
            & $vpy -m pip install -q --upgrade $pkg 2>&1 | Out-Null
            $exe = Join-Path $PyVenv "Scripts\$($pyTools[$pkg]).exe"
            if ($LASTEXITCODE -eq 0 -and (Test-Path $exe)) {
                # tiny launcher so the tool works from any terminal without activating the venv
                Set-Content -Encoding ASCII -Path (Join-Path $Bin "$($pyTools[$pkg]).cmd") -Value "@`"$exe`" %*"
                Good $pkg
            } else { Bad $pkg "pip install failed" }
        }
    } catch { Bad "python venv" $_.Exception.Message }
} else { Bad "python tools" "Python not found (or only the Microsoft Store stub) - install Python 3.12 and re-run" }

# ------------------------- 6. Wordlists, patterns, templates -------------------------
Step "Wordlists, gf patterns, nuclei templates"
Refresh-Path
$git = (Get-Command git -ErrorAction SilentlyContinue).Source
if (-not $git -and (Test-Path "C:\Program Files\Git\cmd\git.exe")) { $git = "C:\Program Files\Git\cmd\git.exe" }
function Clone-Or-Pull($url, $dir) {
    if (Test-Path (Join-Path $dir ".git")) { & $git -C $dir pull --quiet 2>&1 | Out-Null }
    else { & $git clone --depth 1 --quiet $url $dir 2>&1 | Out-Null }
    return ($LASTEXITCODE -eq 0)
}
if ($git) {
    if (-not $SkipSecLists) {
        Write-Host "   SecLists (~1.5 GB, takes a few minutes)..."
        if (Clone-Or-Pull "https://github.com/danielmiessler/SecLists.git" (Join-Path $Lists "SecLists")) { Good "SecLists" } else { Bad "SecLists" "git clone failed" }
    }
    $gfSrc = Join-Path $Tools "Gf-Patterns"
    if (Clone-Or-Pull "https://github.com/1ndianl33t/Gf-Patterns.git" $gfSrc) {
        $gfDir = Join-Path $env:USERPROFILE ".gf"
        New-Item -ItemType Directory -Force -Path $gfDir | Out-Null
        Copy-Item (Join-Path $gfSrc "*.json") $gfDir -Force
        Good "gf patterns"
    } else { Bad "gf patterns" "git clone failed" }
} else { Bad "git clones" "Git not found - install Git for Windows and re-run" }

$nuclei = Join-Path $Bin "nuclei.exe"
if (Test-Path $nuclei) {
    $tpl = Join-Path $Tools "nuclei-templates"
    & $nuclei -update-templates -update-template-dir $tpl -silent 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Good "nuclei templates -> $tpl" } else { Bad "nuclei templates" "update failed" }
}

# ------------------------- 7. PATH + inventory for Claude -------------------------
Step "PATH and inventory"
$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
if (-not (($machinePath -split ";") -contains $Bin)) {
    [Environment]::SetEnvironmentVariable("Path", "$machinePath;$Bin", "Machine")
    Good "added $Bin to system PATH"
}
Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue

$inventory = (Get-ChildItem $Bin -File | Where-Object { $_.Extension -in ".exe", ".cmd" } |
              ForEach-Object { $_.BaseName } | Sort-Object -Unique) -join ", "
$claudeMd = Join-Path $Root "CLAUDE.md"
$block = @"
<!-- tools:start (written by install-tools.ps1 - re-run the script to refresh) -->
## Installed tools (Windows)
- Binaries on PATH from $Bin : $inventory
- Wordlists: $Lists\SecLists (e.g. Discovery\Web-Content\raft-medium-words.txt, Discovery\DNS\subdomains-top1million-20000.txt)
- nuclei templates: $Tools\nuclei-templates  (pass -t if nuclei can't find them)
- gf patterns: %USERPROFILE%\.gf
- Python tools run from $PyVenv via .cmd launchers in bin
- Burp proxy at 127.0.0.1:8080 (add -proxy / -x / --proxy flags to route tool traffic through Burp when useful)
- Shell is Git Bash: use forward slashes or quote Windows paths; prefer ``tool -o file`` then read the file over huge stdout
<!-- tools:end -->
"@
$existing = if (Test-Path $claudeMd) { Get-Content $claudeMd -Raw } else { "# Bug bounty workstation`n`n" }
if ($existing -match '(?s)<!-- tools:start.*?<!-- tools:end -->') {
    $existing = [regex]::Replace($existing, '(?s)<!-- tools:start.*?<!-- tools:end -->', $block.Trim())
} else { $existing = $existing.TrimEnd() + "`n`n" + $block.Trim() + "`n" }
Set-Content -Encoding UTF8 -Path $claudeMd -Value $existing
Good "wrote tool inventory to $claudeMd"

# ------------------------- summary -------------------------
Write-Host "`n================ SUMMARY ================" -ForegroundColor Cyan
Write-Host "Installed/updated: $($ok.Count)" -ForegroundColor Green
if ($failed.Count) {
    Write-Host "Problems: $($failed.Count)" -ForegroundColor Yellow
    $failed | ForEach-Object { Write-Host "  - $_" -ForegroundColor Yellow }
}
Write-Host "`nOpen a NEW terminal (so PATH refreshes), then try:  nuclei -version ; httpx -version ; ffuf -V"
Write-Host "Restart the claude-tg bot too so Claude picks up the new PATH:  Stop-ScheduledTask claude-tg; Start-ScheduledTask claude-tg"
