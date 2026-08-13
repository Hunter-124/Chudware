; packaging/windows/chudware.iss — Inno Setup installs the finalized source-free payload
; byte-for-byte and invokes the signed native manifest-owned uninstaller before Inno removes
; bookkeeping. The helper binds to the registered root and deletes verified handles only.
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
#ifndef UninstallHelperSha256
  #error UninstallHelperSha256 must come from the finalized signed payload manifest
#endif

[Setup]
AppId={{6B9E2E6B-6B7B-4B7B-9B7B-AICADSETUP01}
AppName=Chudware
AppVersion={#AppVersion}
AppPublisher=Chudware
; The per-user location is deliberate: it avoids elevation/profile substitution and is the
; canonical root recorded in HKCU Programs-and-Features. The native uninstall helper requires
; this exact registered identity before it opens any manifest-owned child.
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
; Ask Windows Restart Manager to close apps holding files under {app} before uninstall.
; Release staging rejects PDBs/source, and the protected sidecars below are explicit so ISCC
; fails closed if any compiled output is missing.
CloseApplications=yes
RestartApplications=no

[Files]
; Production installation is a byte-for-byte copy of the already verified, source-free payload.
; Developer Python provisioning remains available through services/installer/install.ps1 but is
; never executed by the packaged installer. Explicit protected rows make ISCC fail closed when a
; compiled sidecar or signed manifest is absent.
Source: "{#PayloadDir}\*"; DestDir: "{app}"; \
    Excludes: "chudware-worker.exe,chudware-mcp.exe,chudware-omp-bridge.exe,chudware-uninstall-helper.exe,payload-manifest.v1.json,payload-manifest.v1.sig,install.ps1"; \
    Flags: recursesubdirs createallsubdirs ignoreversion
; packaging/windows/build_sidecars.ps1 creates these before the manifest is hashed.
Source: "{#PayloadDir}\chudware-worker.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\chudware-mcp.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\chudware-omp-bridge.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\chudware-uninstall-helper.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\payload-manifest.v1.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\payload-manifest.v1.sig"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\install.ps1"; DestDir: "{app}"; Flags: ignoreversion


[Code]
function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
  Started: Boolean;
  HelperPath: String;
  HelperCopy: String;
begin
  Result := False;
  HelperPath := ExpandConstant('{app}\chudware-uninstall-helper.exe');
  if not FileExists(HelperPath) then
  begin
    MsgBox('The signed Chudware uninstall helper is missing. Nothing was removed.', mbError, MB_OK);
    Exit;
  end;
  HelperCopy := ExpandConstant('{tmp}\chudware-uninstall-helper.exe');
  if not FileCopy(HelperPath, HelperCopy, False) then
  begin
    MsgBox('The signed Chudware uninstall helper could not be staged. Nothing was removed.', mbError, MB_OK);
    Exit;
  end;
  try
    if CompareText(GetSHA256OfFile(HelperCopy), '{#UninstallHelperSha256}') <> 0 then
    begin
      MsgBox('The signed Chudware uninstall helper was modified. Nothing was removed.', mbError, MB_OK);
      Exit;
    end;
  except
    MsgBox('The signed Chudware uninstall helper could not be verified. Nothing was removed.', mbError, MB_OK);
    Exit;
  end;
  Started := Exec(HelperCopy,
    '--uninstall ' + AddQuotes(ExpandConstant('{app}')),
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := Started and (ResultCode = 0);
  if not Result then
    MsgBox(
      'Chudware could not verify or safely remove the signed payload. No unverified path was removed.',
      mbError, MB_OK);
end;

[Icons]
Name: "{group}\Chudware"; Filename: "{app}\chudware.exe"
Name: "{group}\Uninstall Chudware"; Filename: "{uninstallexe}"
