#!/usr/bin/env python3
"""Rename content-hashed gateway static assets to match their bytes.

``portal_serve.py`` serves ``/portal-assets/<name>.<digest>.<ext>`` with
``Cache-Control: immutable``, and ``tests/test_portal.py`` asserts the digest in
every filename really is the sha256 prefix of the served bytes. That contract
was maintained by hand, so editing a stylesheet meant renaming the file and
hand-patching every referencing page — and forgetting either half ships a portal
that 404s its own CSS with a year-long cache lifetime.

This walks the HTML pages, finds every hashed asset reference, re-digests the
file on disk, and renames + rewrites in lockstep. Idempotent: an already-correct
tree reports zero changes.

    python3 Webservice/deploy/rehash_static.py            # rewrite in place
    python3 Webservice/deploy/rehash_static.py --check     # exit 1 if stale
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "gateway" / "static"

# Mirrors portal_serve._HASHED_ASSET_RE, narrowed to the 12-hex names the pages
# actually emit so an unhashed sibling (base.css, admin.js) is never touched.
# `woff2` is why the extension class carries digits; a letters-only class
# matched "woff" and left the "2" behind, inventing a filename nothing has.
_REFERENCE_RE = re.compile(
    r"(?P<stem>[A-Za-z0-9_-]+)\.(?P<digest>[0-9a-f]{12})\.(?P<ext>[a-z][a-z0-9]*)"
)

_DIGEST_LENGTH = 12


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:_DIGEST_LENGTH]


def _referrers() -> list[Path]:
    """Files that can name a hashed asset.

    Stylesheets count: the portal's @font-face rules point at
    /portal-assets/fonts/<name>.<digest>.woff2, so a font rename edits the CSS,
    which changes the CSS digest, which renames the CSS. That cascade is why
    rehash() iterates to a fixpoint instead of doing a single pass.
    """
    return sorted(
        path
        for pattern in ("*.html", "**/*.css")
        for path in STATIC.glob(pattern)
    )


def _resolve(referrer: Path, name: str) -> Path | None:
    """Locate the file a reference names, beside the referrer or at the root."""
    for candidate in (referrer.parent / name, STATIC / name):
        if candidate.is_file():
            return candidate
    matches = [path for path in STATIC.rglob(name) if path.is_file()]
    return matches[0] if len(matches) == 1 else None


def _one_pass(*, apply: bool) -> tuple[int, list[str]]:
    """Rename every mismatched asset once. Returns (changes, log lines)."""
    renames: dict[Path, str] = {}
    log: list[str] = []

    for referrer in _referrers():
        text = referrer.read_text(encoding="utf-8")
        for match in _REFERENCE_RE.finditer(text):
            stem, digest, ext = match.group("stem", "digest", "ext")
            target = _resolve(referrer, f"{stem}.{digest}.{ext}")
            if target is None:
                # The reference is stale on disk: adopt the one sibling that
                # shares this stem and extension.
                siblings = [
                    path
                    for path in STATIC.rglob(f"{stem}.*.{ext}")
                    if _REFERENCE_RE.fullmatch(path.name)
                ]
                if len(siblings) != 1:
                    print(
                        f"rehash_static: {referrer.name} references "
                        f"{stem}.{digest}.{ext}, which is missing and has "
                        f"{len(siblings)} candidate siblings",
                        file=sys.stderr,
                    )
                    raise SystemExit(2)
                target = siblings[0]
            actual = _digest(target)
            if actual == digest and target.name == f"{stem}.{digest}.{ext}":
                continue
            renames[target] = f"{stem}.{actual}.{ext}"

    if not renames:
        return 0, log

    mapping = {path.name: name for path, name in renames.items() if path.name != name}
    for old, new in sorted(mapping.items()):
        log.append(f"rehash_static: {old} -> {new}")
    if not apply:
        return len(renames), log

    for path, name in renames.items():
        if path.name != name:
            path.rename(path.with_name(name))
    for referrer in _referrers():
        text = original = referrer.read_text(encoding="utf-8")
        for old, new in mapping.items():
            text = text.replace(old, new)
        if text != original:
            referrer.write_text(text, encoding="utf-8")
            log.append(f"rehash_static: rewrote {referrer.name}")
    return len(renames), log


def rehash(*, check: bool) -> int:
    """Return the number of assets whose digest did not match their bytes."""
    total = 0
    # A font rename dirties its stylesheet, whose rename dirties the page that
    # links it. Three levels is the deepest chain the portal has; the bound is
    # generous so a fourth never silently truncates.
    for _ in range(8):
        changed, log = _one_pass(apply=not check)
        for line in log:
            print(line)
        total += changed
        if not changed or check:
            break
    else:
        print("rehash_static: reference graph did not settle", file=sys.stderr)
        return -1
    if not total:
        print("rehash_static: every hashed asset matches its bytes")
    return total if check else 0



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report stale digests without renaming anything",
    )
    arguments = parser.parse_args()
    result = rehash(check=arguments.check)
    return 1 if result != 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
