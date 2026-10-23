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

; --- learned mesh-advisor runtime ---------------------------------------------------------
; libchudware.dll resolves its ONNX advisor model package relative to its own module, which
; is {app} in this flat layout, so the package installs as {app}\polymesh-advisor beside the
; engine and the ONNX Runtime installs beside both.
;
; The payload carries either the whole closure or none of it. A partial closure is a
; permanently dead optimizer that silently falls back on every study, so ISCC refuses to
; build an installer around one; a payload from a build with the advisor disabled carries
; neither the runtime nor the package and installs exactly as it did before.
#define AdvisorPackage "polymesh-advisor"
; ISPP's FileExists resolves a relative name against ISCC's working directory while a Source
; entry resolves it against this script's directory. Probe through an explicitly rooted copy
; of PayloadDir so the gate below can never decide "no advisor" from the wrong directory.
#if Pos(":", PayloadDir) == 2
  #define AdvisorProbeDir PayloadDir
#else
  #define AdvisorProbeDir AddBackslash(SourcePath) + PayloadDir
#endif
#define AdvisorProbePackage AdvisorProbeDir + "\" + AdvisorPackage
#if FileExists(AdvisorProbeDir + "\onnxruntime.dll") || DirExists(AdvisorProbePackage)
  #define AdvisorPayload
  #if !FileExists(AdvisorProbeDir + "\onnxruntime.dll")
    #error Staged payload has a polymesh-advisor package but no onnxruntime.dll; the advisor cannot load. Rebuild the engine so its ONNX Runtime is staged, then restage the payload.
  #endif
  #if !FileExists(AdvisorProbePackage + "\model.onnx")
    #error Staged advisor package is missing model.onnx; refusing to ship an installer whose mesh optimizer is dead.
  #endif
  #if !FileExists(AdvisorProbePackage + "\normalization.json")
    #error Staged advisor package is missing normalization.json; refusing to ship an installer whose mesh optimizer is dead.
  #endif
  #if !FileExists(AdvisorProbePackage + "\clamps.json")
    #error Staged advisor package is missing clamps.json; refusing to ship an installer whose mesh optimizer is dead.
  #endif
  #if !FileExists(AdvisorProbePackage + "\ood.json")
    #error Staged advisor package is missing ood.json; refusing to ship an installer whose mesh optimizer is dead.
  #endif
  ; Excluded from the recursive row below because the explicit rows own them, exactly like
  ; the compiled sidecars: one row per artifact, so ISCC fails closed on a missing one.
  #define AdvisorExcludes ",onnxruntime.dll," + AdvisorPackage + "," + AdvisorPackage + "\*"
#else
  #define AdvisorExcludes ""
#endif

[Setup]
AppId={{6B9E2E6B-6B7B-4B7B-9B7B-AICADSETUP01}
AppName=Chudware
AppVersion={#AppVersion}
AppPublisher=Chudware contributors
AppPublisherURL=https://chudware.com
AppSupportURL=https://github.com/Hunter-124/Chudware/issues
AppUpdatesURL=https://github.com/Hunter-124/Chudware/releases
AppCopyright=Copyright (c) 2026 Hunter Edmonds and Chudware contributors
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
LicenseFile=..\..\LICENSE
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
    Excludes: "chudware-worker.exe,chudware-mcp.exe,chud.exe,chudware-uninstall-helper.exe,payload-manifest.v1.json,payload-manifest.v1.sig,install.ps1{#AdvisorExcludes}"; \
    Flags: recursesubdirs createallsubdirs ignoreversion
; packaging/windows/build_sidecars.ps1 creates these before the manifest is hashed.
Source: "{#PayloadDir}\chudware-worker.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\chudware-mcp.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\chud.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\chudware-uninstall-helper.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\payload-manifest.v1.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\payload-manifest.v1.sig"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\install.ps1"; DestDir: "{app}"; Flags: ignoreversion
#ifdef AdvisorPayload
; Learned mesh-advisor closure: the ONNX Runtime the engine imports plus the exact four
; artifacts pm::advisor::Advisor opens. Nothing else from the model's bench directory is
; packaged — the cross-validation dumps and dashboard there are campaign evidence.
Source: "{#PayloadDir}\onnxruntime.dll"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PayloadDir}\{#AdvisorPackage}\model.onnx"; DestDir: "{app}\{#AdvisorPackage}"; Flags: ignoreversion
Source: "{#PayloadDir}\{#AdvisorPackage}\normalization.json"; DestDir: "{app}\{#AdvisorPackage}"; Flags: ignoreversion
Source: "{#PayloadDir}\{#AdvisorPackage}\clamps.json"; DestDir: "{app}\{#AdvisorPackage}"; Flags: ignoreversion
Source: "{#PayloadDir}\{#AdvisorPackage}\ood.json"; DestDir: "{app}\{#AdvisorPackage}"; Flags: ignoreversion
#endif


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
