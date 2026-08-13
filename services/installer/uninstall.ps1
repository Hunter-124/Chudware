# Developer convenience wrapper for the OS-registered Chudware uninstaller.
#
# The packaged uninstall trust path is the Inno uninstaller plus the signed native
# chudware-uninstall-helper.exe. This wrapper performs no deletion itself: it validates the
# registered install identity and launches that OS-owned entry point. User data, projects,
# documents, caches, and legacy trees are never inspected or removed here.
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\Programs\Chudware"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 3.0
$UninstallRegistryKey = "Registry::HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Uninstall\{6B9E2E6B-6B7B-4B7B-9B7B-AICADSETUP01}_is1"

function Stop-Uninstall([string]$message) {
    throw "Chudware uninstall stopped: $message. Nothing was removed."
}

function Get-CanonicalAbsolutePath([string]$path) {
    if ([string]::IsNullOrWhiteSpace($path) -or -not [System.IO.Path]::IsPathRooted($path)) {
        Stop-Uninstall "path is empty or not absolute"
    }
    $full = [System.IO.Path]::GetFullPath($path).TrimEnd([char[]]@('\', '/'))
    $volume = [System.IO.Path]::GetPathRoot($full)
    if ([string]::IsNullOrEmpty($volume) -or $full.Length -le $volume.Length) {
        Stop-Uninstall "path resolves to a filesystem root"
    }
    return $full
}

function Assert-NoReparseComponents([string]$path) {
    $full = Get-CanonicalAbsolutePath $path
    $volume = [System.IO.Path]::GetPathRoot($full)
    $cursor = $volume
    foreach ($component in $full.Substring($volume.Length).Split(
            [char[]]@('\', '/'), [System.StringSplitOptions]::RemoveEmptyEntries)) {
        $cursor = Join-Path $cursor $component
        $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            Stop-Uninstall "a reparse point occurs in the registered uninstall path"
        }
    }
}

try {
    if (-not (Test-Path -LiteralPath $UninstallRegistryKey)) {
        Stop-Uninstall "the per-user installer registration is missing"
    }
    $registration = Get-ItemProperty -LiteralPath $UninstallRegistryKey -ErrorAction Stop
    $registeredRoot = Get-CanonicalAbsolutePath ([string]$registration.InstallLocation)
    $requestedRoot = Get-CanonicalAbsolutePath $InstallDir
    if (-not $registeredRoot.Equals($requestedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        Stop-Uninstall "the requested path does not match the OS-registered install root"
    }
    Assert-NoReparseComponents $registeredRoot

    # Inno writes a quoted absolute executable path. Refuse shell syntax and optional command
    # text rather than evaluating a mutable registry string. The uninstaller needs no args.
    $command = [string]$registration.UninstallString
    if ($command -cnotmatch '^\s*"([^\"]+)"\s*$') {
        Stop-Uninstall "the OS uninstall command has an unexpected form"
    }
    $uninstaller = Get-CanonicalAbsolutePath $Matches[1]
    if (-not ([System.IO.Path]::GetDirectoryName($uninstaller)).Equals(
            $registeredRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        [System.IO.Path]::GetFileName($uninstaller) -cnotmatch '^(?i:unins[0-9]+\.exe)$') {
        Stop-Uninstall "the OS uninstall executable is outside the registered install root"
    }
    Assert-NoReparseComponents $uninstaller
    $item = Get-Item -LiteralPath $uninstaller -Force -ErrorAction Stop
    if ($item.PSIsContainer -or
        ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        Stop-Uninstall "the OS uninstall executable is not a plain file"
    }

    $process = Start-Process -FilePath $uninstaller -Wait -PassThru
    exit $process.ExitCode
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
