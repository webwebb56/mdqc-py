<#
.SYNOPSIS
    Install, upgrade or remove the MD QC Agent in one step.

.DESCRIPTION
    Finds a Python 3.11+ interpreter, creates (or reuses) a virtual
    environment, installs a pinned release with the tray icon, checks the
    installed version, and runs `mdqc doctor`. It then makes the agent start by
    itself, with no window to keep open:

      - By default the agent and its tray icon start when this user logs in.
        No administrator rights are needed.
      - With -Service the agent runs as a Windows service instead, which starts
        with Windows before anyone logs in. That needs an elevated PowerShell
        and NSSM. The tray icon still starts at login.

    It adds a "Mass Dynamics QC" Start-menu folder (MD QC Dashboard, MD QC
    Agent, MD QC Diagnostics) and starts everything straight away.

    Safe to re-run: a running agent is stopped first, the environment is
    upgraded in place, and an existing config.toml is never touched.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1 -Service -NssmPath C:\Tools\nssm.exe
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    # Release tag to install. Pinned, so every site installs the same build.
    [string]$Version = "1.2.0",
    # Where the virtual environment lives.
    [string]$InstallDir = "C:\MassDynamics\mdqc",
    # Install from a local checkout or another URL instead of the release
    # tarball - for testing a build before it is tagged.
    [string]$Source = "",
    # Run the agent as a Windows service (needs NSSM and an elevated shell).
    [switch]$Service,
    [string]$NssmPath = "nssm.exe",
    # A specific python.exe to build the environment with, instead of the
    # newest 3.11+ the py launcher knows about.
    [string]$Python = "",
    # Install and register, but don't start anything now.
    [switch]$NoStart,
    # Stop the agent and remove the environment, startup entries, shortcuts
    # and service. The data folder (configuration, results) is kept.
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$ServiceName = "MassDynamicsQC"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$RunValue = "MassDynamicsQC"
# Written by the Inno Setup installer; replaced by $RunValue here.
$LegacyTrayRunValue = "MassDynamicsQCTray"
$Aumid = "MassDynamics.QCAgent"
$AumidKey = "HKCU:\Software\Classes\AppUserModelId\$Aumid"
$MenuDir = Join-Path ([Environment]::GetFolderPath("Programs")) "Mass Dynamics QC"
$DataDir = if ($env:MDQC_DATA_DIR) { $env:MDQC_DATA_DIR } else { Join-Path $env:ProgramData "MassDynamics\QC" }

$venvPython = Join-Path $InstallDir "Scripts\python.exe"
$venvPythonw = Join-Path $InstallDir "Scripts\pythonw.exe"
$mdqc = Join-Path $InstallDir "Scripts\mdqc.exe"

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "`nINSTALL FAILED: $text" -ForegroundColor Red; exit 1 }
function IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function FindNssm {
    $cmd = Get-Command $NssmPath -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $beside = Join-Path $InstallDir "nssm.exe"
    if (Test-Path $beside) { return $beside }
    return $null
}

# Processes of this installation: the agent and the tray, however they were
# started - `mdqc run` in a console, `python -m mdqc`, or pythonw at login.
function AgentProcesses {
    $dir = [regex]::Escape($InstallDir.TrimEnd('\'))
    Get-CimInstance Win32_Process -Filter "Name like 'python%' or Name = 'mdqc.exe'" |
        Where-Object {
            $_.CommandLine -match '(-m\s+mdqc|mdqc(\.exe)?"?)\s+(run|tray)\b' -and
            ($_.CommandLine -match $dir -or $_.ExecutablePath -match $dir -or
             (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.ParentProcessId)" -ErrorAction SilentlyContinue |
                Where-Object { $_.ExecutablePath -match $dir }))
        }
}

# Stop whatever is running from this installation, so pip can replace its
# files. The agent is asked to stop first (it finishes the file in hand); a
# process that hasn't gone after 60 s is ended.
function StopAgent {
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -ne "Stopped") {
        if (-not (IsAdmin)) { Fail "the $ServiceName service is running. Run this again from an elevated PowerShell." }
        Write-Host "    Stopping the $ServiceName service"
        Stop-Service -Name $ServiceName -Force
    }
    $runtime = Join-Path $DataDir "runtime.json"
    if (Test-Path $runtime) {
        try {
            $info = Get-Content $runtime -Raw | ConvertFrom-Json
            $base = "http://127.0.0.1:$($info.port)"
            try {
                Invoke-RestMethod -Method Post -TimeoutSec 5 -Uri "$base/api/shutdown?token=$($info.token)" | Out-Null
            } catch {
                # Agents before 1.2 have no /api/shutdown; their restart
                # endpoint just stops them when nothing supervises them.
                Invoke-RestMethod -Method Post -TimeoutSec 5 -Uri "$base/api/restart?token=$($info.token)" | Out-Null
            }
            Write-Host "    Asked the running agent to stop"
        } catch { }
    }
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        $agents = @(AgentProcesses | Where-Object { $_.CommandLine -match '\brun\b' })
        if (-not $agents) { break }
        Start-Sleep -Milliseconds 500
    }
    foreach ($p in @(AgentProcesses)) {
        Write-Host "    Ending process $($p.ProcessId)"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function RemoveStartup {
    foreach ($v in $RunValue, $LegacyTrayRunValue) {
        Remove-ItemProperty -Path $RunKey -Name $v -ErrorAction SilentlyContinue
    }
    if (Test-Path $MenuDir) { Remove-Item -Recurse -Force $MenuDir }
    if (Test-Path $AumidKey) { Remove-Item -Recurse -Force $AumidKey }
}

function RemoveService {
    if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) { return }
    if (-not (IsAdmin)) { Fail "removing the $ServiceName service needs an elevated PowerShell." }
    $nssm = FindNssm
    if ($nssm) { & $nssm remove $ServiceName confirm | Out-Null } else { & sc.exe delete $ServiceName | Out-Null }
    Write-Host "    Removed the $ServiceName service"
}

# The web UI address once the agent answers, else nothing. Native stderr is
# discarded with the preference relaxed: under "Stop", Windows PowerShell 5.1
# turns any stderr line from an exe into a terminating error.
function DashboardUrl {
    $ErrorActionPreference = "Continue"
    $out = & $venvPython -m mdqc open --print-url 2>$null
    if ($LASTEXITCODE -eq 0) { return $out }
    return $null
}

# Run an exe and pass its output on as plain text. Its stderr (doctor's
# warnings, say) would otherwise become a red NativeCommandError under
# "Stop" in Windows PowerShell 5.1. $LASTEXITCODE is still set for the caller.
function Native($exe) {
    $ErrorActionPreference = "Continue"
    $rest = @($args)
    & $exe @rest 2>&1 | ForEach-Object { "$_" }
}

function Shortcut($name, $target, $arguments, $description) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut((Join-Path $MenuDir "$name.lnk"))
    $lnk.TargetPath = $target
    $lnk.Arguments = $arguments
    $lnk.WorkingDirectory = $InstallDir
    $lnk.Description = $description
    if (Test-Path $shortcutIcon) { $lnk.IconLocation = "$shortcutIcon,0" }
    $lnk.Save()
}

# Windows shows notifications from a desktop app only if a Start-menu shortcut
# carries the app id the app sends them under (System.AppUserModel.ID). The
# Inno Setup installer sets it the same way; WScript.Shell can't, hence this.
function SetShortcutAppId($lnk, $id) {
    if (-not ("MdqcShortcutId" -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class MdqcShortcutId {
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore {
        int GetCount(out uint c);
        int GetAt(uint i, out PropertyKey k);
        int GetValue(ref PropertyKey k, out PropVariant v);
        int SetValue(ref PropertyKey k, ref PropVariant v);
        int Commit();
    }
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    public struct PropertyKey { public Guid fmtid; public uint pid; }
    [StructLayout(LayoutKind.Explicit)]
    public struct PropVariant {
        [FieldOffset(0)] public ushort vt;
        [FieldOffset(8)] public IntPtr p;
        [FieldOffset(16)] public IntPtr pad;
    }
    [DllImport("shell32.dll", PreserveSig = false)]
    static extern void SHGetPropertyStoreFromParsingName(
        [MarshalAs(UnmanagedType.LPWStr)] string path, IntPtr bc, int flags,
        ref Guid riid, [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);

    public static void Set(string lnk, string id) {
        Guid iid = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");
        IPropertyStore store;
        SHGetPropertyStoreFromParsingName(lnk, IntPtr.Zero, 2, ref iid, out store);
        PropertyKey key = new PropertyKey();
        key.fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
        key.pid = 5;
        PropVariant v = new PropVariant();
        v.vt = 31;
        v.p = Marshal.StringToCoTaskMemUni(id);
        try {
            Marshal.ThrowExceptionForHR(store.SetValue(ref key, ref v));
            Marshal.ThrowExceptionForHR(store.Commit());
        } finally {
            Marshal.FreeCoTaskMem(v.p);
            Marshal.ReleaseComObject(store);
        }
    }
}
"@
    }
    [MdqcShortcutId]::Set($lnk, $id)
}

# --- Uninstall ---------------------------------------------------------------
if ($Uninstall) {
    Step "Removing the MD QC Agent from $InstallDir"
    StopAgent
    RemoveService
    RemoveStartup
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
    Write-Host "`nRemoved. Configuration and results are kept in $DataDir." -ForegroundColor Green
    exit 0
}

if ($Service -and -not (IsAdmin)) {
    Fail "-Service needs an elevated PowerShell (Run as administrator)."
}

# 1. A Python the agent supports. `py -0p` lists every installed interpreter;
#    take the newest 3.11+ rather than whatever `python` resolves to.
Step "Finding Python 3.11 or newer"
$python = $null
if ($Python) {
    if (-not (Test-Path $Python)) { Fail "no Python at $Python" }
    $python = (Resolve-Path $Python).Path
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $ErrorActionPreference = "Continue"
    $candidates = & py -0p 2>$null | ForEach-Object {
        if ($_ -match '-V:(3)\.(\d+)\S*\s+\*?\s*(.+python\.exe)\s*$') {
            [pscustomobject]@{ Minor = [int]$Matches[2]; Path = $Matches[3].Trim() }
        }
    } | Where-Object { $_.Minor -ge 11 } | Sort-Object Minor -Descending
    if ($candidates) { $python = @($candidates)[0].Path }
    $ErrorActionPreference = "Stop"
}
if (-not $python -and -not $Python) {
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

# 2. Stop a running agent: Windows won't let pip replace files in use.
Step "Stopping the agent if it is running"
StopAgent

# 3. The environment. Calling its pip and mdqc by full path means it never has
#    to be activated, so no execution-policy change is needed afterwards.
if (Test-Path $venvPython) {
    Step "Upgrading the existing environment at $InstallDir"
} else {
    Step "Creating the environment at $InstallDir"
    & $python -m venv $InstallDir
    if ($LASTEXITCODE -ne 0) { Fail "could not create a virtual environment at $InstallDir" }
}

# 4. The release, with the tray icon's packages.
$target = if (-not $Source) {
    "mdqc[tray] @ https://github.com/MassDynamics/md-qc/archive/refs/tags/v$Version.tar.gz"
} elseif (Test-Path $Source) {
    "$((Resolve-Path $Source).Path)[tray]"
} else {
    "mdqc[tray] @ $Source"
}
Step "Installing $target"
& $venvPython -m pip install --quiet --disable-pip-version-check --upgrade $target
if ($LASTEXITCODE -ne 0) { Fail "pip could not install $target" }

# 5. Prove it is the version asked for, not a leftover.
Step "Checking the installed version"
$installed = (Native $mdqc version | Select-Object -Last 1) -replace '^mdqc\s+', ''
Write-Host "    mdqc $installed"
if (-not $Source -and $installed -ne $Version) {
    Fail "expected $Version but the environment reports $installed"
}
Native $mdqc selfcheck
if ($LASTEXITCODE -ne 0) { Fail "the installed agent is missing modules it needs (see above)" }

# 6. The data folder. config.toml is left alone if it exists.
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$configPath = Join-Path $DataDir "config.toml"
$starter = $null
if (Test-Path $configPath) {
    Write-Host "    Existing configuration kept: $configPath"
} else {
    # The agent won't start without a config file, and it has to be running
    # to reach Settings - so a fresh PC gets an empty one to start from.
    $starter = "# MD QC Agent configuration, created by install.ps1.`r`n" +
        "# Set up instruments and the cloud token in the dashboard's Settings page,`r`n" +
        "# or by hand: see docs/INSTALL.md section 4b.`r`n"
    [IO.File]::WriteAllText($configPath, $starter, (New-Object Text.ASCIIEncoding))
    Write-Host "    Created an empty configuration: $configPath"
}

# 7. Health check. Reported, not fatal: on a fresh PC Skyline or the
#    configuration may legitimately not be in place yet.
Step "Running mdqc doctor"
Native $mdqc doctor
if ($LASTEXITCODE -ne 0) {
    Write-Host "    doctor reported problems - see above. The install itself succeeded." -ForegroundColor Yellow
}

# 8. How the agent runs: as a service, or at this user's login.
if ($Service) {
    Step "Registering the $ServiceName service"
    $nssm = FindNssm
    if (-not $nssm) {
        Fail "NSSM not found at '$NssmPath'. Pass -NssmPath, or install without -Service."
    }
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        & $nssm set $ServiceName Application $mdqc | Out-Null
    } else {
        & $nssm install $ServiceName $mdqc run --service-mode | Out-Null
    }
    $logs = Join-Path $DataDir "logs"
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    & $nssm set $ServiceName AppParameters "run --service-mode" | Out-Null
    & $nssm set $ServiceName AppDirectory $InstallDir | Out-Null
    & $nssm set $ServiceName DisplayName "Mass Dynamics QC Agent (Python)" | Out-Null
    & $nssm set $ServiceName Description "MD QC Agent: automated quality control monitoring for mass spectrometry instruments" | Out-Null
    & $nssm set $ServiceName Start SERVICE_DELAYED_AUTO_START | Out-Null
    & $nssm set $ServiceName AppExit Default Restart | Out-Null
    & $nssm set $ServiceName AppRestartDelay 5000 | Out-Null
    & $nssm set $ServiceName AppThrottle 30000 | Out-Null
    & $nssm set $ServiceName AppStdout (Join-Path $logs "service-stdout.log") | Out-Null
    & $nssm set $ServiceName AppStderr (Join-Path $logs "service-stderr.log") | Out-Null
} elseif (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    # Installed as a service earlier: keep it that way rather than start a
    # second agent at login. -Uninstall, then install again, to switch.
    Write-Host "    The $ServiceName service is installed; the agent keeps running as the service." -ForegroundColor Yellow
}

# 9. Start at login (the agent too, unless the service runs it), Start-menu
#    shortcuts, and the app id Windows needs to show the agent's notifications.
Step "Adding startup entry and Start-menu shortcuts"
RemoveStartup
# A fresh Windows profile may not have the Run key yet.
if (-not (Test-Path $RunKey)) { New-Item -Path $RunKey -Force | Out-Null }
New-ItemProperty -Path $RunKey -Name $RunValue -Value "`"$venvPythonw`" -m mdqc start" -PropertyType String -Force | Out-Null
New-Item -ItemType Directory -Force -Path $MenuDir | Out-Null
# Shortcuts need an .ico; the package ships a PNG, and Pillow (a tray
# dependency) converts it. Without it the shortcuts get a generic icon.
$shortcutIcon = Join-Path $InstallDir "mdqc.ico"
Native $venvPython -c "from PIL import Image; from mdqc.config.paths import bundled_assets_dir as d; Image.open(d() / 'icon.png').save(r'$shortcutIcon', sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])" | Out-Null
Shortcut "MD QC Dashboard" $venvPythonw "-m mdqc open" "Open the MD QC dashboard"
Shortcut "MD QC Agent" $venvPythonw "-m mdqc start" "Start the MD QC agent and tray icon"
Shortcut "MD QC Diagnostics" "powershell.exe" "-NoExit -NoProfile -Command `"& '$mdqc' doctor`"" "Check the MD QC installation"
try {
    SetShortcutAppId (Join-Path $MenuDir "MD QC Agent.lnk") $Aumid
} catch {
    Write-Host "    Could not register for notifications ($($_.Exception.Message)); the agent works without them." -ForegroundColor Yellow
}
Write-Host "    Starts at login for $env:USERNAME; Start menu: $MenuDir"

# 10. Start it now.
if (-not $NoStart) {
    Step "Starting the agent"
    if ($Service) { Start-Service -Name $ServiceName }
    elseif (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        if (IsAdmin) { Start-Service -Name $ServiceName }
        else { Write-Host "    Start the $ServiceName service from an elevated shell, or restart the PC." -ForegroundColor Yellow }
    }
    Native $venvPython -m mdqc start
    if ($LASTEXITCODE -eq 3) {
        Fail "another MD QC agent is already running on this PC, from a different installation or data folder. Stop it (or uninstall that copy) and run this again."
    }
    $deadline = (Get-Date).AddSeconds(45)
    $url = $null
    while (-not $url -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 1
        $url = DashboardUrl
    }
    if (-not $url) { Fail "the agent did not start within 45 s. Check the log in $(Join-Path $DataDir 'logs')." }
    Write-Host "    Agent is answering"
}

Write-Host "`nInstalled mdqc $installed in $InstallDir." -ForegroundColor Green
Write-Host "Open the dashboard from the tray icon, or Start menu > Mass Dynamics QC > MD QC Dashboard."
if ($starter) {
    Write-Host "Next: open the dashboard and go to Settings to set up instruments and the cloud token."
}
