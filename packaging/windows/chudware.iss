; packaging/windows/chudware.iss — Inno Setup wrapper around the existing PowerShell installer
; (services/installer/install.ps1 / uninstall.ps1), which already does the real work: copy the
; payload, provision the managed runtime venv, register the MCP server, set up file
; associations. This script's only job is to give that a normal Windows installer experience
; (Start Menu entry, Programs-and-Features uninstall entry, a single .exe to hand a user).
;
; Build (on a Windows host with Inno Setup 6 installed — ISCC.exe on PATH):
;   1. python -m installer.build_release --zip          (produces dist\AI-CAD\...)
;   2. ISCC packaging\windows\chudware.iss /DPayloadDir=dist\AI-CAD /DAppVersion=1.0.0
;
; See docs/PACKAGING_WINDOWS.md for the full walkthrough (this repo's Linux box cannot run
; Inno Setup or produce a real Windows chudware.exe, so this script is documented, not verified,
; here — see that doc for exactly what to check on a Windows host).

#ifndef PayloadDir
  #define PayloadDir "..\..\dist\AI-CAD"
#endif
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

[Setup]
AppId={{6B9E2E6B-6B7B-4B7B-9B7B-AICADSETUP01}
AppName=Chudware
AppVersion={#AppVersion}
AppPublisher=Chudware
DefaultDirName={autopf}\Chudware
DefaultGroupName=Chudware
DisableProgramGroupPage=yes
OutputDir=..\..\dist
OutputBaseFilename=Chudware-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
; No EULA/license file yet (commercial closed-source per CONTRIBUTING.md — add one before a
; real external release; a bare-bones installer is fine for internal dogfooding builds).
WizardStyle=modern
; Ask Windows Restart Manager to close apps holding files under {app} (chudware.exe and any
; aicad-hostd.exe daemon) before install/uninstall. Belt-and-braces with install.ps1's own
; targeted daemon --stop step; the daemon is also staged/replaced by the payload copy below
; (the [Files] {#PayloadDir}\* glob picks up aicad-hostd.exe/.pdb automatically once
; build_release.py stages them — no extra [Files] entry needed).
CloseApplications=yes
RestartApplications=no

[Files]
; Staged to {tmp} (Inno auto-cleans after setup exits) — install.ps1 reads from $PSScriptRoot
; and copies into -InstallDir itself, so the real payload must NOT also be staged into {app}
; directly (that would make install.ps1 copy {app} onto itself).
Source: "{#PayloadDir}\*"; DestDir: "{tmp}\Chudware-payload"; Flags: recursesubdirs createallsubdirs
; uninstall.ps1 is NOT in install.ps1's own copy list (see services/installer/install.ps1) —
; stage it into {app} directly so [UninstallRun] below can always find it.
Source: "{#PayloadDir}\uninstall.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Run]
Filename: "powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{tmp}\Chudware-payload\install.ps1"" -InstallDir ""{app}"""; \
    StatusMsg: "Provisioning the Chudware runtime (this can take a few minutes)..."; \
    Flags: runhidden waituntilterminated

[UninstallRun]
Filename: "powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\uninstall.ps1"" -InstallDir ""{app}"""; \
    Flags: runhidden waituntilterminated

[Icons]
Name: "{group}\Chudware"; Filename: "{app}\chudware.exe"
Name: "{group}\Uninstall Chudware"; Filename: "{uninstallexe}"

; NOTE: install.ps1 also creates its own Start Menu shortcut (Environment.GetFolderPath
; 'Programs') — a known, harmless duplicate with the [Icons] entry above until install.ps1
; grows a -NoShortcut flag. Not fixed here (can't test the PowerShell change on this box);
; tracked in docs/PACKAGING_WINDOWS.md.
