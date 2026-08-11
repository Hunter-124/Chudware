; packaging/windows/chudware.iss — Inno Setup wrapper around the existing PowerShell installer
; (services/installer/install.ps1 / uninstall.ps1), which already does the real work: copy the
; payload, provision the managed runtime venv, register the MCP server, set up file
; associations. This script's only job is to give that a normal Windows installer experience
; (Start Menu entry, Programs-and-Features uninstall entry, a single .exe to hand a user).
;
; Build (on a Windows host with Inno Setup 6 installed):
;   python -m services.installer.build_release --out dist
; The Python command stages the payload, reads CHUDWARE_VERSION from the generated header,
; invokes ISCC with /DAppVersion, and applies the optional signing hook.
;
; See docs/reference/PACKAGING_WINDOWS.md for the full walkthrough.

#ifndef PayloadDir
  #define PayloadDir "..\..\dist\Chudware"
#endif
#ifndef AppVersion
  #error AppVersion must be supplied by services.installer.build_release from chudware_version.h
#endif
#ifndef InstallerOutputDir
  #define InstallerOutputDir "..\..\dist"
#endif

[Setup]
AppId={{6B9E2E6B-6B7B-4B7B-9B7B-AICADSETUP01}
AppName=Chudware
AppVersion={#AppVersion}
AppPublisher=Chudware
; MUST stay byte-identical to install.ps1's -InstallDir default. Instance identity hashes
; the realpath of chudware-hostd.exe (services/ipc/ipc_discovery.cpp instanceKey), so two
; installs at two different directories are two independent daemons with two tray icons —
; the disagreement between this script and install.ps1 reproduced that bug by construction.
; Per-user wins over {autopf}: the whole product is already per-user (install.ps1 provisions
; the runtime venv into %APPDATA%\Chudware, the daemon is spawn-on-demand per logon, the MCP
; registration and file associations are HKCU), so Program Files bought nothing but a UAC
; prompt. PrivilegesRequired=lowest is REQUIRED here: without it Inno elevates and
; {localappdata} would expand to the ADMIN's profile, not the installing user's.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\Chudware
DefaultGroupName=Chudware
DisableProgramGroupPage=yes
OutputDir={#InstallerOutputDir}
OutputBaseFilename=Chudware-Setup-{#AppVersion}-x86_64
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\..\LICENSE-EULA.txt
WizardStyle=modern
; Ask Windows Restart Manager to close apps holding files under {app} (chudware.exe and any
; chudware-hostd.exe daemon) before install/uninstall. Belt-and-braces with install.ps1's own
; targeted daemon --stop step; the daemon is also staged/replaced by the payload copy below
; (the [Files] {#PayloadDir}\* glob picks up chudware-hostd.exe/.pdb automatically once
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
; grows a -NoShortcut flag. Tracked in docs/reference/PACKAGING_WINDOWS.md.
