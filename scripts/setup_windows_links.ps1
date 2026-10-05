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
  NTFS directory JUNCTION (mklink /J) — no admin / Developer Mode required. Each path is
  marked skip-worktree so the junction doesn't show up as a git change.

  Idempotent: re-running only fixes entries that aren't already correct junctions.

.NOTES
  To undo:  git update-index --no-skip-worktree <name>; git checkout -- <name>
  See docs/reference/BUILD_WINDOWS.md ("Checkout portability"). Alternative:
  `git config core.symlinks true` + Windows Developer Mode, then re-clone.
#>
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $repo
try {
    # Read the git-tracked symlinks (mode 120000) and their targets straight from the index,
    # so this stays correct if the set of compat links ever changes.
    $links = @{}
    # A git that cannot read the repository (e.g. "dubious ownership") prints nothing on
    # stdout; without this check that read as "no symlinks" and left the text stubs in place.
    $entries = @(git ls-files -s)
    if ($LASTEXITCODE -ne 0) { throw "git ls-files -s failed (exit $LASTEXITCODE) in $repo" }
    foreach ($line in $entries) {
        $parts = $line -split '\s+', 4
        if ($parts[0] -eq '120000') {
            $name = $parts[3]
            $target = (git cat-file -p ":$name").Trim()
            $links[$name] = $target
        }
    }
    if ($links.Count -eq 0) { Write-Host 'No git symlinks found — nothing to do.'; return }

    foreach ($name in $links.Keys) {
        $target = $links[$name]
        $targetAbs = (Resolve-Path (Join-Path $repo $target) -ErrorAction SilentlyContinue)
        if (-not $targetAbs) { Write-Warning "SKIP $name -> $target (target missing)"; continue }
        $targetPath = $targetAbs.Path
        $linkPath = Join-Path $repo $name

        # Already a junction/dir pointing at the right place? leave it.
        $item = Get-Item -LiteralPath $linkPath -Force -ErrorAction SilentlyContinue
        if ($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) `
                   -and ($item.Target -and ((Resolve-Path $item.Target).Path -eq $targetPath))) {
            Write-Host "ok   $name -> $target (already a junction)"
            continue
        }

        git update-index --skip-worktree $name 2>$null
        if (Test-Path -LiteralPath $linkPath) { Remove-Item -LiteralPath $linkPath -Force -Recurse }
        cmd /c mklink /J "`"$linkPath`"" "`"$targetPath`"" | Out-Null
        Write-Host "link $name -> $target"
    }
    Write-Host "`nDone. $($links.Count) compat links are directory junctions." -ForegroundColor Green
    Write-Host "git status stays clean (skip-worktree). Undo per header comment."
}
finally { Pop-Location }
