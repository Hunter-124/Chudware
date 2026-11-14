<#
.SYNOPSIS
  Stage build outputs into dist\Chudware while Chudware.exe / chudware-hostd.exe keep running.

.DESCRIPTION
  Windows refuses to overwrite a DLL or exe that a process has mapped, but it does allow
  renaming one. A locked destination is moved aside to <name>.old-<stamp> (the running
  process keeps its mapping) and the new file takes its place. hostd's stale-binary watch
  then sees the new mtime on its own exe / libchudware path and soft-drains, so live sessions
  finish on the old engine and the next request spawns the new build.
  Unchanged files (same size and timestamp) are skipped; leftover *.old-* copies are deleted
  once nothing holds them.

.EXAMPLE
  stage_swap.ps1 -Source build\windows-occt\bin -Dest dist\Chudware -Include *.dll,chudware-hostd.exe
#>
param(
    [Parameter(Mandatory)][string]$Source,
    [Parameter(Mandatory)][string]$Dest,
    [Parameter(Mandatory)][string[]]$Include
)
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $Dest)) { New-Item -ItemType Directory -Path $Dest | Out-Null }

# Sweep copies left by earlier swaps; ones still mapped by a live process stay until next time.
Get-ChildItem -LiteralPath $Dest -File -Filter '*.old-*' | ForEach-Object {
    try { Remove-Item -LiteralPath $_.FullName -Force } catch { }
}

$stamp = Get-Date -Format 'yyyyMMddHHmmssfff'
$swapped = @()
foreach ($pattern in $Include) {
    foreach ($src in Get-ChildItem -LiteralPath $Source -File -Filter $pattern) {
        $dst = Join-Path $Dest $src.Name
        $old = Get-Item -LiteralPath $dst -ErrorAction SilentlyContinue
        if ($old -and $old.Length -eq $src.Length -and $old.LastWriteTimeUtc -eq $src.LastWriteTimeUtc) {
            continue
        }
        try {
            Copy-Item -LiteralPath $src.FullName -Destination $dst -Force
        } catch [System.IO.IOException], [System.UnauthorizedAccessException] {
            Move-Item -LiteralPath $dst -Destination "$dst.old-$stamp" -Force
            Copy-Item -LiteralPath $src.FullName -Destination $dst -Force
            $swapped += $src.Name
        }
    }
}
if ($swapped.Count) {
    Write-Host "[stage] in use, swapped in beside the running copy: $($swapped -join ', ')"
    Write-Host '[stage] a running hostd hands off to the new build once its current work finishes;'
    Write-Host '[stage] restart Chudware.exe to pick up a new GUI.'
}
