#!/usr/bin/env python3
"""Apply the release retention policy and log exactly what it freed.

Run daily by chudware-release-retention.timer. The policy itself lives in
``releases.prune_release_store`` — the same function the admin route calls — so
the timer and the button cannot drift into two different policies.

Why a timer and not just the admin button: production's filesystem is 97% used
with under 4 GB free, and one nightly now publishes an AppImage plus a .deb, an
.rpm and a .tar.zst instead of one AppImage. Unbounded, that is roughly ten
nights before the disk is full, and a full disk takes PostgreSQL and the tunnel
down with the gateway. Retention that depends on somebody remembering to press a
button is not retention.

Output goes to the journal, per (channel, platform, arch, format) and in bytes,
so the next person can tell whether the policy is working without reading the
code or reverse-engineering it from directory listings:

    journalctl --user -u chudware-release-retention.service -n 50
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

GATEWAY = Path(__file__).resolve().parents[1] / "gateway"
if str(GATEWAY) not in sys.path:
    sys.path.insert(0, str(GATEWAY))


def _human(n: int) -> str:
    if n >= 1024**3:
        return f"{n / 1024**3:.2f} GiB"
    if n >= 1024**2:
        return f"{n / 1024**2:.1f} MiB"
    if n >= 1024:
        return f"{n / 1024:.1f} KiB"
    return f"{n} B"


def _report(result: dict[str, object]) -> None:
    """Journal lines, deliberately grep-friendly ``key=value`` where it counts."""
    keep = result["keep_beta_versions"]
    pruned: list[dict[str, object]] = result["pruned"]  # type: ignore[assignment]
    orphans: list[dict[str, object]] = result["incoming_orphans"]  # type: ignore[assignment]

    print(f"keep_beta_versions={keep}")
    print(
        f"pruned_rows={len(pruned)} deleted_files={result['deleted_files']} "
        f"freed_bytes={result['freed_bytes']}"
    )
    print(
        f"incoming_orphans={len(orphans)} "
        f"incoming_freed_bytes={result['incoming_freed_bytes']}"
    )

    # Per (channel, platform, arch, format): the whole point of the breakdown is
    # to answer "is one target the thing filling the disk?" at a glance.
    by_target: dict[tuple[str, str, str, str], tuple[int, int, int]] = {}
    for entry in pruned:
        key = (
            str(entry["channel"]), str(entry["platform"]),
            str(entry["arch"]), str(entry["format"]),
        )
        versions, files, size = by_target.get(key, (0, 0, 0))
        by_target[key] = (
            versions + 1, files + int(entry["files"]), size + int(entry["bytes"])
        )
    for key in sorted(by_target):
        versions, files, size = by_target[key]
        print(
            f"  freed {'/'.join(key)}: versions={versions} files={files} "
            f"bytes={size} ({_human(size)})"
        )
    for entry in sorted(pruned, key=lambda e: str(e["artifact_key"])):
        print(f"  dropped {entry['artifact_key']} ({_human(int(entry['bytes']))})")
    for entry in sorted(orphans, key=lambda e: str(e["name"])):
        print(f"  dropped incoming/{entry['name']} ({_human(int(entry['bytes']))})")

    total = int(result["freed_bytes"]) + int(result["incoming_freed_bytes"])
    print(f"total_freed_bytes={total} ({_human(total)})")
    for problem in result["errors"]:  # type: ignore[union-attr]
        print(f"WARN: {problem}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in args
    for unknown in (a for a in args if a != "--dry-run"):
        print(f"release retention failed: unknown argument {unknown}", file=sys.stderr)
        return 2
    if not (os.environ.get("CHUDWARE_DB_URL") or "").strip():
        # The unit reads gateway.env, which sets it. Without it db.dsn() falls
        # back to the production DSN, which is exactly the wrong thing to do
        # silently from a staging timer.
        print("release retention failed: CHUDWARE_DB_URL is required", file=sys.stderr)
        return 2

    import db  # noqa: PLC0415 - after sys.path is prepared
    import releases  # noqa: PLC0415

    try:
        result = releases.prune_release_store(dry_run=dry_run)
    except Exception as exc:  # journal must show failure without leaking a DSN
        print(f"release retention failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close_pool()

    if dry_run:
        print("dry_run=1 (nothing was deleted)")
    _report(result)
    # A partial failure is reported but not fatal: the rows are already gone and
    # the next run re-reports whatever could not be unlinked.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
