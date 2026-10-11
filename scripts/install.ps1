<#
.SYNOPSIS
    Install or upgrade the MD QC Agent in one step.

.DESCRIPTION
    Finds a Python 3.11+ interpreter, creates (or reuses) a virtual
    environment, installs a pinned release, checks the installed version, and
    runs `mdqc doctor`. With -Service it also registers the agent as a Windows
    service through NSSM, using the same settings as the Inno Setup installer.

    Safe to re-run: an existing environment is upgraded in place, and an
    existing config.toml is never touched.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1 -Version 1.1.0 -Service -NssmPath C:\Tools\nssm.exe
#>
[CmdletBinding()]
param(
    # Release tag to install. Pinned, so every site installs the same build.
    [string]$Version = "1.1.0",
    # Where the virtual environment lives.
    [string]$InstallDir = "C:\MassDynamics\mdqc",
    # Install from a local checkout or another URL instead of the release
    # tarball - for testing a build before it is tagged.
    [string]$Source = "",
    # Register and start the Windows service (needs NSSM and an elevated shell).
    [switch]$Service,
    [string]$NssmPath = "nssm.exe"
)

$ErrorActionPreference = "Stop"
$ServiceName = "MassDynamicsQC"
$DataDir = Join-Path $env:ProgramData "MassDynamics\QC"

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "`nINSTALL FAILED: $text" -ForegroundColor Red; exit 1 }

# 1. A Python the agent supports. `py -0p` lists every installed interpreter;
#    take the newest 3.11+ rather than whatever `python` resolves to.
Step "Finding Python 3.11 or newer"
$python = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $candidates = & py -0p 2>$null | ForEach-Object {
        if ($_ -match '-V:(3)\.(\d+)\S*\s+\*?\s*(.+python\.exe)\s*$') {
            [pscustomobject]@{ Minor = [int]$Matches[2]; Path = $Matches[3].Trim() }
        }
    } | Where-Object { $_.Minor -ge 11 } | Sort-Object Minor -Descending
    if ($candidates) { $python = @($candidates)[0].Path }
}
if (-not $python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        $ver = & $cmd.Source -c "import sys; print('%d.%d' % sys.version_info[:2])"
        if ([version]$ver -ge [version]"3.11") { $python = $cmd.Source }
    }
}
if (-not $python) {
    Fail "No Python 3.11 or newer found. Install it from https://www.python.org/downloads (tick 'Add Python to PATH') and run this again."
}
Write-Host "    $python"

# 2. The environment. Calling its pip and mdqc by full path means it never has
#    to be activated, so no execution-policy change is needed afterwards.
$venvPython = Join-Path $InstallDir "Scripts\python.exe"
$mdqc = Join-Path $InstallDir "Scripts\mdqc.exe"
if (Test-Path $venvPython) {
    Step "Upgrading the existing environment at $InstallDir"
} else {
    Step "Creating the environment at $InstallDir"
    & $python -m venv $InstallDir
    if ($LASTEXITCODE -ne 0) { Fail "could not create a virtual environment at $InstallDir" }
}

# 3. The release.
$target = if ($Source) { $Source } else {
    "https://github.com/MassDynamics/md-qc/archive/refs/tags/v$Version.tar.gz"
}
Step "Installing $target"
& $venvPython -m pip install --quiet --disable-pip-version-check --upgrade $target
if ($LASTEXITCODE -ne 0) { Fail "pip could not install $target" }

# 4. Prove it is the version asked for, not a leftover.
Step "Checking the installed version"
$installed = (& $mdqc version) -replace '^mdqc\s+', ''
Write-Host "    mdqc $installed"
if (-not $Source -and $installed -ne $Version) {
    Fail "expected $Version but the environment reports $installed"
}

# 5. The data folder. config.toml is left alone if it exists.
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$configPath = Join-Path $DataDir "config.toml"
if (Test-Path $configPath) {
    Write-Host "    Existing configuration kept: $configPath"
}

# 6. Health check. Reported, not fatal: on a fresh PC Skyline or the
#    configuration may legitimately not be in place yet.
Step "Running mdqc doctor"
& $mdqc doctor
if ($LASTEXITCODE -ne 0) {
    Write-Host "    doctor reported problems - see above. The install itself succeeded." -ForegroundColor Yellow
}

# 7. Optional service, mirroring installer/mdqc.iss.
if ($Service) {
    Step "Registering the $ServiceName service"
    if (-not (Get-Command $NssmPath -ErrorAction SilentlyContinue)) {
        Fail "NSSM not found at '$NssmPath'. Pass -NssmPath, or install without -Service and start the agent by hand."
    }
    $existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($existing) {
        & $NssmPath stop $ServiceName | Out-Null
        & $NssmPath set $ServiceName Application $mdqc | Out-Null
    } else {
        & $NssmPath install $ServiceName $mdqc run --service-mode | Out-Null
    }
    $logs = Join-Path $DataDir "logs"
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    & $NssmPath set $ServiceName AppParameters "run --service-mode" | Out-Null
    & $NssmPath set $ServiceName AppDirectory $InstallDir | Out-Null
    & $NssmPath set $ServiceName DisplayName "Mass Dynamics QC Agent (Python)" | Out-Null
    & $NssmPath set $ServiceName Start SERVICE_DELAYED_AUTO_START | Out-Null
    & $NssmPath set $ServiceName AppExit Default Restart | Out-Null
    & $NssmPath set $ServiceName AppRestartDelay 5000 | Out-Null
    & $NssmPath set $ServiceName AppThrottle 30000 | Out-Null
    & $NssmPath set $ServiceName AppStdout (Join-Path $logs "service-stdout.log") | Out-Null
    & $NssmPath set $ServiceName AppStderr (Join-Path $logs "service-stderr.log") | Out-Null
    & $NssmPath start $ServiceName | Out-Null
    Write-Host "    $ServiceName started"
}

Write-Host "`nInstalled mdqc $installed in $InstallDir." -ForegroundColor Green
if (-not $Service) {
    Write-Host "Start it with:  $mdqc run --foreground"
}
if (-not (Test-Path $configPath)) {
    Write-Host "No configuration yet: start the agent and open the web UI it prints to set up instruments and the cloud token."
}
