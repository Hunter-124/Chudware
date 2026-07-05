"""PLM bridge — agent milestone checkpoints, ledger-annotated history.

Contract: contracts/advisor.md §15 (aicad.plmlink/1, FROZEN).
Vault API: contracts/plm.md (aicad.plm/1) drafted by T-R1; matched here
against the plm_checkpoint / plm_history envelopes in §7.

Design decisions
----------------
* Thin ``VaultClient`` Protocol — realignment to the frozen plm.md is a
  one-file change when T-R2 ships.
* Feature flag ``AICAD_PLM_BRIDGE`` (default off → MockVault).  Set to "1"
  in production once services/plm/ (T-R2) is merged.
* Trigger config table: which buildgraph / pipeline events emit a PLM
  checkpoint.  Only meaningful events (not every stage): part_done,
  mission_done, plan_ratified, stage_exit (part_verify/assembly/sim only),
  change_request.
* Offline queue: plmlink events persist in the ledger (kind="plmlink");
  a flush routine runs on availability.  Idempotency key:
  (run_id, buildgraph_node, trigger) for build triggers, audit_id for
  change_request.  A "plmlink_flushed" marker event prevents double-flush.
* Verbatim-splice policy: dimension rationales come from ledger.events
  (NOT ledger.state — provenance is stripped from materialized state).
  Decision rationale text is copied verbatim without paraphrasing.
* Pillar 19 locked intent: NO git vocabulary in user-facing messages;
  Project/History/Checkpoint nouns only.  Message vocabulary test included
  in test_plm_bridge.py.
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Trigger configuration table (normative — per T-W18 step 2)
# ---------------------------------------------------------------------------

# Which (trigger, stage_exit_value) pairs create a PLM checkpoint by default.
# stage_exit triggers only fire for meaningful verification milestones.
CHECKPOINT_TRIGGERS: dict[str, bool] = {
    "plan_ratified": True,
    "part_done": True,
    "mission_done": True,
    "change_request": True,
    # stage_exit fires only for the three verification stages below:
    "stage_exit:part_verify": True,
    "stage_exit:assembly": True,
    "stage_exit:sim": True,
    # NOT triggered for every stage — only verification milestones:
    "stage_exit:sketch": False,
    "stage_exit:model": False,
    "stage_exit:layout": False,
    "stage_exit:planning": False,
}

# Forbidden words in user-facing checkpoint messages (Pillar 19, §8.1 of plm.md)
_GIT_VOCAB = frozenset({
    "git", "commit", "repository", "repo", "branch", "merge",
    "rebase", "head", "ref", "stage", "staging", "push", "pull",
    "sha", "hash",
})

SCHEMA = "aicad.plmlink/1"

# ---------------------------------------------------------------------------
# VaultClient Protocol — thin seam to services/plm/ (T-R2)
# ---------------------------------------------------------------------------


@runtime_checkable
class VaultClient(Protocol):
    """Minimal surface T-W18 uses from the PLM vault (aicad.plm/1 §7).

    Implemented by MockVault (tests/feature-flag-off) and by a real thin
    wrapper around services/plm/ once T-R2 is merged.
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
# MockVault — used when AICAD_PLM_BRIDGE != "1"
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
    ) -> dict[str, Any]:
        commit = "mock_" + uuid.uuid4().hex[:8]
        entry = {
            "commit": commit,
            "kind": "checkpoint",
            "authored_at": _now(),
            "author": {"kind": author_kind, "id": "agent", "display": "AI Agent"},
            "message": message,
            "ai": {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "run_id": run_id or "",
                "artifact_id": artifact_id or "",
                "reversible": True,
            } if author_kind == "ai" else None,
        }
        self._checkpoints.append({"project_id": project_id, "item_id": item_id, "entry": entry})
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

def _default_vault() -> VaultClient:
    """Return the vault client selected by the feature flag.

    AICAD_PLM_BRIDGE=1  → attempt to import and use the real PLM client
                          (services/plm/, T-R2).  Falls back to MockVault
                          if that module is not yet available.
    (unset / anything else) → MockVault (safe offline default).
    """
    if os.environ.get("AICAD_PLM_BRIDGE") == "1":
        try:
            from services.plm.client import PLMVaultClient  # type: ignore[import]
            return PLMVaultClient()
        except ImportError:
            pass  # T-R2 not yet merged — fall back to mock
    return MockVault()


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

    Verbatim-splice policy (contract §15 + Pillar 19):
    - Decision rationale text is copied VERBATIM from the event payload.
    - Dimension provenance is copied from ledger.events (not ledger.state,
      which strips the provenance field).
    - Numerals in the message must originate from ledger fields only —
      no hand-typed numbers.
    - No git vocabulary anywhere in the output.
    """
    # Collect rationale snippets from decisions and dimension provenance
    # since the last checkpoint (or from the beginning of the run).
    rationale_parts: list[str] = []
    dimension_parts: list[str] = []

    for ev in ledger_events:
        kind = ev.get("kind", "")
        payload = ev.get("payload", {})

        if kind == "decision":
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

    # Build message parts
    parts: list[str] = []

    # Trigger prefix (Pillar 19 nouns — no git vocab)
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
            "part_verify": "Part verification passed.",
            "assembly": "Assembly stage complete.",
            "sim": "Simulation complete.",
        }.get(stage, f"Stage {stage!r} complete.")
        parts.append(stage_label)

    if dimension_parts:
        parts.append("Dimensions: " + "; ".join(dimension_parts) + ".")

    if rationale_parts:
        # Verbatim splice — join as separate sentences
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
    uses Pillar 19 nouns: Project, History, Checkpoint.
    """

    def __init__(
        self,
        run_id: str,
        vault: VaultClient | None = None,
        model_id: str = "claude-sonnet-4-6",
        provider: str = "anthropic",
    ) -> None:
        self.run_id = run_id
        self._vault: VaultClient = vault if vault is not None else _default_vault()
        self._model_id = model_id
        self._provider = provider
        self._ledger: Any | None = None  # set via attach_ledger()

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
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Called when a pipeline milestone fires.

        Args:
            trigger:            One of the CHECKPOINT_TRIGGERS keys.
            buildgraph_node:    The bg_... node id from T-W15 executor.
            stage:              For stage_exit triggers, the stage name.
            audit_id:           For change_request triggers, the audit id.
            modifies_checkpoint: The checkpoint the CR modifies (for timeline).
            item_id:            Optional PLM item_id for per-part checkpoints.
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

        dedup = _dedup_key(self.run_id, trigger, buildgraph_node, audit_id)

        # Build plmlink record (§15 schema)
        ledger_event_seqs = self._collect_ledger_seqs_since_last_checkpoint()
        message = draft_checkpoint_message(
            self._events_since_last_checkpoint(),
            trigger=trigger,
            stage=stage,
        )

        # Pillar 19 vocabulary guard
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
            "change_request": {
                "audit_id": audit_id or "",
                "modifies_checkpoint": modifies_checkpoint,
            },
            "dedup_key": dedup,
            "queued": False,
        }

        # Attempt vault call
        if self._vault is not None and self._vault.is_available() and resolved_project_id:
            result = self._vault.checkpoint(
                project_id=resolved_project_id,
                message=message,
                item_id=item_id,
                author_kind="ai",
                run_id=self.run_id,
                artifact_id=buildgraph_node or "",
            )
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

            result = self._vault.checkpoint(
                project_id=resolved_project_id,
                message=message,
                item_id=record.get("item_id"),
                author_kind="ai",
                run_id=self.run_id,
                artifact_id=buildgraph_node,
            )
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
# Buildgraph executor hook (T-W15 seam)
# ---------------------------------------------------------------------------

def make_buildgraph_emit_hook(bridge: "PLMBridge") -> Any:
    """Return an emit callback for BuildGraphExecutor that fires PLM checkpoints.

    Usage:
        executor = BuildGraphExecutor(graph, emit=make_buildgraph_emit_hook(bridge))

    The hook fires on "buildgraph.node.done" events for checkpoint-kind nodes,
    mapping them to the appropriate PLM trigger (part_done or mission_done).
    This is the real T-W15 seam — no feature flag needed since T-W15 is merged.
    """
    def _emit(event_type: str, payload: dict[str, Any]) -> None:
        if event_type != "buildgraph.node.done":
            return
        node_id = payload.get("node_id", "")
        # We need the full node dict to check kind and label.
        # The executor passes status but not the node dict — look it up via
        # the bridge's cached graph if available, else use the event payload.
        node_kind = payload.get("kind", "")
        node_label = payload.get("label", "")

        trigger: str | None = None
        if node_label == "mission_done" or node_kind == "checkpoint" and "mission" in node_label:
            trigger = "mission_done"
        elif node_kind == "checkpoint" or "part_done" in node_label:
            trigger = "part_done"

        if trigger is None:
            return

        bridge.on_milestone(
            trigger=trigger,
            buildgraph_node=node_id,
        )

    return _emit


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
