<#
.SYNOPSIS
  Replace any git-tracked symlinks with directory junctions on a Windows checkout.

.DESCRIPTION
  The repository currently tracks no symlinks (mode 120000): Python packages and fixture
  corpora are referenced by their canonical services/... and corpora/... paths, so a default
  Windows checkout needs no junction setup and this script reports "nothing to do".
  CI and scripts/build_windows.ps1 still run it as a guard: should a tracked symlink ever be
  reintroduced, a checkout with core.symlinks=false would materialize it as a small TEXT
  FILE rather than a directory, breaking imports and data paths that traverse it.

  This script reads every mode-120000 entry from the index and replaces each with a real
  NTFS directory JUNCTION — no admin / Developer Mode required. Each path is
  marked skip-worktree so the junction doesn't show up as a git change.

  Idempotent: re-running only fixes entries that aren't already correct junctions.

.NOTES
  To undo:  git update-index --no-skip-worktree <name>; git checkout -- <name>
  See docs/reference/BUILD_WINDOWS.md ("Checkout portability"). Alternative:
  `git config core.symlinks true` + Windows Developer Mode, then re-clone.
#>
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Push-Location -LiteralPath $repo
$previousOutputEncoding = [Console]::OutputEncoding
try {
    # Git emits UTF-8 path names when core.quotePath is disabled.
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
    # Read the git-tracked symlinks (mode 120000) and their targets straight from the index,
    # so this stays correct if the set of compat links ever changes.
    $links = @{}
    # A git that cannot read the repository (e.g. "dubious ownership") prints nothing on
    # stdout; without this check that read as "no symlinks" and left the text stubs in place.
    $entries = @(git -c core.quotePath=false ls-files -s)
    if ($LASTEXITCODE -ne 0) { throw "git ls-files -s failed (exit $LASTEXITCODE) in $repo" }
    foreach ($line in $entries) {
        $parts = $line -split '\s+', 4
        if ($parts[0] -eq '120000') {
            $name = $parts[3]
            $targetText = @(git cat-file -p ":$name")
            if ($LASTEXITCODE -ne 0) { throw "git cat-file failed (exit $LASTEXITCODE) for $name" }
            $target = ($targetText -join "`n").Trim()
            $links[$name] = $target
        }
    }
    if ($links.Count -eq 0) { Write-Host 'No git symlinks found — nothing to do.'; return }

    foreach ($name in $links.Keys) {
        $target = $links[$name]
        $linkPath = Join-Path $repo $name
        $targetAbs = Resolve-Path -LiteralPath (Join-Path (Split-Path $linkPath -Parent) $target)
        $targetPath = $targetAbs.Path
        if (-not (Test-Path -LiteralPath $targetPath -PathType Container)) {
            throw "Directory junction target is not a directory: $name -> $target"
        }

        # Already a junction/dir pointing at the right place? leave it.
        $item = Get-Item -LiteralPath $linkPath -Force -ErrorAction SilentlyContinue
        if ($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) `
                   -and ($item.Target -and ((Resolve-Path -LiteralPath $item.Target).Path -eq $targetPath))) {
            Write-Host "ok   $name -> $target (already a junction)"
            continue
        }

        git update-index --skip-worktree -- $name
        if ($LASTEXITCODE -ne 0) { throw "git update-index failed (exit $LASTEXITCODE) for $name" }
        if (Test-Path -LiteralPath $linkPath) { Remove-Item -LiteralPath $linkPath -Force -Recurse }
        # Windows PowerShell 5.1 expands -Target as a wildcard even though -Path
        # creates a literal name. Escape only its target lookup; the provider
        # resolves that expression before storing the real junction destination.
        $junctionTarget = $targetPath
        if ($PSVersionTable.PSVersion.Major -le 5) {
            $junctionTarget = [System.Management.Automation.WildcardPattern]::Escape($targetPath)
        }
        New-Item -ItemType Junction -Path $linkPath -Target $junctionTarget | Out-Null
        Write-Host "link $name -> $target"
    }
    Write-Host "`nDone. $($links.Count) compat links are directory junctions." -ForegroundColor Green
    Write-Host "git status stays clean (skip-worktree). Undo per header comment."
}
finally {
    [Console]::OutputEncoding = $previousOutputEncoding
    Pop-Location
}
