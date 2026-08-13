# Chudware manifest-owned uninstaller. It intentionally does not remove user data,
# projects, documents, caches, legacy trees, or any file absent from the signed payload
# manifest. Inno Setup invokes this script before it removes its own bookkeeping files.
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\Programs\Chudware"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 3.0
$ManifestName = "payload-manifest.v1.json"
$SignatureName = "payload-manifest.v1.sig"
$ExpectedTopKeys = @("arch", "created_at", "files", "key_id", "platform", "product", "schema", "version")
$ExpectedFileKeys = @("kind", "mode", "path", "sha256", "size")
$AllowedKinds = @("executable", "library", "worker", "policy", "resource")
$UninstallRegistryKey = "Registry::HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Uninstall\{6B9E2E6B-6B7B-4B7B-9B7B-AICADSETUP01}_is1"

function Stop-Uninstall([string]$message) {
    throw "Chudware uninstall stopped: $message. No unverified path will be removed."
}

function Get-NormalizedFullPath([string]$path) {
    if ([string]::IsNullOrWhiteSpace($path)) { Stop-Uninstall "empty install path" }
    $full = [System.IO.Path]::GetFullPath($path)
    $root = [System.IO.Path]::GetPathRoot($full)
    if ([string]::IsNullOrEmpty($root) -or $full.Length -le $root.Length) {
        Stop-Uninstall "the install root cannot be a filesystem root"
    }
    return $full.TrimEnd([char[]]@('\', '/'))
}

function Assert-NoReparseComponents([string]$path) {
    $full = [System.IO.Path]::GetFullPath($path)
    $volume = [System.IO.Path]::GetPathRoot($full)
    if ([string]::IsNullOrEmpty($volume)) { Stop-Uninstall "path has no volume root" }
    $cursor = $volume
    $remainder = $full.Substring($volume.Length)
    foreach ($component in $remainder.Split([char[]]@('\', '/'), [System.StringSplitOptions]::RemoveEmptyEntries)) {
        $cursor = Join-Path $cursor $component
        $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            Stop-Uninstall "a reparse point occurs in an owned path"
        }
    }
}

function Assert-ExactKeys($object, [string[]]$expected, [string]$label) {
    if ($null -eq $object) { Stop-Uninstall "$label is null" }
    $actual = @($object.PSObject.Properties.Name | Sort-Object)
    $wanted = @($expected | Sort-Object)
    if ($actual.Count -ne $wanted.Count) { Stop-Uninstall "$label has an unexpected schema" }
    for ($i = 0; $i -lt $wanted.Count; ++$i) {
        if ($actual[$i] -cne $wanted[$i]) { Stop-Uninstall "$label has an unexpected schema" }
    }
}

function Convert-OwnedRelativePath([string]$relative) {
    if ([string]::IsNullOrEmpty($relative) -or
        $relative.IndexOf([char]0) -ge 0 -or
        $relative.Contains('\') -or
        $relative.StartsWith('/') -or
        [System.IO.Path]::IsPathRooted($relative)) {
        Stop-Uninstall "manifest contains a non-relative path"
    }
    $parts = @($relative.Split('/'))
    if ($parts.Count -eq 0) { Stop-Uninstall "manifest contains an empty path" }
    foreach ($part in $parts) {
        if ([string]::IsNullOrEmpty($part) -or $part -eq '.' -or $part -eq '..' -or
            $part.Contains(':') -or $part.EndsWith('.') -or $part.EndsWith(' ')) {
            Stop-Uninstall "manifest contains an unsafe path component"
        }
        $deviceStem = $part.Split('.')[0]
        if ($deviceStem -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$') {
            Stop-Uninstall "manifest contains a reserved Windows path component"
        }
    }
    return ($parts -join '\')
}

function Assert-UnderInstallRoot([string]$root, [string]$path) {
    $full = [System.IO.Path]::GetFullPath($path)
    $prefix = $root + [System.IO.Path]::DirectorySeparatorChar
    if (-not $full.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        Stop-Uninstall "manifest path escapes the registered install root"
    }
    return $full
}

function Assert-OwnedFileMatches($owned) {
    Assert-NoReparseComponents $owned.FullPath
    $item = Get-Item -LiteralPath $owned.FullPath -Force -ErrorAction Stop
    if ($item.PSIsContainer -or
        ($item.Attributes -band [System.IO.FileAttributes]::Device) -ne 0 -or
        ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        Stop-Uninstall "an owned file is not a plain file"
    }
    if ([int64]$item.Length -ne $owned.Size) { Stop-Uninstall "an owned file size changed" }
    $digest = (Get-FileHash -LiteralPath $owned.FullPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($digest -cne $owned.Sha256) { Stop-Uninstall "an owned file hash changed" }

}

try {
    if (-not (Test-Path -LiteralPath $UninstallRegistryKey)) {
        Stop-Uninstall "the per-user installer registration is missing"
    }
    $registered = [string](Get-ItemPropertyValue -LiteralPath $UninstallRegistryKey -Name InstallLocation)
    $registeredRoot = Get-NormalizedFullPath $registered
    $requestedRoot = Get-NormalizedFullPath $InstallDir
    if (-not $registeredRoot.Equals($requestedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        Stop-Uninstall "the requested path does not match the OS-registered install root"
    }
    Assert-NoReparseComponents $registeredRoot

    $manifestPath = Assert-UnderInstallRoot $registeredRoot (Join-Path $registeredRoot $ManifestName)
    $signaturePath = Assert-UnderInstallRoot $registeredRoot (Join-Path $registeredRoot $SignatureName)
    Assert-NoReparseComponents $manifestPath
    Assert-NoReparseComponents $signaturePath
    foreach ($metadataPath in @($manifestPath, $signaturePath)) {
        $metadata = Get-Item -LiteralPath $metadataPath -Force -ErrorAction Stop
        if ($metadata.PSIsContainer -or
            ($metadata.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            Stop-Uninstall "signed manifest metadata is not a plain file"
        }
    }

    # Signature verification belongs to the signed native binary so the release public-key
    # ring is compiled in and distinct from entitlement trust. This mode must not initialize
    # the GUI, workers, or user profile. Missing/old binaries time out or return nonzero.
    $verifier = Assert-UnderInstallRoot $registeredRoot (Join-Path $registeredRoot "chudware.exe")
    Assert-NoReparseComponents $verifier
    $verifierItem = Get-Item -LiteralPath $verifier -Force -ErrorAction Stop
    if ($verifierItem.PSIsContainer -or
        ($verifierItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        Stop-Uninstall "the native payload verifier is not a plain file"
    }
    $processInfo = New-Object System.Diagnostics.ProcessStartInfo
    $processInfo.FileName = $verifier
    $processInfo.Arguments = "--verify-payload-manifest `"$manifestPath`" `"$signaturePath`""
    $processInfo.UseShellExecute = $false
    $processInfo.CreateNoWindow = $true
    $processInfo.RedirectStandardOutput = $true
    $processInfo.RedirectStandardError = $true
    $verifyProcess = New-Object System.Diagnostics.Process
    $verifyProcess.StartInfo = $processInfo
    if (-not $verifyProcess.Start()) { Stop-Uninstall "the native payload verifier did not start" }
    if (-not $verifyProcess.WaitForExit(30000)) {
        try { $verifyProcess.Kill() } catch {}
        Stop-Uninstall "the native payload verifier timed out"
    }
    if ($verifyProcess.ExitCode -ne 0) { Stop-Uninstall "payload signature verification failed" }

    $manifestBytes = [System.IO.File]::ReadAllBytes($manifestPath)
    $manifestText = New-Object System.Text.UTF8Encoding($false, $true)
    $manifest = ConvertFrom-Json ($manifestText.GetString($manifestBytes))
    Assert-ExactKeys $manifest $ExpectedTopKeys "payload manifest"
    if ($manifest.schema -cne "chudware-payload-v1" -or $manifest.product -cne "Chudware" -or
        $manifest.platform -cne "windows" -or $manifest.arch -cne "x86_64" -or
        [string]::IsNullOrWhiteSpace([string]$manifest.key_id)) {
        Stop-Uninstall "payload manifest identity does not match this product"
    }
    if ($manifest.files -isnot [System.Array]) { Stop-Uninstall "payload manifest files is not an array" }

    $ownedFiles = New-Object System.Collections.Generic.List[object]
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    $previousPath = $null
    foreach ($row in $manifest.files) {
        Assert-ExactKeys $row $ExpectedFileKeys "payload manifest file row"
        $relative = [string]$row.path
        $nativeRelative = Convert-OwnedRelativePath $relative
        if ($relative -ceq $ManifestName -or $relative -ceq $SignatureName) {
            Stop-Uninstall "payload manifest cannot own its signed-envelope metadata"
        }
        if (-not $seen.Add($nativeRelative)) { Stop-Uninstall "payload manifest contains duplicate paths" }
        if ($null -ne $previousPath -and [string]::CompareOrdinal($previousPath, $relative) -ge 0) {
            Stop-Uninstall "payload manifest file rows are not strictly sorted"
        }
        $previousPath = $relative
        if ($AllowedKinds -cnotcontains [string]$row.kind) { Stop-Uninstall "payload manifest has an invalid file kind" }
        if ($row.size -isnot [int] -and $row.size -isnot [long]) { Stop-Uninstall "payload manifest has an invalid file size" }
        if ([int64]$row.size -lt 0) { Stop-Uninstall "payload manifest has a negative file size" }
        if ($row.mode -isnot [int] -and $row.mode -isnot [long]) { Stop-Uninstall "payload manifest has an invalid file mode" }
        if ([int64]$row.mode -lt 0 -or [int64]$row.mode -gt 4095) { Stop-Uninstall "payload manifest has an invalid file mode" }
        $sha256 = [string]$row.sha256
        if ($sha256 -cnotmatch '^[0-9a-f]{64}$') { Stop-Uninstall "payload manifest has an invalid SHA-256" }
        $fullPath = Assert-UnderInstallRoot $registeredRoot (Join-Path $registeredRoot $nativeRelative)
        $ownedFiles.Add([pscustomobject]@{
            Relative = $nativeRelative
            FullPath = $fullPath
            Size = [int64]$row.size
            Sha256 = $sha256
            Mode = [int64]$row.mode
        })
    }
    if ($ownedFiles.Count -eq 0) { Stop-Uninstall "payload manifest owns no files" }

    # Complete preflight before the first deletion. Static damage therefore cannot produce a
    # partial uninstall. Each file is checked again immediately before its literal deletion.
    foreach ($owned in $ownedFiles) { Assert-OwnedFileMatches $owned }

    $deleteOrder = @($ownedFiles | Sort-Object @{Expression = { $_.Relative.Split('\').Count }; Descending = $true}, Relative)
    $ownedDirectories = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($owned in $deleteOrder) {
        Assert-OwnedFileMatches $owned
        [System.IO.File]::Delete($owned.FullPath)
        $parent = [System.IO.Path]::GetDirectoryName($owned.FullPath)
        while ($parent -and -not $parent.Equals($registeredRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            [void]$ownedDirectories.Add($parent)
            $parent = [System.IO.Path]::GetDirectoryName($parent)
        }
    }

    # Remove only now-empty directories implied by owned paths. Directory.Delete(false)
    # neither enumerates nor recursively removes an unexpected/user-created child.
    foreach ($directory in @($ownedDirectories | Sort-Object { $_.Length } -Descending)) {
        if (-not (Test-Path -LiteralPath $directory -PathType Container)) { continue }
        Assert-NoReparseComponents $directory
        try { [System.IO.Directory]::Delete($directory, $false) } catch [System.IO.IOException] {}
    }

    # These two fixed signed-envelope files cannot list their own hashes. They are the only
    # implicit package metadata removed, and are reparse-checked again at point of deletion.
    foreach ($metadataPath in @($signaturePath, $manifestPath)) {
        Assert-NoReparseComponents $metadataPath
        [System.IO.File]::Delete($metadataPath)
    }

    Write-Host "==> Removed signed manifest-owned Chudware payload files. User data was left untouched."
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
