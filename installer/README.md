# MD QC Agent: the Windows setup program

`MDQC-Setup-<version>.exe` installs MDQC on an instrument PC with no Python, no
PowerShell and no internet, so it can be carried on a USB stick. CI builds it on
every run and attaches it to each GitHub release.

## What it contains

- **python.org's embeddable Python**, the official build signed by the Python
  Software Foundation, with MDQC and its dependencies installed into it. It's a
  private copy: it doesn't touch any Python already on the PC, and nothing else
  uses it. This replaced the earlier PyInstaller executable, whose self-extracting
  loader antivirus products often flag.
- **Shortcuts:** a Start-menu folder, *Mass Dynamics QC*, with MD QC Dashboard,
  MD QC Agent (with the app id Windows needs for notifications) and MD QC
  Diagnostics.
- **Startup:** an entry that starts the agent and tray icon at login.
- **An uninstaller**, listed in *Settings > Apps*.

## What it does

| Step | |
|---|---|
| First page | Install for me only (default, no admin rights) or for all users (admin) |
| Skyline | Warns if Skyline isn't found; installing can continue |
| Upgrade | Runs `mdqc stop` first, so the agent finishes the file in hand |
| After install | Writes an empty `config.toml` if there is none, starts the agent and opens the dashboard |
| Uninstall | Stops the agent and removes the program, shortcuts and startup entry. Configuration and results in `C:\ProgramData\MassDynamics\QC` are kept |

Silent install for IT: `MDQC-Setup-1.2.1.exe /VERYSILENT /CURRENTUSER` (or
`/ALLUSERS`). It starts the agent without opening a browser.

## Building it

Needs Python 3.11+ and [Inno Setup 6](https://jrsoftware.org/isinfo.php).

```powershell
pip install ".[build]"
python scripts/build_installer.py
```

The script builds the wheel, downloads the embeddable Python into
`build\cache`, installs everything into it from ready-built Windows packages
only, smoke-tests it, and compiles `installer\mdqc.iss` into `dist\`.
`--stage-only` stops before Inno Setup. The Python download is pinned by its
SHA-256 in the script; change `PYTHON_VERSION` and `PYTHON_SHA256` together.

## Signing

The setup program is unsigned until the CI signing step is switched on. Unsigned,
Windows SmartScreen shows "Windows protected your PC" (*More info > Run anyway*).

Signing uses Microsoft's **Azure Artifact Signing** (formerly Trusted Signing).
Once the company's identity validation and certificate profile are in place:

1. In Azure, create an app registration for the GitHub build. Give it the
   **Artifact Signing Certificate Profile Signer** role on the signing account,
   and nothing else, then create a client secret for it.
2. In GitHub, go to *md-qc > Settings > Secrets and variables > Actions* and add:
   - Secrets: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`
   - Variables: `SIGNING_ENDPOINT` (e.g. `https://eus.codesigning.azure.net/`),
     `SIGNING_ACCOUNT`, `SIGNING_PROFILE`
3. The next build signs the setup program and timestamps it.

The uninstaller inside it is signed only when signing runs locally through
Inno Setup (`build_installer.py --sign "<signtool command> $f"`).
