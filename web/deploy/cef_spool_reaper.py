#!/usr/bin/env python3
"""Bound the Chromium persistent-histogram spool under the CEF cache roots.

Chromium writes one `.pma` persistent-histogram file per browser session into
`<cache root>/BrowserMetrics/`, and defers others into
`DeferredBrowserMetrics/`. They are only ever consumed by a metrics uploader.
Chudware configures none, so nothing reads them and nothing deletes them.

Measured on livingroom-pc on 2026-08-27: 2,790 files, 10.5 GB, accumulated in
16 days — about 650 MB/day — on a 119 GB SSD that also carries PostgreSQL, the
release store and the cloudflared tunnel. That is a disk-full outage on a timer.

The real fix is in the host (`host/src/ui/widgets/cef/`), which reaps the spool
at CEF startup. This exists anyway, deliberately, for two reasons: the daemon on
this host can run for weeks without a restart, so a startup-only reaper has no
opportunity to fire; and a host binary is only redeployed when someone rebuilds
it, while this runs whether or not that has happened. Defence in depth against a
failure whose blast radius is the whole box.

Losing a spool file is harmless by construction — nothing reads them — so this
never needs to be careful about which it drops, only about staying inside the
two spool directories and never touching a file a live process still needs.

    python3 Webservice/deploy/cef_spool_reaper.py            # reap
    python3 Webservice/deploy/cef_spool_reaper.py --dry-run   # report only
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Keep anything a currently-running browser could still hold open. The host's
# own reaper uses 24h; this is deliberately tighter because it runs daily on a
# machine whose daemon may never restart, and an hour is already far longer than
# any session's write window.
MAX_AGE_SECONDS = 3600

# Second bound, so a restart loop cannot outrun the age rule: even if every file
# is younger than MAX_AGE_SECONDS, keep only the newest this many per directory.
MAX_RETAINED = 32

# The ONLY directories this touches, relative to each CEF cache root.
SPOOL_DIRS = ("BrowserMetrics", "DeferredBrowserMetrics")

# GUI and daemon have separate ProcessSingleton roots; see
# host/src/ui/widgets/cef/cef_platform.h.
CACHE_LEAVES = ("cef_cache_daemon", "cef_cache_gui", "cef_cache")


def _data_home() -> Path:
    xdg = (os.environ.get("XDG_DATA_HOME") or "").strip()
    return Path(xdg) if xdg else Path.home() / ".local" / "share"


def spool_directories() -> list[Path]:
    """Every existing spool directory under every CEF cache root."""
    root = _data_home() / "chudware"
    return [
        root / leaf / spool
        for leaf in CACHE_LEAVES
        for spool in SPOOL_DIRS
        if (root / leaf / spool).is_dir()
    ]


def reap(directory: Path, *, now: float, dry_run: bool) -> tuple[int, int]:
    """Delete stale spool files in one directory. Returns (files, bytes)."""
    entries: list[tuple[float, int, Path]] = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((stat.st_mtime, stat.st_size, path))

    entries.sort(reverse=True)  # newest first
    doomed = [
        entry
        for index, entry in enumerate(entries)
        if index >= MAX_RETAINED or (now - entry[0]) > MAX_AGE_SECONDS
    ]

    files = 0
    freed = 0
    for _, size, path in doomed:
        if not dry_run:
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as error:
                print(f"  could not remove {path}: {error}", file=sys.stderr)
                continue
        files += 1
        freed += size
    return files, freed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only")
    arguments = parser.parse_args()

    now = time.time()
    directories = spool_directories()
    if not directories:
        print("cef_spool_reaper: no CEF metrics spool directories present")
        return 0

    total_files = 0
    total_bytes = 0
    for directory in directories:
        files, freed = reap(directory, now=now, dry_run=arguments.dry_run)
        total_files += files
        total_bytes += freed
        verb = "would remove" if arguments.dry_run else "removed"
        print(
            f"cef_spool_reaper: {verb} {files} file(s), "
            f"{freed / 1048576:.1f} MiB from {directory}"
        )

    verb = "would free" if arguments.dry_run else "freed"
    print(
        f"cef_spool_reaper: {verb} {total_bytes / 1048576:.1f} MiB "
        f"across {total_files} file(s) "
        f"(age > {MAX_AGE_SECONDS}s or beyond the newest {MAX_RETAINED})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
