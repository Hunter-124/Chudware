"""CI guard: consumer Claude/Gemini subscription-OAuth endpoints are allowlisted.

Contract: contracts/providers.md §4.2 + §4.4. Subscription-auth provider modules under
services/agent/providers/ are explicitly allowlisted (§4.4, owner-authorized 2026-07-07).
Gemini consumer OAuth remains fully prohibited. This grep-level gate fails if any
NON-ALLOWLISTED tracked source references a consumer OAuth token-reuse endpoint for
Claude/Gemini. The BLESSED session login for non-subscription use is OpenRouter PKCE
(openrouter.ai/auth).

Runs with no network/secrets; scans the working tree under the repo root.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Endpoints/markers that indicate consumer-subscription OAuth token reuse for Claude/Gemini.
# OpenRouter (openrouter.ai/auth) and generic BYO api-key envs are NOT prohibited.
PROHIBITED = [
    re.compile(r"claude\.ai/(?:oauth|api/auth)", re.I),
    re.compile(r"console\.anthropic\.com/oauth", re.I),
    re.compile(r"api\.anthropic\.com/oauth", re.I),
    re.compile(r"CLAUDE_CODE_OAUTH_TOKEN", re.I),
    re.compile(r"gemini(?:_|-)?(?:code_?assist|cloud_?code)", re.I),
    re.compile(r"accounts\.google\.com/o/oauth2.*(?:gemini|codeassist)", re.I),
    re.compile(r"oauth2\.googleapis\.com/token.*gemini", re.I),
]

# This test file, the contract, the master plan, and the subscription-auth provider
# modules (§4.4) legitimately reference these endpoints.
ALLOWLIST = {
    "services/agent/test_prohibited_auth.py",
    "contracts/providers.md",
    "docs/AI_TOOLING_MASTER_PLAN.md",
    "services/agent/providers/sub_auth.py",
    "services/agent/providers/codex_sub_provider.py",
    "services/agent/providers/anthropic_sub_provider.py",
    "services/agent/providers/cursor_sub_provider.py",
    # Vendored OMP provider registry: subscription-auth (Claude OAuth) is a
    # sanctioned sign-in path through the OMP sidecar (§4.4).
    "vendor/omp/packages/ai/src/registry/oauth/anthropic.ts",
}
SCAN_EXT = {".py", ".cpp", ".h", ".hpp", ".ps1", ".js", ".ts", ".json", ".toml", ".cfg"}

# A path `git ls-files` MUST report. If it doesn't, the scan is not looking at this repo.
SCAN_SENTINEL = "services/agent/providers/sub_auth.py"


def _require_plausible_scan(files: list[str]) -> list[str]:
    """Fail loudly when *files* cannot possibly be this repo's tracked sources.

    A grep-style guard that scans zero files still reaches its assertion and reports
    green — a false negative in exactly the environments nobody watches: `git archive`
    exports, sdists, and CI checkouts with no .git, where `git ls-files` returns
    nothing (or errors) and the prohibition is never actually evaluated. Refuse to
    issue a clean bill of health for a scan that did not happen.
    """
    if SCAN_SENTINEL not in files:
        raise AssertionError(
            f"cannot scan repo, guard did not run: `git ls-files` under {REPO} yielded "
            f"{len(files)} scannable file(s), none of them the known-tracked sentinel "
            f"{SCAN_SENTINEL!r}. The consumer-OAuth prohibition was NOT checked; this "
            "run proves nothing."
        )
    return files


def _tracked_files() -> list[str]:
    try:
        out = subprocess.run(["git", "-C", str(REPO), "ls-files"],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise AssertionError(
            f"cannot scan repo, guard did not run: `git ls-files` failed under {REPO} "
            f"({exc}). The consumer-OAuth prohibition was NOT checked."
        ) from exc
    return _require_plausible_scan(
        [f for f in out.splitlines() if Path(f).suffix in SCAN_EXT])


def test_no_consumer_oauth_token_reuse():
    hits = []
    for rel in _tracked_files():
        if rel in ALLOWLIST:
            continue
        try:
            text = (REPO / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in PROHIBITED:
            if pat.search(text):
                hits.append(f"{rel}: matches {pat.pattern!r}")
    assert not hits, (
        "Consumer Claude/Gemini subscription-OAuth endpoint detected in non-allowlisted "
        "file (contract §4.2). If this is a new subscription-auth provider, add it to the "
        "ALLOWLIST in this test (§4.4):\n  " + "\n  ".join(hits))


def test_scan_guard_refuses_to_pass_on_a_vacuous_file_list():
    """A scan that saw nothing must fail loudly, never silently pass.

    Regression: run from a `git archive` export (no .git), `git ls-files` yielded an
    empty list, so the prohibition above looped zero times and asserted `not []` —
    green without checking a single file.
    """
    with pytest.raises(AssertionError, match="guard did not run"):
        _require_plausible_scan([])
    with pytest.raises(AssertionError, match="guard did not run"):
        _require_plausible_scan(["README.md", "setup.py"])
    assert _require_plausible_scan([SCAN_SENTINEL, "x.py"]) == [SCAN_SENTINEL, "x.py"]


def test_scan_sentinel_is_actually_tracked():
    """The sentinel is only a valid liveness check while git really tracks it."""
    tracked = subprocess.run(["git", "-C", str(REPO), "ls-files", "--error-unmatch",
                              SCAN_SENTINEL], capture_output=True, text=True)
    assert tracked.returncode == 0, tracked.stderr


def test_openrouter_pkce_is_present_as_the_blessed_path():
    """Sanity: the blessed alternative actually exists, so the prohibition has an out."""
    pk = (REPO / "services/agent/providers/oauth_pkce.py").read_text(encoding="utf-8")
    assert "openrouter.ai/auth" in pk
    assert "code_challenge_method" in pk and "S256" in pk
