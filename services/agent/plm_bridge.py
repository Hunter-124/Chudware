"""PLM bridge — agent milestone checkpoints, ledger-annotated history.

Contract: contracts/advisor.md §15 (aicad.plmlink/1, FROZEN).
Vault API: contracts/plm.md (aicad.plm/1); matched here
against the plm_checkpoint / plm_history envelopes in §7.

Design decisions
----------------
* Thin ``VaultClient`` Protocol — realignment to the frozen plm.md is a
  one-file change.
* Enablement precedence (default OFF):
    1. project manifest ``ai_mode == "managed"`` → real PLMVaultClient
       (agent CAD-touching milestones auto-checkpoint; no env needed)
    2. ``CHUDWARE_PLM_BRIDGE=1`` → real PLMVaultClient
    3. otherwise → MockVault (safe offline default)
  Reachable without the env flag via PLM Settings → AI assistance → Managed
  (``plm_set_ai_mode`` / worker twin already round-trip the tier).
* Trigger config table: meaningful buildgraph / verification milestones plus
  the four typed design-pipeline boundaries (decompose → buildpath).
* Offline queue: plmlink events persist in the ledger (kind="plmlink");
  a flush routine runs on availability.  Idempotency key:
  (run_id, buildgraph_node, trigger) for build triggers, audit_id for
  change_request.  A "plmlink_flushed" marker event prevents double-flush.
* Verbatim-splice policy: dimension rationales come from ledger.events
  (NOT ledger.state — provenance is stripped from materialized state).
  Decision rationale text is copied verbatim without paraphrasing.
* Locked intent: NO git vocabulary in user-facing messages;
  Project/History/Checkpoint nouns only.  Message vocabulary test included
  in test_plm_bridge.py.
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Trigger configuration table (normative)
# ---------------------------------------------------------------------------

# Which (trigger, stage_exit_value) pairs create a PLM checkpoint by default.
# Besides verification milestones, the design pipeline checkpoints the four typed design
# stages whose ledger slices form the Project History design narrative.
CHECKPOINT_TRIGGERS: dict[str, bool] = {
    "plan_ratified": True,
    "part_done": True,
    "mission_done": True,
    "change_request": True,
    "stage_exit:decompose": True,
    "stage_exit:geometry": True,
    "stage_exit:linkage": True,
    "stage_exit:buildpath": True,
    "stage_exit:part_verify": True,
    "stage_exit:assembly": True,
    "stage_exit:sim": True,
    # Low-signal legacy stages remain suppressed.
    "stage_exit:sketch": False,
    "stage_exit:model": False,
    "stage_exit:layout": False,
    "stage_exit:planning": False,
}

# Forbidden words in user-facing checkpoint messages (contracts/plm.md §8.1)
_GIT_VOCAB = frozenset({
    "git", "commit", "repository", "repo", "branch", "merge",
    "rebase", "head", "ref", "stage", "staging", "push", "pull",
    "sha", "hash",
})

SCHEMA = "aicad.plmlink/1"

# ---------------------------------------------------------------------------
# VaultClient Protocol — thin seam to services/plm/
# ---------------------------------------------------------------------------


@runtime_checkable
class VaultClient(Protocol):
    """Minimal surface the bridge uses from the PLM vault (aicad.plm/1 §7).

    Implemented by MockVault (tests/feature-flag-off) and by the real thin
    wrapper around services/plm/ (``services.plm.client.PLMVaultClient``).
    """

    def is_available(self) -> bool:
        """Return True when the vault is reachable and functional."""
        ...

    def checkpoint(
        self,
        project_id: str,
        message: str,
        item_id: str | None = None,
        author_kind: str = "ai",
        run_id: str | None = None,
        artifact_id: str | None = None,
        item_artifacts: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create a checkpoint history entry.

        Returns the plm_checkpoint success envelope:
            {ok, commit, entry: {commit, kind, authored_at, author, message}}
        or {ok:false, reason, hint} on failure.
        """
        ...

    def project_id_for_run(self, run_id: str) -> str | None:
        """Return the project_id associated with this run, or None."""
        ...


# ---------------------------------------------------------------------------
# MockVault — used when the bridge is not enabled (default)
# ---------------------------------------------------------------------------

class MockVault:
    """In-memory mock matching the VaultClient Protocol.

    Records checkpoint calls so tests can assert round-trips without
    a real PLM vault.
    """

    def __init__(self) -> None:
        self._checkpoints: list[dict[str, Any]] = []
        self._project_map: dict[str, str] = {}  # run_id -> project_id

    def is_available(self) -> bool:
        return True  # mock is always available

    def checkpoint(
        self,
        project_id: str,
        message: str,
        item_id: str | None = None,
        author_kind: str = "ai",
        run_id: str | None = None,
        artifact_id: str | None = None,
        item_artifacts: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        effective_artifact = (
            (item_artifacts or {}).get(item_id, artifact_id or "")
            if item_id is not None
            else artifact_id or ""
        )
        commit = "mock_" + uuid.uuid4().hex[:8]
        entry = {
            "commit": commit,
            "kind": "checkpoint",
            "authored_at": _now(),
            "author": {"kind": author_kind, "id": "agent", "display": "AI Agent"},
            "message": message,
            "ai": {
                "provider": "anthropic",
                "model": "claude-sonnet-5-5",
                "run_id": run_id or "",
                "artifact_id": effective_artifact,
                "reversible": True,
            } if author_kind == "ai" else None,
        }
        self._checkpoints.append({
            "project_id": project_id,
            "item_id": item_id,
            "item_artifacts": dict(item_artifacts or {}),
            "entry": entry,
        })
        return {"ok": True, "commit": commit, "entry": entry}

    def project_id_for_run(self, run_id: str) -> str | None:
        return self._project_map.get(run_id)

    def register_project(self, run_id: str, project_id: str) -> None:
        """Test helper: associate a run_id with a project."""
        self._project_map[run_id] = project_id

    @property
    def checkpoints(self) -> list[dict[str, Any]]:
        return list(self._checkpoints)


# ---------------------------------------------------------------------------
# Feature-flag factory
# ---------------------------------------------------------------------------

def bridge_enabled_by_env() -> bool:
    """True when ``CHUDWARE_PLM_BRIDGE=1`` (case/whitespace-tolerant)."""
    return os.environ.get("CHUDWARE_PLM_BRIDGE", "").strip() == "1"


def _project_dir_is_managed(project_dir: str | os.PathLike | None) -> bool:
    """True when *project_dir* has a vault with ``ai_mode == managed``."""
    if not project_dir:
        return False
    root = os.fspath(project_dir)
    if not root or not os.path.isdir(root):
        return False
    try:
        from services.plm.vault import Vault  # type: ignore[import]

        v = Vault.open(root)
        try:
            return v.ai_mode() == "managed"
        finally:
            v.close()
    except Exception:
        return False


def bridge_should_use_real(
    project_dir: str | os.PathLike | None = None,
) -> bool:
    """Enablement gate: managed ai_mode > CHUDWARE_PLM_BRIDGE env > off.

    Default remains OFF. Managed mode is reachable from PLM Settings without
    setting the env var; the env flag forces the real client for any tier
    (the client still refuses AI-authored cuts unless the project is managed).
    """
    if _project_dir_is_managed(project_dir):
        return True
    return bridge_enabled_by_env()


def _default_vault(
    project_dir: str | os.PathLike | None = None,
) -> VaultClient:
    """Return the vault client selected by the enablement gate.

    Precedence: managed ai_mode > CHUDWARE_PLM_BRIDGE=1 > MockVault.
    Uses the process-wide :func:`services.plm.client.shared_client` so
    ``register_run`` / ``register_project`` stick across bridge instances.
    """
    if bridge_should_use_real(project_dir):
        try:
            from services.plm.client import shared_client  # type: ignore[import]

            return shared_client()
        except ImportError:
            pass  # fall back to mock
    return MockVault()


def bridge_for_run(
    run_id: str,
    *,
    project_dir: str | os.PathLike | None = None,
    ledger: Any | None = None,
    model_id: str = "claude-sonnet-5-5",
    provider: str = "anthropic",
    vault: VaultClient | None = None,
) -> "PLMBridge":
    """Construct a bridge for *run_id*, binding the project when enabled.

    When the enablement gate is open (managed ai_mode or env flag), registers
    ``run_id`` ↔ project on the shared PLMVaultClient so milestones cut real
    vault checkpoints. Best-effort — never raises into the agent loop.
    """
    resolved_dir = project_dir
    if resolved_dir is None:
        try:
            from services.agent import config as agent_config  # type: ignore[import]

            wd = agent_config.workspace_dir()
            if wd is not None:
                resolved_dir = str(wd)
        except Exception:
            resolved_dir = None

    if vault is None:
        vault = _default_vault(resolved_dir)

    # Bind run↔project on real clients so project_id_for_run resolves.
    if resolved_dir and hasattr(vault, "bind_run"):
        try:
            vault.bind_run(run_id, resolved_dir)  # type: ignore[attr-defined]
        except Exception:
            pass
    elif resolved_dir and hasattr(vault, "register_project_dir"):
        try:
            pid = vault.register_project_dir(resolved_dir)  # type: ignore[attr-defined]
            if pid and hasattr(vault, "register_run"):
                vault.register_run(run_id, pid)  # type: ignore[attr-defined]
        except Exception:
            pass

    bridge = PLMBridge(
        run_id=run_id,
        vault=vault,
        model_id=model_id,
        provider=provider,
    )
    if ledger is not None:
        bridge.attach_ledger(ledger)
    return bridge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _trigger_enabled(trigger: str, stage: str | None = None) -> bool:
    """Return True when this trigger should create a checkpoint by default."""
    if trigger == "stage_exit" and stage:
        key = f"stage_exit:{stage}"
        return CHECKPOINT_TRIGGERS.get(key, False)
    return CHECKPOINT_TRIGGERS.get(trigger, False)


def _dedup_key(
    run_id: str,
    trigger: str,
    buildgraph_node: str | None,
    audit_id: str | None,
) -> str:
    """Stable idempotency key for a plmlink event."""
    if trigger == "change_request" and audit_id:
        return f"cr:{audit_id}"
    return f"{run_id}:{buildgraph_node or ''}:{trigger}"


def _scan_git_vocab(text: str) -> list[str]:
    """Return any forbidden git-vocabulary words found in ``text``."""
    lower = text.lower()
    return [w for w in _GIT_VOCAB if w in lower.split()]


# ---------------------------------------------------------------------------
# Message drafting (verbatim-splice policy)
# ---------------------------------------------------------------------------

def draft_checkpoint_message(
    ledger_events: list[dict[str, Any]],
    trigger: str,
    stage: str | None = None,
) -> str:
    """Draft a plain-language checkpoint message from ledger event provenance.

    Verbatim-splice policy (contract §15 + the PLM no-git-vocabulary rule):
    - Decision rationale text is copied VERBATIM from the event payload.
    - Dimension provenance is copied from ledger.events (not ledger.state,
      which strips the provenance field).
    - Numerals in the message must originate from ledger fields only —
      no hand-typed numbers.
    - No git vocabulary anywhere in the output.
    """
    # Collect the typed design story, rationale, and dimension provenance since
    # the previous checkpoint. All user-visible numerals originate in payloads.
    feature_parts: list[str] = []
    geometry_parts: list[str] = []
    linkage_parts: list[str] = []
    build_parts: list[str] = []
    rationale_parts: list[str] = []
    dimension_parts: list[str] = []

    for ev in ledger_events:
        kind = ev.get("kind", "")
        payload = ev.get("payload", {})

        if kind == "feature":
            feature_id = str(payload.get("id", "")).strip()
            function = str(payload.get("function", "")).strip()
            if function:
                feature_parts.append(
                    f"{feature_id}: {function}" if feature_id else function
                )

        elif kind == "geometry_spec":
            spec_id = str(payload.get("id", "")).strip()
            ops = [
                str(shape.get("op", "")).strip()
                for shape in (payload.get("shapes") or [])
                if isinstance(shape, dict) and str(shape.get("op", "")).strip()
            ]
            realizes = [
                str(value) for value in (payload.get("realizes") or [])
                if str(value).strip()
            ]
            provenance = str(payload.get("provenance", "")).strip()
            details: list[str] = []
            if ops:
                details.append("uses " + ", ".join(ops))
            if realizes:
                details.append("realizes " + ", ".join(realizes))
            if provenance:
                details.append(provenance)
            if details:
                prefix = f"{spec_id}: " if spec_id else ""
                geometry_parts.append(prefix + "; ".join(details))

        elif kind == "linkage":
            for edge in payload.get("reference_edges") or []:
                if not isinstance(edge, dict):
                    continue
                source = str(edge.get("from", "")).strip()
                target = str(edge.get("to", "")).strip()
                relation = str(edge.get("relation", "")).strip()
                why = str(edge.get("why", "")).strip()
                relation_text = " → ".join(v for v in (source, target) if v)
                if relation:
                    relation_text += (": " if relation_text else "") + relation
                if why:
                    relation_text += (" — " if relation_text else "") + why
                if relation_text:
                    linkage_parts.append(relation_text)
            for interface in payload.get("shared_interfaces") or []:
                if not isinstance(interface, dict):
                    continue
                features = ", ".join(
                    str(value) for value in (interface.get("features") or [])
                    if str(value).strip()
                )
                location = str(interface.get("on", "")).strip()
                agreement = str(interface.get("must_agree", "")).strip()
                text = features
                if location:
                    text += (" on " if text else "") + location
                if agreement:
                    text += (": " if text else "") + agreement
                if text:
                    linkage_parts.append(text)

        elif kind == "build_node":
            node_id = str(payload.get("id", "")).strip()
            op = str(payload.get("op", "")).strip()
            elements = ", ".join(
                str(value) for value in (payload.get("elements") or [])
                if str(value).strip()
            )
            why = str(payload.get("why", "")).strip()
            details = op
            if elements:
                details += (" builds " if details else "") + elements
            if why:
                details += (" — " if details else "") + why
            if details:
                build_parts.append(f"{node_id}: {details}" if node_id else details)

        elif kind == "decision":
            rationale = payload.get("rationale", "")
            text = payload.get("text", "")
            if rationale:
                rationale_parts.append(rationale)  # verbatim
            elif text:
                rationale_parts.append(text)  # verbatim

        elif kind == "dimension":
            # provenance lives ONLY in the event payload, not in materialized state
            name = payload.get("name", "")
            value = payload.get("value", "")
            unit = payload.get("unit", "")
            provenance = payload.get("provenance", "")
            if name and value != "":
                dim_str = f"{name}: {value} {unit}".strip()
                if provenance:
                    dim_str += f" ({provenance})"
                dimension_parts.append(dim_str)

    parts: list[str] = []
    if trigger == "plan_ratified":
        parts.append("Project plan approved.")
    elif trigger == "part_done":
        parts.append("Part modeling complete.")
    elif trigger == "mission_done":
        parts.append("Mission complete — all parts finished.")
    elif trigger == "change_request":
        parts.append("Change request applied.")
    elif trigger == "stage_exit" and stage:
        stage_label = {
            "decompose": "Functional decomposition complete.",
            "geometry": "Geometry realization complete.",
            "linkage": "Linkage definition complete.",
            "buildpath": "Build path planning complete.",
            "part_verify": "Part verification passed.",
            "assembly": "Assembly verification passed.",
            "sim": "Simulation passed.",
        }.get(stage, f"{stage.replace('_', ' ').title()} complete.")
        parts.append(stage_label)

    if feature_parts:
        parts.append("Features: " + " | ".join(feature_parts))
    if geometry_parts:
        parts.append("Geometry: " + " | ".join(geometry_parts))
    if linkage_parts:
        parts.append("Linkage: " + " | ".join(linkage_parts))
    if build_parts:
        parts.append("Build path: " + " | ".join(build_parts))
    if dimension_parts:
        parts.append("Dimensions: " + "; ".join(dimension_parts) + ".")
    if rationale_parts:
        parts.append("Rationale: " + " | ".join(rationale_parts))
    if not parts:
        parts.append("Checkpoint recorded.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# PLM Bridge — the main class
# ---------------------------------------------------------------------------

class PLMBridge:
    """Emit PLM checkpoint links from agent milestone events.

    Lifecycle
    ---------
    1. At run start, attach a ledger: ``bridge.attach_ledger(ledger)``.
    2. On milestone events (plan_ratified / part_done / mission_done /
       stage_exit / change_request), call ``bridge.on_milestone(...)``.
    3. Offline queue persists in the ledger; ``bridge.flush()`` drains it
       to the vault when available.

    The bridge never surfaces git concepts to users.  All user-facing text
    uses the PLM user nouns: Project, History, Checkpoint.
    """

    def __init__(
        self,
        run_id: str,
        vault: VaultClient | None = None,
        model_id: str = "claude-sonnet-5-5",
        provider: str = "anthropic",
        project_dir: str | os.PathLike | None = None,
    ) -> None:
        self.run_id = run_id
        self._vault: VaultClient = (
            vault if vault is not None else _default_vault(project_dir)
        )
        self._model_id = model_id
        self._provider = provider
        self._ledger: Any | None = None  # set via attach_ledger()
        self._project_dir = (
            os.fspath(project_dir) if project_dir is not None else None
        )

    def attach_ledger(self, ledger: Any) -> None:
        """Attach the design ledger for this run."""
        self._ledger = ledger

    # ------------------------------------------------------------------
    # Milestone entry point
    # ------------------------------------------------------------------

    def on_milestone(
        self,
        trigger: str,
        buildgraph_node: str | None = None,
        stage: str | None = None,
        audit_id: str | None = None,
        modifies_checkpoint: str | None = None,
        item_id: str | None = None,
        item_artifacts: dict[str, str] | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Called when a pipeline milestone fires.

        Args:
            trigger:            One of the CHECKPOINT_TRIGGERS keys.
            buildgraph_node:    The bg_... node id from the buildgraph executor.
            stage:              For stage_exit triggers, the stage name.
            audit_id:           For change_request triggers, the audit id.
            modifies_checkpoint: The checkpoint the CR modifies (for timeline).
            item_id:            Optional PLM item_id for per-part checkpoints.
            item_artifacts:     Per-item buildgraph artifact ids for multi-part
                                attribution in each item's timeline metadata.
            project_id:         PLM project_id; looked up via vault if None.

        Returns the aicad.plmlink/1 record if a checkpoint was created, or
        None when the trigger is disabled / the vault is unavailable and the
        link is queued.
        """
        if not _trigger_enabled(trigger, stage):
            return None

        # Resolve project
        resolved_project_id = project_id
        if resolved_project_id is None and self._vault is not None:
            resolved_project_id = self._vault.project_id_for_run(self.run_id)

        # Assist-tier gate (docs/design/PLM_VERSION_CONTROL_PLAN.md §2): the
        # copilot cuts checkpoints BY ITSELF only in AI-managed mode. When the vault can report the
        # project's tier and it is not "managed", do nothing — no cut, no
        # queue (the user has not opted into AI-cut entries). Vaults that
        # cannot report a tier (mock/legacy) keep the previous behavior.
        if resolved_project_id and not self._managed_mode_allows(resolved_project_id):
            return None

        dedup = _dedup_key(self.run_id, trigger, buildgraph_node, audit_id)

        # Build plmlink record (§15 schema)
        ledger_event_seqs = self._collect_ledger_seqs_since_last_checkpoint()
        message = draft_checkpoint_message(
            self._events_since_last_checkpoint(),
            trigger=trigger,
            stage=stage,
        )

        # User-facing vocabulary guard (no git terms)
        bad_words = _scan_git_vocab(message)
        if bad_words:
            # Replace message — never emit forbidden vocab
            message = "Checkpoint recorded."

        plmlink: dict[str, Any] = {
            "schema": SCHEMA,
            "checkpoint_ref": None,  # filled after vault call
            "run_id": self.run_id,
            "buildgraph_node": buildgraph_node or "",
            "trigger": trigger if trigger != "stage_exit" else f"stage_exit:{stage or ''}",
            "message": message,
            "ledger_events": ledger_event_seqs,
            "item_id": item_id,
            "item_artifacts": dict(item_artifacts or {}),
            "change_request": {
                "audit_id": audit_id or "",
                "modifies_checkpoint": modifies_checkpoint,
            },
            "dedup_key": dedup,
            "queued": False,
        }

        # Attempt vault call
        if self._vault is not None and self._vault.is_available() and resolved_project_id:
            checkpoint_args: dict[str, Any] = {
                "project_id": resolved_project_id,
                "message": message,
                "item_id": item_id,
                "author_kind": "ai",
                "run_id": self.run_id,
                "artifact_id": buildgraph_node or "",
            }
            if item_artifacts:
                checkpoint_args["item_artifacts"] = dict(item_artifacts)
            result = self._vault.checkpoint(**checkpoint_args)
            if result.get("ok"):
                entry = result.get("entry", {})
                plmlink["checkpoint_ref"] = entry.get("commit", "")
                plmlink["queued"] = False
                self._record_plmlink(plmlink)
                self._record_flushed_marker(dedup)
                return plmlink
            # Vault returned an error — queue offline
        else:
            # Vault unavailable or no project — queue offline
            pass

        plmlink["queued"] = True
        self._record_plmlink(plmlink)
        return plmlink

    def _managed_mode_allows(self, project_id: str) -> bool:
        """True unless the vault reports a non-managed assist tier.

        Duck-typed: vaults without ``ai_mode_for_project`` (MockVault, older
        clients) cannot report a tier, so the gate stays open for them.
        """
        probe = getattr(self._vault, "ai_mode_for_project", None)
        if not callable(probe):
            return True
        try:
            mode = probe(project_id)
        except Exception:
            return True
        return mode is None or mode == "managed"

    # ------------------------------------------------------------------
    # Offline flush
    # ------------------------------------------------------------------

    def flush(self, project_id: str | None = None) -> dict[str, Any]:
        """Flush all queued plmlink events to the vault.

        Idempotent: events already marked as flushed (via plmlink_flushed
        marker in ledger) are skipped.  Partial flushes are safe.

        Returns summary: {flushed: int, skipped: int, failed: int}.
        """
        if self._vault is None or not self._vault.is_available():
            return {"flushed": 0, "skipped": 0, "failed": 0, "error": "vault_unavailable"}

        resolved_project_id = project_id
        if resolved_project_id is None:
            resolved_project_id = self._vault.project_id_for_run(self.run_id)

        if not resolved_project_id:
            return {"flushed": 0, "skipped": 0, "failed": 0, "error": "no_project_id"}

        if not self._managed_mode_allows(resolved_project_id):
            return {"flushed": 0, "skipped": 0, "failed": 0, "error": "ai_managed_off"}

        # Collect already-flushed dedup keys
        flushed_keys = self._flushed_dedup_keys()

        # Collect queued plmlink events
        queued = self._queued_plmlinks()

        flushed = 0
        skipped = 0
        failed = 0

        for record in queued:
            dedup = record.get("dedup_key", "")
            if dedup in flushed_keys:
                skipped += 1
                continue

            message = record.get("message", "Checkpoint recorded.")
            buildgraph_node = record.get("buildgraph_node", "")

            checkpoint_args: dict[str, Any] = {
                "project_id": resolved_project_id,
                "message": message,
                "item_id": record.get("item_id"),
                "author_kind": "ai",
                "run_id": self.run_id,
                "artifact_id": buildgraph_node,
            }
            if record.get("item_artifacts"):
                checkpoint_args["item_artifacts"] = dict(record["item_artifacts"])
            result = self._vault.checkpoint(**checkpoint_args)
            if result.get("ok"):
                # Update record with resolved checkpoint_ref
                entry = result.get("entry", {})
                record["checkpoint_ref"] = entry.get("commit", "")
                record["queued"] = False
                # Persist the resolved record and flush marker
                self._record_plmlink(record)
                self._record_flushed_marker(dedup)
                flushed_keys.add(dedup)
                flushed += 1
            else:
                failed += 1

        return {"flushed": flushed, "skipped": skipped, "failed": failed}

    # ------------------------------------------------------------------
    # Ledger persistence helpers
    # ------------------------------------------------------------------

    def _record_plmlink(self, plmlink: dict[str, Any]) -> None:
        """Persist a plmlink record in the ledger (kind='plmlink')."""
        if self._ledger is None:
            return
        self._ledger.append(
            actor="plm_bridge",
            kind="plmlink",
            payload=dict(plmlink),
        )

    def _record_flushed_marker(self, dedup_key: str) -> None:
        """Record a flush marker so idempotent flush can skip this key."""
        if self._ledger is None:
            return
        self._ledger.append(
            actor="plm_bridge",
            kind="plmlink_flushed",
            payload={"dedup_key": dedup_key, "ts": _now()},
        )

    def _flushed_dedup_keys(self) -> set[str]:
        """Collect all dedup keys already marked as flushed in the ledger."""
        if self._ledger is None:
            return set()
        return {
            ev["payload"]["dedup_key"]
            for ev in self._ledger.events
            if ev.get("kind") == "plmlink_flushed"
        }

    def _queued_plmlinks(self) -> list[dict[str, Any]]:
        """Return all queued (unflushed) plmlink records from ledger events."""
        if self._ledger is None:
            return []
        return [
            dict(ev["payload"])
            for ev in self._ledger.events
            if ev.get("kind") == "plmlink" and ev.get("payload", {}).get("queued", False)
        ]

    def _collect_ledger_seqs_since_last_checkpoint(self) -> list[int]:
        """Return event seq numbers since the last plmlink_flushed marker."""
        if self._ledger is None:
            return []
        last_flush_seq = -1
        for ev in self._ledger.events:
            if ev.get("kind") == "plmlink_flushed":
                last_flush_seq = ev.get("seq", -1)
        return [
            ev["seq"]
            for ev in self._ledger.events
            if ev.get("seq", -1) > last_flush_seq
            and ev.get("kind") not in ("plmlink", "plmlink_flushed")
        ]

    def _events_since_last_checkpoint(self) -> list[dict[str, Any]]:
        """Return raw ledger events since the last flushed checkpoint."""
        if self._ledger is None:
            return []
        last_flush_seq = -1
        for ev in self._ledger.events:
            if ev.get("kind") == "plmlink_flushed":
                last_flush_seq = ev.get("seq", -1)
        return [
            ev
            for ev in self._ledger.events
            if ev.get("seq", -1) > last_flush_seq
            and ev.get("kind") not in ("plmlink", "plmlink_flushed")
        ]


# ---------------------------------------------------------------------------
# Buildgraph executor hook
# ---------------------------------------------------------------------------

def make_buildgraph_node_emit_hook(bridge: "PLMBridge", graph: dict[str, Any]) -> Any:
    """Like make_buildgraph_emit_hook but has access to the graph node dicts.

    Preferred over make_buildgraph_emit_hook when the graph is available
    at hook construction time (gives accurate kind/label lookup).
    """
    node_map: dict[str, dict[str, Any]] = {
        n["id"]: n for n in graph.get("nodes", [])
    }

    def _emit(event_type: str, payload: dict[str, Any]) -> None:
        if event_type != "buildgraph.node.done":
            return
        node_id = payload.get("node_id", "")
        node = node_map.get(node_id, {})
        node_kind = node.get("kind", "")
        node_label = node.get("label", "")

        if node_kind != "checkpoint":
            return

        trigger: str
        if node_label == "mission_done" or "mission" in node_label:
            trigger = "mission_done"
        else:
            trigger = "part_done"

        bridge.on_milestone(
            trigger=trigger,
            buildgraph_node=node_id,
        )

    return _emit


# ---------------------------------------------------------------------------
# Agent-turn seam — managed-mode vault checkpoint on CAD-touching tools
# ---------------------------------------------------------------------------

# Tools whose success is a meaningful project-doc edit for managed vault cuts.
# Broader than every mutate: only geometry / assembly / save paths that land
# on a project document. AI-VC still records the full mutate set separately.
_PROJECT_DOC_TOOLS = frozenset({
    "add_features",
    "doc_apply_ops",
    "save_part",
    "doc_save",
    "new_part",
    "new_assembly",
    "asm_add_component",
    "asm_insert_component",
    "asm_add_mate",
    "asm_mate",
    "part_place",
    "delete_feature",
    "remove_feature",
    "set_material",
    "regen_resolve",
})


def maybe_vault_checkpoint_on_tool(
    tool: str,
    args: dict | None,
    result: Any,
    *,
    run_id: str | None = None,
    project_dir: str | os.PathLike | None = None,
    ledger: Any | None = None,
    summary: str | None = None,
) -> dict[str, Any] | None:
    """Cut a managed-mode vault checkpoint after a successful CAD-touching tool.

    Complements AI-VC (always-on, per-doc): this writes the project History
    entry with ``author=ai`` when the project's ``ai_mode`` is ``managed``.
    No-op for non-project tools, failed results, or non-managed tiers.
    Best-effort — never raises.
    """
    try:
        if tool not in _PROJECT_DOC_TOOLS:
            return None
        if isinstance(result, dict) and result.get("ok") is False:
            return None

        resolved_dir = project_dir
        if resolved_dir is None:
            try:
                from services.agent import config as agent_config

                wd = agent_config.workspace_dir()
                if wd is not None:
                    resolved_dir = str(wd)
            except Exception:
                resolved_dir = None
        if not resolved_dir or not _project_dir_is_managed(resolved_dir):
            return None

        rid = run_id or "agent"
        bridge = bridge_for_run(rid, project_dir=resolved_dir, ledger=ledger)
        # part_done trigger drafts a git-free checkpoint message; buildgraph_node tags the tool.
        return bridge.on_milestone(
            trigger="part_done",
            buildgraph_node=f"tool:{tool}",
            item_id=None,
        )
    except Exception:
        return None

