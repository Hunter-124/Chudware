#!/usr/bin/env python3
"""Policy gate for the load-bearing Chudware CI workflow set.

This gate exists because the expensive half of CI (self-hosted Windows/macOS
builders, the full Linux gate, packaging) only runs *after* GitHub has accepted
a workflow. A workflow that fails to parse, or that pins an action to a mutable
tag, is therefore discovered late -- at dispatch time, or never. Everything
checked here is cheap, deterministic, and answerable from the workflow text
alone.

The second reason this file exists is money. This repository is private, and on
a private repository GitHub bills GitHub-*hosted* runner minutes while
self-hosted minutes are free. It also bills artifact storage above the 500 MB
included with the Free plan, accrued hourly. Chudware's CI budget is $0, which
is not a convention anyone can be relied on to remember: a single
``runs-on: ubuntu-latest`` added in a hurry, or one uploaded 500 MB installer
left at the 90-day default retention, spends real money silently. So the
zero-spend rules below are enforced here, in code, on every push that touches
``.github/workflows/**``.

Rules enforced over the policy scope:

  missing-workflow          a contract-named workflow is absent
  empty-workflow            a contract-named workflow exists but has no content
  workflow-parse-error      the file is not parseable as a workflow document
  unpinned-action-ref       an external ``uses:`` is not a 40-hex commit SHA
  missing-local-workflow    a ``./`` reusable workflow target does not exist
  missing-permissions       no explicit top-level ``permissions:``
  overbroad-permissions     top-level ``permissions: write-all``
  missing-concurrency       no top-level ``concurrency:`` group
  unsafe-cancel-in-progress ``cancel-in-progress: true`` outside pull requests
  coherent-build-cancellation a multi-platform artifact leg can cancel non-PR work
  hosted-runner             a job can land on a metered GitHub-hosted runner
  string-form-runs-on       a job's ``runs-on`` is a bare string, not a label array
  missing-runs-on           a job declares no runner at all
  unresolved-runner-matrix  a ``runs-on`` expression has no checkable matrix source
  dedicated-runner-misuse   a non-publish/control job targets infrastructure
  unsafe-generic-linux-routing a Linux job can land on the production host
  billable-artifact-retention an upload has no short ``retention-days``
  powershell-non-ascii      a PowerShell step body carries a non-ASCII byte
  incompatible-windows-shell a Windows run step can dispatch to an unavailable shell
  missing-jobs              the document declares no jobs
  missing-job-timeout       a job has no usable ``timeout-minutes:``
  banned-permission         ``permissions.administration`` anywhere
  invalid-permission-scope  a permission key GitHub does not accept
  runner-trust-boundary     a job lacks an explicit isolated self-hosted group
  protected-job-guard       persistent jobs lack the protected ref/event guard
  protected-job-environment persistent jobs bypass protected environment approval
  untrusted-secret-delivery branch/PR jobs can receive secrets or environment secrets
  untrusted-write-permission branch/PR jobs have a write-capable GITHUB_TOKEN
  protected-untrusted-input persistent jobs consume artifacts/caches or arbitrary refs
  reusable-secret-boundary  reusable jobs inherit secrets or bypass guarded local calls

Dependency-free by design. The gate has to run on a bare CPython before any
package manager is available, so it ships a small indentation-driven parser
for the YAML subset GitHub Actions actually uses rather than importing PyYAML.
Every entry point takes an explicit path so fixture trees can be driven through
the same code the real tree goes through.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence as Seq, Tuple

# --------------------------------------------------------------------------
# Policy scope
# --------------------------------------------------------------------------

# The workflow set named in the Chudware CI contract (plan step 1.3). Each is
# load-bearing: losing one silently drops a gate, a platform, or the daily
# capability probe that tells an operator a runner has fallen off the fleet.
REQUIRED_WORKFLOWS: Tuple[str, ...] = (
    # Cheapest gate in the repository and the only one that runs on every
    # platform: scripts/check.py's source/audit sub-gates. If this stops
    # running, source-level drift reaches master unchecked.
    "fast-gate.yml",
    # The full Linux gate (scripts/gate.sh) plus the two suites gate.sh has
    # never run itself: host/ ctest and the gateway pytest tree.
    "linux-gate.yml",
    # Per-platform configure + build + ctest. This is the only thing that
    # keeps the Windows and macOS trees compiling between releases.
    "native-build.yml",
    # Daily capability probes. Without them a runner that lost its toolchain
    "runner-smoke.yml",
    # Commercial payload preparation must remain self-hosted, bounded, and stop at
    # the authenticated offline-owner signing boundary.
    "release.yml",
    "release-prepare.yml",
    # The continuous-deployment actuator. Without it a green master commit is
    # never promoted, which is exactly how production served a fifteen-day-old
    # web bundle behind a fully green tree. It is required rather than
    # advisory for a second reason: it is the only workflow that runs on the
    # production host and the only one that can restart the customer-facing
    # daemon, so a routing or permissions regression in it is a production
    # incident, not a CI annoyance.
    "deploy-prod.yml",
)

# This gate polices itself too.
SELF_WORKFLOW = "ci-workflow-policy.yml"

POLICY_WORKFLOWS: Tuple[str, ...] = REQUIRED_WORKFLOWS + (
    SELF_WORKFLOW, "nightly-release.yml", "runner-provision.yml",
    "chudcode-runtime-windows-build.yml", "stripe-sandbox.yml",
    "site-build.yml", "prod-external-watch.yml",
)

WORKFLOW_DIR = Path(".github") / "workflows"

# Permission scopes GitHub accepts in a workflow ``permissions:`` block.
# Anything else makes GitHub reject the workflow at parse time.
PERMISSION_SCOPES = frozenset(
    {
        "actions",
        "attestations",
        "checks",
        "contents",
        "deployments",
        "discussions",
        "id-token",
        "issues",
        "models",
        "packages",
        "pages",
        "pull-requests",
        "repository-projects",
        "security-events",
        "statuses",
    }
)

# ``administration`` is the specific trap the sibling fleet repository has
# already hit: listing self-hosted runners needs Administration:read, and
# writing it into ``permissions:`` looks plausible. GitHub rejects the whole
# workflow at parse time, so it must come from the CI_RUNNER_ADMIN_TOKEN PAT
# secret instead.
BANNED_PERMISSIONS = frozenset({"administration"})

# --------------------------------------------------------------------------
# Zero-spend policy
# --------------------------------------------------------------------------
#
# This repository is private. On a private repository GitHub meters
# GitHub-hosted runner minutes and bills them; self-hosted runner minutes are
# free. Artifact storage is metered too -- 500 MB included, then $0.25 per
# GB-month accrued hourly -- and Chudware's artifacts are ~500 MB installers.
# Job logs and $GITHUB_STEP_SUMMARY are not metered, so diagnostics belong
# there, never in an uploaded artifact.
#
# The whole CI budget is $0. That is a property of the workflow text, so it is
# checked here rather than trusted to reviewer memory.

SELF_HOSTED_LABEL = "self-hosted"

# Any label matching one of these is a metered GitHub-hosted image or a larger
# runner. Matched as a prefix or as an exact ``*-latest`` label.
HOSTED_LABEL_PREFIXES = ("ubuntu-", "windows-", "macos-")
HOSTED_LABEL_MARKERS = ("-latest", "-largerunner")

# Artifacts are billed by the GB-hour. Seven days of a build output is real
# money; one day is the CloudCastle-proven outage fallback.
UPLOAD_ARTIFACT_ACTION = "actions/upload-artifact"
MAX_ARTIFACT_RETENTION_DAYS = 7

# The runner writes a step's script to disk as UTF-8 with no BOM. Windows
# PowerShell 5.1 -- the only PowerShell on the Chudware Windows endpoints --
# decodes a BOM-less file as the ANSI code page instead, so a UTF-8 em dash
# (E2 80 94) arrives as three CP1252 characters ending in U+201D, a curly
# closing quote. PowerShell accepts curly quotes as string delimiters, so one
# em dash in a comment terminates a string several lines earlier and the whole
# step dies at parse time with "missing closing '}'" -- before a single line
# of it runs, and with an error that points at the wrong line. Observed twice
# on ryzen-7900x3d while provisioning it. PowerShell step bodies are therefore
# ASCII-only, and that is a property of the text, so it is checked here.
POWERSHELL_SHELLS = frozenset({"powershell", "pwsh"})

# Infrastructure labels are not spare build capacity. Exceptions are exact
# workflow/job pairs whose observable contract requires execution on the
# serving or production-control host. A nearby job name is not enough --
# ``livingroom-pc`` is simultaneously a CI runner and the production gateway
# host, so a build leg that lands there starves paying customers.
DEDICATED_RUNNER_LABELS = frozenset(
    {"cc-infra-webservice", "cc-prod-control", "cc-artifact-host"}
)
LINUX_ENDPOINT_LABELS = frozenset(
    {"cc-linux-build", "cc-linux-livingroom", "cc-linux-hunter"}
)
DEDICATED_RUNNER_EXCEPTIONS: Dict[Tuple[str, str], frozenset] = {
    # External health watch. The whole point is that it runs OFF the box it is
    # watching: an alerting job on livingroom-pc cannot report that
    # livingroom-pc is down. cloudcastle-server is the only always-on Linux
    # endpoint that is not the production host, and this is a control job by
    # definition, so the dedicated label is the requirement rather than a
    # convenience. Both of that host's role labels are accepted because the
    # watcher is equally at home under either.
    ("prod-external-watch.yml", "watch"): frozenset(
        {"cc-artifact-host", "cc-prod-control"}
    ),
}

# These workflows produce a per-platform artifact set whose commit identity
# must stay coherent once work has begun -- the cross-platform parity gate
# compares one platform's dump against another's, and a half-cancelled matrix
# compares two different commits. Only stale pull-request work may be
# cancelled; later pushes and tags must queue.
COHERENT_RELEASE_WORKFLOWS = frozenset({"native-build.yml"})
PR_ONLY_CANCEL_EXPRESSION = "${{ github.event_name == 'pull_request' }}"

# Groups are free self-hosted capacity, not GitHub-hosted larger runners.
# Group membership and exact workflow/ref authorization are enforced outside
# the checkout; this verifier cannot attest to those operational controls.
UNTRUSTED_RUNNER_GROUP = "chudware-untrusted-ephemeral"
TRUSTED_RUNNER_GROUP = "chudware-trusted-protected"
PROTECTED_ENVIRONMENTS = frozenset({
    "chudware-release-protected", "chudware-control-protected",
    "chudware-maintenance-protected",
})
TRUSTED_JOB_GUARD = (
    "github.ref_protected && "
    "(github.ref == 'refs/heads/master' || startsWith(github.ref, 'refs/tags/v')) && "
    "(github.event_name == 'push' || github.event_name == 'workflow_dispatch' || "
    "github.event_name == 'schedule')"
)

PERMISSION_VALUES = frozenset({"read", "write", "none"})
PERMISSION_SHORTHANDS = frozenset({"read-all", "write-all"})

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DOCKER_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ACTION_RE = re.compile(r"^(?P<repo>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)(?P<path>/[^@]*)?(?:@(?P<ref>.*))?$")
EXPRESSION_RE = re.compile(r"\$\{\{.*\}\}")

ERROR = "error"
ADVISORY = "advisory"


# --------------------------------------------------------------------------
# Minimal YAML subset parser
# --------------------------------------------------------------------------


class WorkflowParseError(Exception):
    """Raised when a workflow file cannot be parsed."""

    def __init__(self, message: str, line: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.line = line

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.line:
            return "line {0}: {1}".format(self.line, self.message)
        return self.message


class Mapping(dict):
    """``dict`` that remembers the source line of each key."""

    __slots__ = ("key_lines", "line")

    def __init__(self, line: int = 0) -> None:
        super().__init__()
        self.key_lines: Dict[str, int] = {}
        self.line = line

    def line_of(self, key: str) -> int:
        return self.key_lines.get(key, self.line)


class Sequence(list):
    """``list`` that remembers where it started."""

    __slots__ = ("line",)

    def __init__(self, line: int = 0) -> None:
        super().__init__()
        self.line = line


@dataclass
class _Line:
    indent: int
    text: str
    lineno: int
    block: Optional[str] = None


_BLOCK_HEADER_RE = re.compile(r"^[|>][+-]?[0-9]*$")


def _scan_quoted(text: str, start: int) -> int:
    """Return the index just past the quoted scalar starting at ``start``."""
    quote = text[start]
    i = start + 1
    while i < len(text):
        ch = text[i]
        if quote == '"' and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if quote == "'" and i + 1 < len(text) and text[i + 1] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    raise WorkflowParseError("unterminated {0} quoted scalar".format(quote))


def _opens_quoted_scalar(text: str, i: int) -> bool:
    """True when the quote at ``i`` actually opens a quoted scalar.

    An apostrophe inside a plain scalar -- ``- name: Verify this runner's
    target`` -- is literal text, not a quote. YAML only honours a quote that
    sits where a scalar may begin.
    """
    j = i - 1
    while j >= 0 and text[j] in " \t":
        j -= 1
    if j < 0:
        return True
    return text[j] in ":,-[{"


def _strip_comment(text: str) -> str:
    """Drop a trailing ``# ...`` comment that sits outside quotes."""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "\"'" and _opens_quoted_scalar(text, i):
            i = _scan_quoted(text, i)
            continue
        if ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
        i += 1
    return text.rstrip()


def _strip_seq_prefix(text: str) -> str:
    while text == "-" or text.startswith("- "):
        text = text[2:].lstrip() if text.startswith("- ") else ""
    return text


def _is_seq_item(text: str) -> bool:
    return text == "-" or text.startswith("- ")


def _split_key_value(text: str) -> Optional[Tuple[str, str]]:
    """Split ``key: value``; return ``None`` when ``text`` is not a mapping entry."""
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "\"'" and _opens_quoted_scalar(text, i):
            i = _scan_quoted(text, i)
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == ":" and depth == 0:
            rest = text[i + 1 :]
            if rest == "" or rest[0] in " \t":
                key = text[:i].strip()
                if key.startswith(("'", '"')) and _scan_quoted(key, 0) == len(key):
                    key = _unquote(key)
                if not key:
                    return None
                return key, rest.strip()
        i += 1
    return None


def _block_header_of(text: str) -> Optional[str]:
    body = _strip_seq_prefix(text)
    kv = _split_key_value(body)
    candidate = kv[1] if kv is not None else body
    if candidate and _BLOCK_HEADER_RE.match(candidate):
        return candidate
    return None


def _tokenize(text: str) -> List[_Line]:
    raw = text.split("\n")
    lines: List[_Line] = []
    i = 0
    total = len(raw)
    while i < total:
        physical = raw[i]
        lineno = i + 1
        stripped = physical.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        leading = physical[: len(physical) - len(physical.lstrip(" \t"))]
        if "\t" in leading:
            raise WorkflowParseError("tab character in indentation", lineno)
        indent = len(leading)
        if stripped in ("---", "..."):
            i += 1
            continue
        try:
            content = _strip_comment(physical[indent:].rstrip())
        except WorkflowParseError as exc:
            raise WorkflowParseError(exc.message, lineno) from None
        if not content:
            i += 1
            continue

        block: Optional[str] = None
        if _block_header_of(content) is not None:
            body: List[str] = []
            j = i + 1
            while j < total:
                nxt = raw[j]
                if not nxt.strip():
                    body.append("")
                    j += 1
                    continue
                nindent = len(nxt) - len(nxt.lstrip(" "))
                if nindent <= indent:
                    break
                body.append(nxt[indent + 1 :])
                j += 1
            block = "\n".join(body)
            i = j
        else:
            i += 1
        lines.append(_Line(indent, content, lineno, block))
    return lines


def _unquote(text: str) -> str:
    quote = text[0]
    inner = text[1:-1]
    if quote == "'":
        return inner.replace("''", "'")
    out: List[str] = []
    i = 0
    escapes = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", '"': '"', "\\": "\\", "/": "/"}
    while i < len(inner):
        ch = inner[i]
        if ch == "\\" and i + 1 < len(inner):
            out.append(escapes.get(inner[i + 1], inner[i + 1]))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _flow_balanced(text: str) -> bool:
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "\"'" and _opens_quoted_scalar(text, i):
            try:
                i = _scan_quoted(text, i)
            except WorkflowParseError:
                return False
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        i += 1
    return depth == 0


def _parse_flow(text: str, lineno: int) -> Any:
    value, idx = _parse_flow_node(text, 0, lineno)
    if text[idx:].strip():
        raise WorkflowParseError("trailing content after flow collection", lineno)
    return value


def _skip_space(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t":
        i += 1
    return i


def _parse_flow_node(text: str, i: int, lineno: int) -> Tuple[Any, int]:
    i = _skip_space(text, i)
    if i >= len(text):
        return None, i
    if text[i] == "[":
        seq = Sequence(lineno)
        i += 1
        while True:
            i = _skip_space(text, i)
            if i >= len(text):
                raise WorkflowParseError("unterminated flow sequence", lineno)
            if text[i] == "]":
                return seq, i + 1
            item, i = _parse_flow_node(text, i, lineno)
            seq.append(item)
            i = _skip_space(text, i)
            if i < len(text) and text[i] == ",":
                i += 1
                continue
            if i < len(text) and text[i] == "]":
                return seq, i + 1
            raise WorkflowParseError("malformed flow sequence", lineno)
    if text[i] == "{":
        mapping = Mapping(lineno)
        i += 1
        while True:
            i = _skip_space(text, i)
            if i >= len(text):
                raise WorkflowParseError("unterminated flow mapping", lineno)
            if text[i] == "}":
                return mapping, i + 1
            key, i = _parse_flow_scalar(text, i, lineno, stop=":,}")
            i = _skip_space(text, i)
            if i >= len(text) or text[i] != ":":
                raise WorkflowParseError("flow mapping entry without a value", lineno)
            value, i = _parse_flow_node(text, i + 1, lineno)
            mapping[str(key)] = value
            mapping.key_lines[str(key)] = lineno
            i = _skip_space(text, i)
            if i < len(text) and text[i] == ",":
                i += 1
                continue
            if i < len(text) and text[i] == "}":
                return mapping, i + 1
            raise WorkflowParseError("malformed flow mapping", lineno)
    return _parse_flow_scalar(text, i, lineno, stop=",]}")


def _parse_flow_scalar(text: str, i: int, lineno: int, stop: str) -> Tuple[Any, int]:
    if text[i] in "\"'":
        try:
            end = _scan_quoted(text, i)
        except WorkflowParseError as exc:
            raise WorkflowParseError(exc.message, lineno) from None
        return _unquote(text[i:end]), end
    start = i
    while i < len(text) and text[i] not in stop:
        i += 1
    return _plain_scalar(text[start:i].strip()), i


def _plain_scalar(text: str) -> Any:
    if text in ("", "~", "null", "Null", "NULL"):
        return None
    return text


def _parse_scalar(text: str, lineno: int) -> Any:
    text = text.strip()
    if not text:
        return None
    if text[0] in "\"'":
        try:
            end = _scan_quoted(text, 0)
        except WorkflowParseError as exc:
            raise WorkflowParseError(exc.message, lineno) from None
        if end == len(text):
            return _unquote(text)
        return text
    if text[0] in "[{":
        return _parse_flow(text, lineno)
    return _plain_scalar(text)


def _parse_node(lines: List[_Line], i: int, indent: int, anchors: Dict[str, Any]) -> Tuple[Any, int]:
    if _is_seq_item(lines[i].text):
        return _parse_seq(lines, i, indent, anchors)
    return _parse_map(lines, i, indent, anchors)


def _parse_seq(lines: List[_Line], i: int, indent: int, anchors: Dict[str, Any]) -> Tuple[Sequence, int]:
    seq = Sequence(lines[i].lineno)
    while i < len(lines):
        line = lines[i]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise WorkflowParseError("unexpected indentation", line.lineno)
        if not _is_seq_item(line.text):
            break
        rest = line.text[2:] if line.text.startswith("- ") else ""
        offset = len(rest) - len(rest.lstrip(" "))
        rest = rest.strip()
        if not rest:
            j = i + 1
            if j < len(lines) and lines[j].indent > indent:
                value, i = _parse_node(lines, j, lines[j].indent, anchors)
            else:
                value, i = None, j
        elif line.block is not None and _split_key_value(rest) is None:
            # ``- |`` : the item is itself a block scalar.
            value = line.block
            i += 1
        elif _split_key_value(rest) is None and not _is_seq_item(rest):
            # A plain scalar item such as ``- main`` or ``- './path/**'``.
            if rest[0] in "[{" and not _flow_balanced(rest):
                acc = rest
                j = i + 1
                while j < len(lines) and lines[j].indent > indent and not _flow_balanced(acc):
                    acc = acc + " " + lines[j].text
                    j += 1
                if not _flow_balanced(acc):
                    raise WorkflowParseError("unterminated flow collection", line.lineno)
                value = _parse_flow(acc, line.lineno)
                i = j
            else:
                value = _parse_scalar(rest, line.lineno)
                i += 1
        else:
            # Re-anchor the item's content at the column it actually starts on
            # so ``- name: x`` / ``  uses: y`` reads as one mapping.
            content_indent = indent + 2 + offset
            lines[i] = _Line(content_indent, rest, line.lineno, line.block)
            value, i = _parse_node(lines, i, content_indent, anchors)
        seq.append(value)
    return seq, i


def _parse_map(lines: List[_Line], i: int, indent: int, anchors: Dict[str, Any]) -> Tuple[Mapping, int]:
    mapping = Mapping(lines[i].lineno)
    while i < len(lines):
        line = lines[i]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise WorkflowParseError("unexpected indentation", line.lineno)
        if _is_seq_item(line.text):
            raise WorkflowParseError("sequence item where a mapping key was expected", line.lineno)
        kv = _split_key_value(line.text)
        if kv is None:
            raise WorkflowParseError("expected a 'key: value' mapping entry", line.lineno)
        key, raw_value = kv
        if key in mapping:
            raise WorkflowParseError("duplicate key {0!r}".format(key), line.lineno)

        # Shared block steps are valid Actions YAML. Resolve them in this
        # document so policy checks inspect the inherited steps, not a string.
        anchor_match = re.fullmatch(r"&([A-Za-z0-9_-]+)", raw_value)
        if anchor_match:
            raw_value = ""

        if line.block is not None:
            value: Any = line.block
            i += 1
        elif raw_value == "":
            j = i + 1
            if j < len(lines) and lines[j].indent > indent:
                value, i = _parse_node(lines, j, lines[j].indent, anchors)
            elif j < len(lines) and lines[j].indent == indent and _is_seq_item(lines[j].text):
                value, i = _parse_seq(lines, j, indent, anchors)
            else:
                value, i = None, j
        elif raw_value.startswith("*"):
            alias_match = re.fullmatch(r"\*([A-Za-z0-9_-]+)", raw_value)
            if alias_match is None or alias_match[1] not in anchors:
                raise WorkflowParseError("unknown YAML alias {0!r}".format(raw_value), line.lineno)
            value = anchors[alias_match[1]]
            i += 1
        elif raw_value[0] in "[{" and not _flow_balanced(raw_value):
            acc = raw_value
            j = i + 1
            while j < len(lines) and lines[j].indent > indent and not _flow_balanced(acc):
                acc = acc + " " + lines[j].text
                j += 1
            if not _flow_balanced(acc):
                raise WorkflowParseError("unterminated flow collection", line.lineno)
            value = _parse_flow(acc, line.lineno)
            i = j
        else:
            value = _parse_scalar(raw_value, line.lineno)
            i += 1

        if anchor_match:
            anchors[anchor_match[1]] = value

        mapping[key] = value
        mapping.key_lines[key] = line.lineno
    return mapping, i


def parse_workflow(text: str) -> Mapping:
    """Parse workflow ``text`` into a mapping, or raise :class:`WorkflowParseError`."""
    lines = _tokenize(text)
    if not lines:
        raise WorkflowParseError("document is empty")
    document, index = _parse_node(lines, 0, lines[0].indent, {})
    if index < len(lines):
        raise WorkflowParseError("unexpected content after the document", lines[index].lineno)
    if not isinstance(document, Mapping):
        raise WorkflowParseError("workflow must be a mapping at the top level", lines[0].lineno)
    return document


def load_workflow(path: Path) -> Mapping:
    """Parse the workflow at ``path``. Accepts any fixture path."""
    return parse_workflow(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    path: str
    line: int
    message: str

    def render(self) -> str:
        where = self.path if not self.line else "{0}:{1}".format(self.path, self.line)
        return "{0} [{1}] {2}: {3}".format(self.severity, self.rule, where, self.message)

    def annotation(self) -> str:
        level = "error" if self.severity == ERROR else "warning"
        location = "file={0}".format(self.path)
        if self.line:
            location += ",line={0}".format(self.line)
        return "::{0} {1},title={2}::{3}".format(level, location, self.rule, self.message)


def _walk_mappings(node: Any) -> Iterator[Mapping]:
    if isinstance(node, Mapping):
        yield node
        for value in node.values():
            for found in _walk_mappings(value):
                yield found
    elif isinstance(node, Sequence):
        for value in node:
            for found in _walk_mappings(value):
                yield found


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def check_action_ref(ref: str) -> Optional[str]:
    """Return a violation message for ``uses:`` value ``ref``, else ``None``.

    Local reusable workflows (``./...``) carry no ref and are always allowed;
    they are versioned by the commit that contains them.
    """
    ref = ref.strip()
    if not ref:
        return "empty 'uses:' value"
    if ref.startswith("./") or ref.startswith("../"):
        if "@" in ref:
            return "local reusable workflow {0!r} must not carry an '@ref'".format(ref)
        return None
    if ref.startswith("docker://"):
        _, _, remainder = ref.partition("docker://")
        _, sep, digest = remainder.rpartition("@")
        if not sep or not DOCKER_DIGEST_RE.match(digest):
            return "docker action {0!r} must be pinned to an immutable @sha256: digest".format(ref)
        return None
    match = ACTION_RE.match(ref)
    if match is None:
        return "unrecognised 'uses:' value {0!r}".format(ref)
    pinned = match.group("ref")
    if pinned is None:
        return "action {0!r} has no ref; pin it to a full 40-character commit SHA".format(ref)
    if not SHA_RE.match(pinned):
        return (
            "action {0!r} is pinned to the mutable ref {1!r}; "
            "use a full 40-character commit SHA with a trailing version comment".format(ref, pinned)
        )
    return None


def _triggers(document: Mapping) -> List[str]:
    node = document.get("on")
    if isinstance(node, Mapping):
        return [str(key) for key in node.keys()]
    if isinstance(node, Sequence):
        return [str(item) for item in node]
    if isinstance(node, str):
        return [node]
    return []


def is_reusable_only(document: Mapping) -> bool:
    """True when the workflow is exclusively a reusable ``workflow_call`` target.

    A reusable workflow inherits the caller's run context; giving it its own
    top-level concurrency group would serialise every caller through one slot,
    which is exactly wrong for the Windows runner picker.
    """
    triggers = _triggers(document)
    return bool(triggers) and set(triggers) == {"workflow_call"}


def _is_valid_timeout(value: Any) -> bool:
    if isinstance(value, str) and EXPRESSION_RE.search(value):
        return True
    try:
        return int(str(value).strip()) > 0
    except (TypeError, ValueError):
        return False


MATRIX_REF_RE = re.compile(r"^\$\{\{\s*matrix\.([A-Za-z0-9_.-]+)\s*\}\}$")


def _matrix_values(job: Mapping, key: str) -> Optional[List[Any]]:
    """Every value ``matrix.<key>`` can take, or ``None`` when not static.

    Covers both matrix shapes: a top-level ``<key>: [a, b]`` axis and per-row
    ``include:`` entries. ``None`` means the axis is computed at run time (the
    ``fromJSON(needs.*.outputs.matrix)`` online-inventory pattern), which this
    file cannot resolve and must not pretend to.
    """
    strategy = job.get("strategy")
    if not isinstance(strategy, Mapping):
        return None
    matrix = strategy.get("matrix")
    if not isinstance(matrix, Mapping):
        return None
    values: List[Any] = []
    found = False
    if key in matrix:
        found = True
        axis = matrix[key]
        if isinstance(axis, Sequence):
            values.extend(axis)
        else:
            values.append(axis)
    include = matrix.get("include")
    if isinstance(include, Sequence):
        for entry in include:
            if isinstance(entry, Mapping) and key in entry:
                found = True
                values.append(entry[key])
    return values if found else None


def is_hosted_label(label: str) -> bool:
    """True when ``label`` names a metered GitHub-hosted image or larger runner."""
    lowered = str(label).strip().lower()
    if not lowered or EXPRESSION_RE.search(lowered):
        return False
    if any(marker in lowered for marker in HOSTED_LABEL_MARKERS):
        return True
    return any(lowered.startswith(prefix) for prefix in HOSTED_LABEL_PREFIXES)


def resolve_runs_on(job: Mapping) -> Tuple[List[List[str]], Optional[str]]:
    """Resolve every label set ``job`` can land on.

    Returns ``(candidates, unresolved_reason)``. Each candidate is one flat list
    of labels. An element that is a ``${{ matrix.x }}`` reference is expanded
    from the job's own ``strategy.matrix`` when that axis is static; when it is
    not, the element is dropped and the literal ``self-hosted`` sitting beside
    it in the same array is what keeps the job off metered capacity.
    """
    value = job.get("runs-on")

    if isinstance(value, Mapping):
        if set(value) != {"group", "labels"}:
            return [], "'runs-on' must name a group and explicit self-hosted labels"
        value = value.get("labels")

    if isinstance(value, str):
        ref = MATRIX_REF_RE.match(value.strip())
        if ref is not None:
            resolved = _matrix_values(job, ref.group(1))
            if resolved is None:
                return [], "'runs-on' resolves from matrix.{0}, which this job does not define statically".format(
                    ref.group(1)
                )
            candidates: List[List[str]] = []
            for item in resolved:
                if isinstance(item, Sequence):
                    candidates.append([str(entry) for entry in item])
                else:
                    candidates.append([str(item)])
            return candidates, None
        if EXPRESSION_RE.search(value):
            return [], "'runs-on' is a computed expression with no statically checkable source"
        return [[value]], None

    if isinstance(value, Sequence):
        labels: List[str] = []
        for item in value:
            text = str(item)
            ref = MATRIX_REF_RE.match(text.strip())
            if ref is None:
                labels.append(text)
                continue
            resolved = _matrix_values(job, ref.group(1))
            if resolved is not None:
                labels.extend(str(entry) for entry in resolved)
        return [labels], None

    return [], None


def scalar_runs_on(job: Mapping) -> Optional[str]:
    """Describe a scalar ``runs-on``, or ``None`` when it resolves to label arrays.

    ``runs-on: <string>`` can carry exactly one label. That is the shape every
    accidental ``ubuntu-latest`` arrives in, and even ``runs-on: self-hosted``
    is unsafe on this fleet: a single label routes to *any* registered runner,
    including the production gateway host, and leaves nothing for the hosted
    label check to bite on if the string is later edited. The contract is an
    explicit array naming ``self-hosted`` plus a capacity label.

    A ``${{ matrix.x }}`` reference is fine when the axis holds arrays (the
    CloudCastle pattern); it is rejected when the axis holds bare strings,
    because that is the same one-label routing wearing a matrix.
    """
    value = job.get("runs-on")
    if isinstance(value, Mapping):
        value = value.get("labels")
    if isinstance(value, Sequence) or value is None:
        return None
    text = str(value).strip()
    ref = MATRIX_REF_RE.match(text)
    if ref is None:
        if EXPRESSION_RE.search(text):
            return None  # reported as unresolved-runner-matrix
        return "the literal string {0!r}".format(text)
    resolved = _matrix_values(job, ref.group(1))
    if resolved is None:
        return None  # reported as unresolved-runner-matrix
    scalars = [str(item) for item in resolved if not isinstance(item, Sequence)]
    if scalars:
        return "matrix.{0}, whose non-array value(s) {1} each name one label".format(
            ref.group(1), ", ".join(repr(item) for item in scalars)
        )
    return None

def _condition_parts(expression: str, operator: str) -> Tuple[List[str], bool]:
    """Split a condition at top-level operators, respecting quotes and nesting."""
    parts: List[str] = []
    depth = 0
    start = 0
    i = 0
    outer_end = -1
    while i < len(expression):
        character = expression[i]
        if character in "'\"":
            i = _scan_quoted(expression, i)
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                raise WorkflowParseError("unbalanced condition")
            if depth == 0 and outer_end < 0:
                outer_end = i
        elif depth == 0 and expression.startswith(operator, i):
            parts.append(expression[start:i].strip())
            i += len(operator)
            start = i
            continue
        i += 1
    if depth:
        raise WorkflowParseError("unbalanced condition")
    parts.append(expression[start:].strip())
    return parts, expression.startswith("(") and outer_end == len(expression) - 1


def _condition_on_windows(value: Any, row: Dict[str, Any]) -> Optional[bool]:
    """Prove true/false for the guard subset in use; unknown stays executable.

    This is deliberately not a GitHub expression interpreter. Unknown contexts,
    functions and syntax cannot prove a step excluded from Windows. Boolean
    structure is retained so an OR bypass never masquerades as an OS guard.
    """
    expression = str(value).strip()
    if expression.startswith("${{") and expression.endswith("}}"):
        expression = expression[3:-2].strip()
    try:
        parts, enclosed = _condition_parts(expression, "||")
        if len(parts) > 1:
            values = [_condition_on_windows(part, row) for part in parts]
            return True if True in values else (False if all(v is False for v in values) else None)
        parts, enclosed = _condition_parts(expression, "&&")
        if len(parts) > 1:
            values = [_condition_on_windows(part, row) for part in parts]
            return False if False in values else (True if all(v is True for v in values) else None)
        if enclosed:
            return _condition_on_windows(expression[1:-1], row)
        if expression.startswith("!") and not expression.startswith("!="):
            result = _condition_on_windows(expression[1:], row)
            return None if result is None else not result

        unknown = object()

        def operand(text: str) -> Any:
            if text == "runner.os":
                return "Windows"
            match = re.fullmatch(r"matrix\.([A-Za-z0-9_-]+)", text)
            if match:
                result = row.get(match.group(1), unknown)
                return unknown if (
                    isinstance(result, (Mapping, Sequence))
                    or isinstance(result, str) and EXPRESSION_RE.search(result)
                ) else result
            if text.startswith("'") and _scan_quoted(text, 0) == len(text):
                return text[1:-1].replace("''", "'")
            if text.lower() in ("true", "false"):
                return text.lower() == "true"
            return unknown

        for operator in ("==", "!="):
            parts, _ = _condition_parts(expression, operator)
            if len(parts) == 2:
                left, right = (operand(part) for part in parts)
                if left is unknown or right is unknown or type(left) is not type(right):
                    return None
                equal = left.lower() == right.lower() if isinstance(left, str) else left == right
                return equal if operator == "==" else not equal
            if len(parts) > 2:
                return None
        result = operand(expression)
        if result is unknown:
            return None
        return bool(result)
    except WorkflowParseError:
        return None


def _windows_matrix_contexts(job: Mapping) -> Iterator[Dict[str, Any]]:
    """Retain include-row correlations; unresolved OS labels may mean Windows."""
    strategy = job.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, Mapping) else None
    include = matrix.get("include") if isinstance(matrix, Mapping) else None
    if isinstance(include, Sequence) and not (set(matrix) - {"include", "exclude"}):
        rows = [dict(row) for row in include if isinstance(row, Mapping)]
    else:
        # Cartesian/dynamic matrices remain conservative. Singleton axes are
        # known, but multi-value axes cannot prove a matrix-only guard false.
        rows = [{
            str(key): values[0]
            for key in matrix if key not in ("include", "exclude")
            if (values := _matrix_values(job, key)) is not None and len(values) == 1
        }] if isinstance(matrix, Mapping) else [{}]
    for row in rows:
        routed_job = job
        if isinstance(include, Sequence) and not (set(matrix) - {"include", "exclude"}):
            routed_job = Mapping()
            routed_job.update(job)
            row_matrix = Mapping()
            row_include = Sequence()
            row_mapping = Mapping()
            row_mapping.update(row)
            row_include.append(row_mapping)
            row_matrix["include"] = row_include
            row_strategy = Mapping()
            row_strategy["matrix"] = row_matrix
            routed_job["strategy"] = row_strategy
        candidates, _ = resolve_runs_on(routed_job)
        if not candidates or any(
            "windows" in {label.strip().lower() for label in labels}
            or not ({"linux", "macos"} & {label.strip().lower() for label in labels})
            for labels in candidates
        ):
            yield row


def _default_run_shell(mapping: Mapping) -> Any:
    defaults = mapping.get("defaults")
    run = defaults.get("run") if isinstance(defaults, Mapping) else None
    return run.get("shell") if isinstance(run, Mapping) else None



def _coherent_cancel_is_safe(value: Any) -> bool:
    """Only no cancellation, literal false, or the exact PR-only guard is safe."""
    rendered = str(value).strip()
    return rendered.lower() in ("", "false", "none") or rendered == PR_ONLY_CANCEL_EXPRESSION


def has_trusted_job_guard(value: Any) -> bool:
    """Accept the canonical guard, optionally AND a fully enclosed condition.

    Do not search for guard tokens: ``guard || true`` is not a guard. This
    conservative grammar keeps the policy dependency-free and fail-closed.
    """
    expression = str(value or "").strip()
    if expression.startswith("${{") and expression.endswith("}}"):
        expression = expression[3:-2].strip()
    if expression == TRUSTED_JOB_GUARD:
        return True
    prefix = "(" + TRUSTED_JOB_GUARD + ") && "
    if not expression.startswith(prefix):
        return False
    remainder = expression[len(prefix):].strip()
    if not remainder.startswith("("):
        return False
    depth = 0
    i = 0
    while i < len(remainder):
        character = remainder[i]
        if character in "'\"":
            try:
                i = _scan_quoted(remainder, i)
            except WorkflowParseError:
                return False
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return i == len(remainder) - 1
            if depth < 0:
                return False
        i += 1
    return False


def _scalar_values(node: Any) -> Iterator[str]:
    if isinstance(node, Mapping):
        for value in node.values():
            yield from _scalar_values(value)
    elif isinstance(node, Sequence):
        for value in node:
            yield from _scalar_values(value)
    else:
        yield str(node)


def runner_boundary_findings(
    document: Mapping, job: Mapping, job_id: str, relpath: str,
    root: Optional[Path],
) -> List[Finding]:
    """Check execution/secret boundaries; never downgrade these to advisory."""
    findings: List[Finding] = []

    def add(rule: str, message: str, key: str = "runs-on") -> None:
        findings.append(Finding(rule, ERROR, relpath, job.line_of(key), message))

    if "uses" in job:
        target = str(job["uses"])
        secrets = job.get("secrets")
        if secrets == "inherit":
            add("reusable-secret-boundary", "reusable jobs must pass named secrets, never inherit", "secrets")
        if not target.startswith("./.github/workflows/"):
            add("reusable-secret-boundary", "runner policy only admits checked local reusable workflows", "uses")
        if secrets and not has_trusted_job_guard(job.get("if")):
            add("reusable-secret-boundary", "secret-bearing reusable calls require the protected ref/event guard", "if")
        if root is not None and isinstance(secrets, Mapping) and target.startswith("./"):
            path = Path(root) / target[2:]
            if path.is_file():
                try:
                    callee = parse_workflow(path.read_text(encoding="utf-8"))
                except WorkflowParseError:
                    return findings  # reported on the callee by the repository sweep
                events = callee.get("on")
                call = events.get("workflow_call") if isinstance(events, Mapping) else None
                declared = call.get("secrets") if isinstance(call, Mapping) else None
                undeclared = set(secrets) - (set(declared) if isinstance(declared, Mapping) else set())
                if undeclared:
                    add("reusable-secret-boundary", "callee does not declare secret(s): " + ", ".join(sorted(undeclared)), "secrets")
        return findings

    selector = job.get("runs-on")
    group = selector.get("group") if isinstance(selector, Mapping) else None
    if group not in (UNTRUSTED_RUNNER_GROUP, TRUSTED_RUNNER_GROUP):
        add("runner-trust-boundary", "job " + repr(job_id) + " must select a literal isolated self-hosted runner group")
        return findings
    if group == UNTRUSTED_RUNNER_GROUP:
        values = list(_scalar_values(document.get("env"))) + list(_scalar_values(job))
        if any(re.search(r"\$\{\{[^}]*\bsecrets(?:\.|\[)", value) for value in values) or "secrets" in job or "environment" in job:
            add("untrusted-secret-delivery", "disposable branch/PR jobs must not receive secrets or select an environment", "environment")
        permissions = job.get("permissions", document.get("permissions"))
        if permissions == "write-all" or (
            isinstance(permissions, Mapping) and any(value == "write" for value in permissions.values())
        ):
            add("untrusted-write-permission", "disposable branch/PR jobs require a read-only GITHUB_TOKEN", "permissions")
        return findings

    if not has_trusted_job_guard(job.get("if")):
        add("protected-job-guard", "persistent jobs require the canonical protected ref/event guard", "if")
    if set(_triggers(document)) & {"pull_request", "pull_request_target", "workflow_run"}:
        add("protected-job-guard", "PR and artifact-triggered workflows may not target persistent protected runners", "if")
    environment = job.get("environment")
    name = environment.get("name") if isinstance(environment, Mapping) else environment
    if not isinstance(name, str) or name not in PROTECTED_ENVIRONMENTS:
        add("protected-job-environment", "persistent jobs must select a literal protected environment", "environment")
    steps = job.get("steps")
    if isinstance(steps, Sequence):
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            action = str(step.get("uses", "")).split("@", 1)[0].lower()
            if action in ("actions/download-artifact", "actions/cache") or action.startswith("actions/cache/"):
                add("protected-untrusted-input", "protected jobs may not consume branch/PR Actions artifacts or caches", "steps")
            if action == "actions/checkout":
                options = step.get("with")
                if isinstance(options, Mapping):
                    ref = options.get("ref")
                    if ref is not None and str(ref).strip() != "${{ github.sha }}":
                        add("protected-untrusted-input", "protected checkout must use this guarded event commit, not an arbitrary ref", "steps")
                    if options.get("repository") is not None:
                        add("protected-untrusted-input", "protected checkout must not substitute an external repository", "steps")
    return findings


def check_document(
    document: Mapping,
    *,
    relpath: str,
    severity: str = ERROR,
    root: Optional[Path] = None,
) -> List[Finding]:
    """Apply every structural rule to an already-parsed workflow ``document``."""
    findings: List[Finding] = []

    def add(rule: str, line: int, message: str) -> None:
        findings.append(Finding(rule, severity, relpath, line, message))

    # -- pinned action refs -------------------------------------------------
    for mapping in _walk_mappings(document):
        if "uses" not in mapping:
            continue
        value = mapping["uses"]
        line = mapping.line_of("uses")
        if not isinstance(value, str):
            add("unpinned-action-ref", line, "'uses:' must be a string")
            continue
        problem = check_action_ref(value)
        if problem is not None:
            add("unpinned-action-ref", line, problem)
        elif value.startswith("./") and root is not None:
            target = Path(root) / value[2:]
            local_action = target.is_dir() and (target / "action.yml").is_file()
            if not target.is_file() and not local_action:
                add(
                    "missing-local-workflow",
                    line,
                    "local workflow/action {0!r} does not exist".format(value),
                )
        match = ACTION_RE.match(value)
        if match is not None and match.group("repo").lower() == UPLOAD_ARTIFACT_ACTION:
            options = mapping.get("with")
            retention = options.get("retention-days") if isinstance(options, Mapping) else None
            try:
                days = int(str(retention).strip())
            except (TypeError, ValueError):
                days = None
            if days is None or days <= 0 or days > MAX_ARTIFACT_RETENTION_DAYS:
                add(
                    "billable-artifact-retention",
                    line,
                    "upload-artifact must set 'retention-days' between 1 and {0} (got {1!r}); "
                    "artifact storage above the 500 MB free allowance bills at $0.25/GB-month "
                    "accrued hourly, and the GitHub default is 90 days. Job logs and "
                    "$GITHUB_STEP_SUMMARY are not metered -- put diagnostics there instead "
                    "of uploading them.".format(MAX_ARTIFACT_RETENTION_DAYS, retention),
                )

    # -- ASCII-only PowerShell ----------------------------------------------
    for mapping in _walk_mappings(document):
        shell = mapping.get("shell")
        body = mapping.get("run")
        if not isinstance(shell, str) or not isinstance(body, str):
            continue
        words = shell.strip().split()
        if not words or words[0].lower() not in POWERSHELL_SHELLS:
            continue
        offenders = sorted({character for character in body if ord(character) > 127})
        if offenders:
            rendered = ", ".join(
                "{0!r} (U+{1:04X})".format(character, ord(character)) for character in offenders
            )
            add(
                "powershell-non-ascii",
                mapping.line_of("run"),
                "a '{0}' step body must be pure ASCII; it contains {1}. Windows PowerShell 5.1 "
                "decodes the BOM-less UTF-8 script the runner writes as the ANSI code page, "
                "which turns a UTF-8 em dash into a curly quote that silently terminates a "
                "string -- the step then fails at parse time, several lines away from the "
                "character, before any of it runs.".format(words[0], rendered),
            )

    # -- permissions --------------------------------------------------------
    for mapping in _walk_mappings(document):
        if "permissions" not in mapping:
            continue
        block = mapping["permissions"]
        line = mapping.line_of("permissions")
        if isinstance(block, str):
            if block not in PERMISSION_SHORTHANDS:
                add(
                    "invalid-permission-scope",
                    line,
                    "permissions shorthand {0!r} is not accepted by GitHub".format(block),
                )
            continue
        if not isinstance(block, Mapping):
            add(
                "invalid-permission-scope",
                line,
                "'permissions:' must be a mapping of scopes or a read-all/write-all shorthand",
            )
            continue
        for scope in block.keys():
            scope_line = block.line_of(scope)
            if scope in BANNED_PERMISSIONS:
                add(
                    "banned-permission",
                    scope_line,
                    "'permissions.{0}' is not a valid GITHUB_TOKEN scope; GitHub rejects the "
                    "whole workflow at parse time, so every zero-spend check this gate makes "
                    "over it stops running and a hosted-runner regression lands unnoticed and "
                    "billed. Listing self-hosted runners needs a PAT secret, not a "
                    "permission.".format(scope),
                )
            elif scope not in PERMISSION_SCOPES:
                add(
                    "invalid-permission-scope",
                    scope_line,
                    "'permissions.{0}' is not a permission scope GitHub accepts".format(scope),
                )
            elif block[scope] not in PERMISSION_VALUES:
                add(
                    "invalid-permission-scope",
                    scope_line,
                    "'permissions.{0}' must be read, write, or none (got {1!r})".format(
                        scope, block[scope]
                    ),
                )

    if "permissions" not in document:
        add(
            "missing-permissions",
            document.line,
            "no explicit top-level 'permissions:'; the workflow inherits the repository default",
        )
    elif document.get("permissions") == "write-all":
        add(
            "overbroad-permissions",
            document.line_of("permissions"),
            "top-level 'permissions: write-all' violates least privilege",
        )

    # -- concurrency --------------------------------------------------------
    if not is_reusable_only(document):
        concurrency = document.get("concurrency")
        if "concurrency" not in document:
            add(
                "missing-concurrency",
                document.line,
                "no top-level 'concurrency:'; duplicate pull-request runs will pile up on the "
                "self-hosted pool",
            )
        elif isinstance(concurrency, Mapping):
            if not concurrency.get("group"):
                add(
                    "missing-concurrency",
                    document.line_of("concurrency"),
                    "'concurrency:' has no 'group'",
                )
            if str(concurrency.get("cancel-in-progress")).strip().lower() == "true":
                add(
                    "unsafe-cancel-in-progress",
                    concurrency.line_of("cancel-in-progress"),
                    "'cancel-in-progress: true' cancels main-branch and release runs too; "
                    "gate it on pull requests",
                )
        elif not isinstance(concurrency, str):
            add(
                "missing-concurrency",
                document.line_of("concurrency"),
                "'concurrency:' must be a group name or a mapping",
            )

    # -- coherent release cancellation -------------------------------------
    workflow_name = Path(relpath).name
    if workflow_name in COHERENT_RELEASE_WORKFLOWS:
        concurrency = document.get("concurrency")
        if isinstance(concurrency, Mapping) and "cancel-in-progress" in concurrency:
            value = concurrency["cancel-in-progress"]
            if not _coherent_cancel_is_safe(value):
                add(
                    "coherent-build-cancellation",
                    concurrency.line_of("cancel-in-progress"),
                    "artifact/release workflow cancellation must be false or the exact "
                    "pull-request-only guard; later pushes must not cancel an in-flight coherent build",
                )

    # -- jobs, runner routing, and timeouts ---------------------------------
    jobs = document.get("jobs")
    if not isinstance(jobs, Mapping) or not jobs:
        add("missing-jobs", document.line, "the workflow declares no jobs")
        return findings

    for job_id, job in jobs.items():
        job_line = jobs.line_of(job_id)
        if not isinstance(job, Mapping):
            add("missing-jobs", job_line, "job {0!r} is not a mapping".format(job_id))
            continue
        is_reusable_call = "uses" in job
        findings.extend(runner_boundary_findings(document, job, str(job_id), relpath, root))

        # -- zero-spend runner routing --------------------------------------
        # Everything below exists because this repository is private and
        # GitHub-hosted minutes are metered on private repositories while
        # self-hosted minutes are free. A reusable-workflow call carries no
        # 'runs-on' of its own; the callee is checked on its own terms.
        if not is_reusable_call:
            runs_on_line = job.line_of("runs-on") or job_line
            candidates, unresolved = resolve_runs_on(job)
            windows_rows = list(_windows_matrix_contexts(job))
            steps = job.get("steps")
            default_shell = _default_run_shell(job)
            if default_shell is None:
                default_shell = _default_run_shell(document)
            if isinstance(steps, Sequence):
                for step in steps:
                    if not isinstance(step, Mapping) or "run" not in step:
                        continue
                    shell = step.get("shell", default_shell)
                    if shell == "powershell":
                        continue
                    if any(
                        _condition_on_windows(step.get("if", "true"), row) is not False
                        for row in windows_rows
                    ):
                        add(
                            "incompatible-windows-shell",
                            step.line_of("shell") if "shell" in step else step.line_of("run"),
                            "job {0!r} can execute a run step on Windows with shell {1!r}; "
                            "set shell: powershell (directly or in defaults.run.shell), or "
                            "exclude Windows with a checked OS/matrix guard. These workers "
                            "have Windows PowerShell 5.1, no pwsh, and a bash shim that can "
                            "exit successfully without executing the script. An omitted shell "
                            "does not explicitly select the supported interpreter.".format(job_id, shell),
                        )
            if "runs-on" not in job:
                add(
                    "missing-runs-on",
                    job_line,
                    "job {0!r} declares no 'runs-on'; GitHub cannot schedule it and an added "
                    "default would be a metered hosted runner".format(job_id),
                )
            elif unresolved is not None:
                add(
                    "unresolved-runner-matrix",
                    runs_on_line,
                    "job {0!r} cannot be proven to run self-hosted: {1}. A runner this gate "
                    "cannot read is a runner that can silently become a billed hosted or "
                    "larger runner; list the labels literally, including 'self-hosted'."
                    .format(job_id, unresolved),
                )

            scalar = scalar_runs_on(job)
            if scalar is not None:
                add(
                    "string-form-runs-on",
                    runs_on_line,
                    "job {0!r} sets 'runs-on' to {1}; the contract is an explicit array that "
                    "names 'self-hosted' plus one capacity label. A bare string is the exact "
                    "shape an accidental 'ubuntu-latest' arrives in, and GitHub-hosted minutes "
                    "are billed on this private repository while the self-hosted fleet is "
                    "free.".format(job_id, scalar),
                )

            for candidate in candidates:
                lowered = frozenset(label.strip().lower() for label in candidate)
                billed = sorted({label for label in candidate if is_hosted_label(label)})
                if billed:
                    add(
                        "hosted-runner",
                        runs_on_line,
                        "job {0!r} requests GitHub-hosted/larger runner label(s) {1}; those "
                        "minutes are billed on a private repository while self-hosted minutes "
                        "are free. Chudware CI spends $0 -- use the self-hosted fleet."
                        .format(job_id, ", ".join(billed)),
                    )
                elif SELF_HOSTED_LABEL not in lowered:
                    add(
                        "hosted-runner",
                        runs_on_line,
                        "job {0!r} has no literal 'self-hosted' label in its 'runs-on' ({1}); "
                        "any runner that is not self-hosted is metered on a private repository."
                        .format(job_id, ", ".join(candidate) or "empty"),
                    )

                dedicated = lowered & DEDICATED_RUNNER_LABELS
                allowed = DEDICATED_RUNNER_EXCEPTIONS.get(
                    (workflow_name, str(job_id)), frozenset()
                )
                unexpected = sorted(dedicated - allowed)
                if unexpected:
                    add(
                        "dedicated-runner-misuse",
                        runs_on_line,
                        "job {0!r} may not use dedicated infrastructure label(s) {1}; only exact "
                        "production publish/control jobs are exempt".format(
                            job_id, ", ".join(unexpected)
                        ),
                    )

                generic_linux = {"self-hosted", "linux", "x64", "cloudcastle-ci"}
                # A job carrying an ALLOWED dedicated label is already pinned to
                # exactly one endpoint, so "can land on any box in the pool" is
                # false for it. Only an unexempted dedicated label is a finding,
                # and that is what dedicated-runner-misuse above reports.
                pinned_to_allowed_endpoint = bool(dedicated) and not unexpected
                if (
                    generic_linux <= lowered
                    and not (lowered & LINUX_ENDPOINT_LABELS)
                    and not pinned_to_allowed_endpoint
                ):
                    add(
                        "unsafe-generic-linux-routing",
                        runs_on_line,
                        "self-hosted Linux job {0!r} can land on any box in the pool, including "
                        "the production gateway host; add exactly one capacity label ({1})"
                        .format(job_id, ", ".join(sorted(LINUX_ENDPOINT_LABELS))),
                    )

        job_concurrency = job.get("concurrency")
        if (
            workflow_name in COHERENT_RELEASE_WORKFLOWS
            and isinstance(job_concurrency, Mapping)
            and "cancel-in-progress" in job_concurrency
            and not _coherent_cancel_is_safe(job_concurrency["cancel-in-progress"])
        ):
            add(
                "coherent-build-cancellation",
                job_concurrency.line_of("cancel-in-progress"),
                "job {0!r} may cancel a coherent release operation; use false or the exact "
                "pull-request-only guard".format(job_id),
            )

        if is_reusable_call:
            # A job that calls a reusable workflow cannot carry
            # ``timeout-minutes``; the callee owns its own timeouts.
            continue
        if "timeout-minutes" not in job:
            add(
                "missing-job-timeout",
                job_line,
                "job {0!r} has no 'timeout-minutes'; a hung self-hosted job holds the runner "
                "for the six-hour default".format(job_id),
            )
        elif not _is_valid_timeout(job["timeout-minutes"]):
            add(
                "missing-job-timeout",
                job.line_of("timeout-minutes"),
                "job {0!r} has a non-positive 'timeout-minutes' ({1!r})".format(
                    job_id, job["timeout-minutes"]
                ),
            )

    return findings


def check_workflow(path: Path, *, severity: str = ERROR, root: Optional[Path] = None) -> List[Finding]:
    """Check a single workflow file. ``path`` may point anywhere, including fixtures."""
    path = Path(path)
    relpath = _relpath(path, root)
    if not path.is_file():
        return [Finding("missing-workflow", severity, relpath, 0, "workflow file does not exist")]
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return [Finding("empty-workflow", severity, relpath, 0, "workflow file is empty")]
    try:
        document = parse_workflow(text)
    except WorkflowParseError as exc:
        return [
            Finding(
                "workflow-parse-error",
                severity,
                relpath,
                exc.line,
                "workflow does not parse: {0}".format(exc.message),
            )
        ]
    return check_document(document, relpath=relpath, severity=severity, root=root)


def _relpath(path: Path, root: Optional[Path]) -> str:
    path = Path(path)
    if root is not None:
        try:
            return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def check_repository(
    root: Path,
    *,
    workflows_dir: Optional[Path] = None,
    required: Iterable[str] = REQUIRED_WORKFLOWS,
    scope: Iterable[str] = POLICY_WORKFLOWS,
    check_required_set: bool = True,
    scope_all: bool = False,
) -> List[Finding]:
    """Check every workflow under ``root``. Every path argument accepts fixtures.

    Workflows named in ``scope`` are enforced at ``error`` severity. Anything
    else in the directory is reported as ``advisory`` unless ``scope_all`` is
    set, so this gate never blocks a merge on a workflow the CI contract does
    not own -- while still surfacing the finding.
    """
    root = Path(root)
    directory = Path(workflows_dir) if workflows_dir is not None else root / WORKFLOW_DIR
    required_set = tuple(required)
    scope_set = set(scope)
    findings: List[Finding] = []

    if check_required_set:
        for name in required_set:
            candidate = directory / name
            if not candidate.is_file():
                findings.append(
                    Finding(
                        "missing-workflow",
                        ERROR,
                        _relpath(candidate, root),
                        0,
                        "required workflow is missing; the CI contract names it as load-bearing",
                    )
                )
            elif not candidate.read_text(encoding="utf-8").strip():
                findings.append(
                    Finding(
                        "empty-workflow",
                        ERROR,
                        _relpath(candidate, root),
                        0,
                        "required workflow exists but has no content",
                    )
                )

    present = sorted(
        path
        for path in directory.glob("*")
        if path.is_file() and path.suffix in (".yml", ".yaml")
    )
    for path in present:
        if check_required_set and path.name in required_set:
            if not path.read_text(encoding="utf-8").strip():
                continue  # already reported as empty-workflow
        severity = ERROR if (scope_all or path.name in scope_set) else ADVISORY
        findings.extend(check_workflow(path, severity=severity, root=root))

    return findings


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _default_root() -> Path:
    return Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify Chudware CI workflow policy (no third-party dependencies).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=_default_root(),
        help="repository root to check (default: the repository containing this script)",
    )
    parser.add_argument(
        "--workflows",
        type=Path,
        default=None,
        help="workflow directory (default: <root>/.github/workflows)",
    )
    parser.add_argument(
        "--scope-all",
        action="store_true",
        help="enforce every discovered workflow at error severity, not just the contract set",
    )
    parser.add_argument(
        "--skip-required-set",
        action="store_true",
        help="do not assert that the contract-named workflow set exists (for fixture roots)",
    )
    parser.add_argument(
        "--no-annotations",
        action="store_true",
        help="never emit ::error:: workflow command annotations",
    )
    return parser


def main(argv: Optional[Seq[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    findings = check_repository(
        args.root,
        workflows_dir=args.workflows,
        check_required_set=not args.skip_required_set,
        scope_all=args.scope_all,
    )

    annotate = not args.no_annotations and os.environ.get("GITHUB_ACTIONS") == "true"
    errors = [finding for finding in findings if finding.severity == ERROR]
    advisories = [finding for finding in findings if finding.severity != ERROR]

    for finding in errors + advisories:
        print(finding.render())
        if annotate:
            print(finding.annotation())

    print(
        "\nworkflow policy: {0} error(s), {1} advisory finding(s)".format(
            len(errors), len(advisories)
        )
    )
    if errors:
        return 1
    print("workflow policy: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
