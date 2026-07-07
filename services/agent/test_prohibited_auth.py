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
}
SCAN_EXT = {".py", ".cpp", ".h", ".hpp", ".ps1", ".js", ".ts", ".json", ".toml", ".cfg"}


def _tracked_files():
    out = subprocess.run(["git", "-C", str(REPO), "ls-files"],
                         capture_output=True, text=True, check=True).stdout
    return [f for f in out.splitlines() if Path(f).suffix in SCAN_EXT]


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


def test_openrouter_pkce_is_present_as_the_blessed_path():
    """Sanity: the blessed alternative actually exists, so the prohibition has an out."""
    pk = (REPO / "services/agent/providers/oauth_pkce.py").read_text(encoding="utf-8")
    assert "openrouter.ai/auth" in pk
    assert "code_challenge_method" in pk and "S256" in pk
