"""Pure, stdlib-only implementation of the ``aicad.req/1`` requirements schema.

This module owns:
- Dataclass definitions for every record in the schema.
- ``to_dict`` / ``from_dict`` round-trip (JSON-safe plain dicts).
- ``VALID_VERIFIERS`` — the canonical list of MCP tool names that may appear in
  ``verify_by[].verifier``.  The verifier battery may extend this list via contract amendment.
- ``markdown_projection`` — a deterministic, diff-stable, **read-only** view.
- Factory helpers for creating new documents and mutating them correctly.

Nothing here touches the engine, the network, or the filesystem — all public functions
operate on plain dicts or dataclass instances.  Persistence (doc_set_meta / .req.json)
lives in the MCP tool layer.
"""
from __future__ import annotations

import copy
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# VALID_VERIFIERS — normative list; see contracts/requirements.md §3.
# The verifier battery may extend this via contract amendment.
# ---------------------------------------------------------------------------
VALID_VERIFIERS: frozenset[str] = frozenset({
    "check_sketch",
    "check_feature_overlap",
    "check_interference",
    "check_symmetry",
    "compare_parts",
    "confirm_dimensions",
    "dfm_check",
    "draft_analysis",
    "fit_report",
    "measure_between",
    "measure_part",
    "query_geometry",
    "run_cfd_accurate",
    "run_cfd_flow",
    "run_fea_modal",
    "run_fea_static",
    "solve_mates",
    "wall_thickness",
})

# Valid requirement kinds.
VALID_KINDS: frozenset[str] = frozenset({
    "dimensional",
    "functional",
    "material",
    "interface",
    "cost",
    "standard",
    "process",
})

# Valid requirement statuses.
VALID_STATUSES: frozenset[str] = frozenset({
    "proposed",
    "user_confirmed",
    "verified",
    "failed",
    "waived",
})

# Status transitions that agents are allowed to make autonomously.
# user_confirmed and waived require explicit user action.
AGENT_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"verified", "failed"}),
    "user_confirmed": frozenset({"verified", "failed"}),
    "verified": frozenset(),
    "failed": frozenset({"proposed"}),  # re-open after fix
    "waived": frozenset(),
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Quantity:
    """Typed measurement: the ONLY place numbers live in a requirements doc."""

    param: str
    op: str                           # "==" | ">=" | "<=" | "range"
    value: float
    unit: str
    value_max: Optional[float] = None  # required when op == "range"
    tol: Optional[dict[str, float]] = None  # {"plus": float, "minus": float}

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "param": self.param,
            "op": self.op,
            "value": self.value,
            "unit": self.unit,
        }
        if self.value_max is not None:
            d["value_max"] = self.value_max
        if self.tol is not None:
            d["tol"] = copy.deepcopy(self.tol)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Quantity":
        return cls(
            param=d["param"],
            op=d["op"],
            value=float(d["value"]),
            unit=d["unit"],
            value_max=d.get("value_max"),
            tol=copy.deepcopy(d["tol"]) if d.get("tol") else None,
        )

    def format_short(self) -> str:
        """Human-readable short form for markdown projection."""
        val_str = f"{self.value:g}"
        if self.op == "range" and self.value_max is not None:
            base = f"{self.param} {val_str}–{self.value_max:g} {self.unit}"
        elif self.op == "==":
            base = f"{self.param} = {val_str} {self.unit}"
        elif self.op == ">=":
            base = f"{self.param} ≥ {val_str} {self.unit}"
        elif self.op == "<=":
            base = f"{self.param} ≤ {val_str} {self.unit}"
        else:
            base = f"{self.param} {self.op} {val_str} {self.unit}"
        if self.tol:
            plus = self.tol.get("plus", 0)
            minus = self.tol.get("minus", 0)
            base += f" +{plus:g}/−{minus:g}"
        return base


@dataclass
class VerifyRef:
    """Reference to one deterministic verifier."""

    verifier: str
    args: Optional[str] = None

    def validate(self) -> None:
        if self.verifier not in VALID_VERIFIERS:
            raise ValueError(
                f"Unknown verifier {self.verifier!r}. "
                f"Must be one of: {sorted(VALID_VERIFIERS)}"
            )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"verifier": self.verifier}
        if self.args is not None:
            d["args"] = self.args
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VerifyRef":
        return cls(verifier=d["verifier"], args=d.get("args"))


@dataclass
class Trace:
    """Links requirement to model features / studies / evidence."""

    features: list[str] = field(default_factory=list)
    study: Optional[str] = None
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "features": list(self.features),
            "study": self.study,
            "evidence": copy.deepcopy(self.evidence),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Trace":
        return cls(
            features=list(d.get("features") or []),
            study=d.get("study"),
            evidence=copy.deepcopy(d.get("evidence") or []),
        )


@dataclass
class Requirement:
    """One requirement in the brief."""

    id: str
    need: str
    kind: str
    text: str
    verify_by: list[VerifyRef]
    status: str = "proposed"
    quantity: Optional[Quantity] = None
    trace: Trace = field(default_factory=Trace)

    def validate(self) -> None:
        if self.kind not in VALID_KINDS:
            raise ValueError(f"Invalid kind {self.kind!r}. Must be one of: {sorted(VALID_KINDS)}")
        if self.status not in VALID_STATUSES:
            raise ValueError(f"Invalid status {self.status!r}.")
        if not self.verify_by:
            raise ValueError(f"Requirement {self.id!r} must name ≥1 verifier in verify_by.")
        for vr in self.verify_by:
            vr.validate()
        _check_no_numbers_in_text(self.text, field_name=f"requirement[{self.id}].text")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "need": self.need,
            "kind": self.kind,
            "text": self.text,
            "quantity": self.quantity.to_dict() if self.quantity is not None else None,
            "verify_by": [v.to_dict() for v in self.verify_by],
            "status": self.status,
            "trace": self.trace.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Requirement":
        return cls(
            id=d["id"],
            need=d["need"],
            kind=d["kind"],
            text=d["text"],
            quantity=Quantity.from_dict(d["quantity"]) if d.get("quantity") else None,
            verify_by=[VerifyRef.from_dict(v) for v in d.get("verify_by", [])],
            status=d.get("status", "proposed"),
            trace=Trace.from_dict(d.get("trace") or {}),
        )


@dataclass
class StakeholderNeed:
    """One stakeholder need that requirements trace back to."""

    id: str
    text: str
    source: str = "chat"  # "chat" | "doc" | "import"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "source": self.source}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StakeholderNeed":
        return cls(id=d["id"], text=d["text"], source=d.get("source", "chat"))


@dataclass
class AuditEntry:
    """One immutable audit log entry."""

    who: str   # "user" | "agent"
    ts: str    # ISO-8601 UTC
    change: str

    def to_dict(self) -> dict[str, Any]:
        return {"who": self.who, "ts": self.ts, "change": self.change}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AuditEntry":
        return cls(who=d["who"], ts=d["ts"], change=d["change"])


@dataclass
class RequirementsDoc:
    """Complete ``aicad.req/1`` document."""

    id: str
    title: str
    revision: int
    created: str
    modified: str
    stakeholder_needs: list[StakeholderNeed] = field(default_factory=list)
    requirements: list[Requirement] = field(default_factory=list)
    interfaces: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[dict[str, Any]] = field(default_factory=list)
    audit: list[AuditEntry] = field(default_factory=list)

    SCHEMA = "aicad.req/1"

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def new(cls, title: str) -> "RequirementsDoc":
        """Create a fresh empty document."""
        now = _iso_now()
        doc_id = str(uuid.uuid4())
        doc = cls(
            id=doc_id,
            title=title,
            revision=1,
            created=now,
            modified=now,
        )
        doc.audit.append(AuditEntry(who="agent", ts=now, change="Document created"))
        return doc

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> None:
        need_ids = {n.id for n in self.stakeholder_needs}
        for req in self.requirements:
            req.validate()
            if req.need not in need_ids:
                raise ValueError(
                    f"Requirement {req.id!r} references unknown need {req.need!r}."
                )

    # ------------------------------------------------------------------
    # Mutation helpers
    # ------------------------------------------------------------------

    def add_need(self, text: str, source: str = "chat") -> StakeholderNeed:
        """Append a new stakeholder need and return it."""
        n_id = f"N{len(self.stakeholder_needs) + 1}"
        need = StakeholderNeed(id=n_id, text=text, source=source)
        self.stakeholder_needs.append(need)
        self._touch("agent", f"Added stakeholder need {n_id}")
        return need

    def add_requirement(
        self,
        need_id: str,
        kind: str,
        text: str,
        verify_by: list[dict[str, Any]],
        quantity: Optional[dict[str, Any]] = None,
    ) -> Requirement:
        """Append a new requirement and return it.

        ``verify_by`` items: ``{"verifier": "...", "args": "..."}`` (args optional).
        ``quantity``: see Quantity schema; may be None for qualitative requirements.
        """
        r_id = f"R{len(self.requirements) + 1}"
        vrs = [VerifyRef.from_dict(v) for v in verify_by]
        qty = Quantity.from_dict(quantity) if quantity else None
        req = Requirement(
            id=r_id,
            need=need_id,
            kind=kind,
            text=text,
            verify_by=vrs,
            quantity=qty,
        )
        req.validate()
        self.requirements.append(req)
        self._touch("agent", f"Added requirement {r_id}")
        return req

    def set_status(
        self,
        req_id: str,
        new_status: str,
        *,
        who: str,
        evidence: Optional[list[dict[str, Any]]] = None,
    ) -> Requirement:
        """Transition a requirement status.

        Agents may only set ``verified`` or ``failed``.
        ``user_confirmed`` and ``waived`` require ``who="user"``.
        """
        req = self._get_req(req_id)
        if new_status not in VALID_STATUSES:
            raise ValueError(f"Invalid status {new_status!r}.")
        if who == "agent" and new_status not in AGENT_ALLOWED_TRANSITIONS.get(req.status, set()):
            raise ValueError(
                f"Agent cannot transition {req_id} from {req.status!r} to {new_status!r}. "
                "user_confirmed and waived require explicit user action."
            )
        old_status = req.status
        req.status = new_status
        if evidence:
            req.trace.evidence.extend(evidence)
        self._touch(who, f"{req_id}: {old_status} → {new_status}")
        return req

    def update_requirement(
        self,
        req_id: str,
        *,
        who: str,
        text: Optional[str] = None,
        kind: Optional[str] = None,
        quantity: Optional[dict[str, Any]] = None,
        verify_by: Optional[list[dict[str, Any]]] = None,
    ) -> Requirement:
        """Update fields of an existing requirement."""
        req = self._get_req(req_id)
        changes: list[str] = []
        if text is not None:
            _check_no_numbers_in_text(text, field_name=f"requirement[{req_id}].text")
            req.text = text
            changes.append("text")
        if kind is not None:
            if kind not in VALID_KINDS:
                raise ValueError(f"Invalid kind {kind!r}.")
            req.kind = kind
            changes.append("kind")
        if quantity is not None:
            req.quantity = Quantity.from_dict(quantity)
            changes.append("quantity")
        if verify_by is not None:
            vrs = [VerifyRef.from_dict(v) for v in verify_by]
            for vr in vrs:
                vr.validate()
            req.verify_by = vrs
            changes.append("verify_by")
        req.validate()
        self._touch(who, f"{req_id}: updated {', '.join(changes)}")
        return req

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "id": self.id,
            "title": self.title,
            "revision": self.revision,
            "created": self.created,
            "modified": self.modified,
            "stakeholder_needs": [n.to_dict() for n in self.stakeholder_needs],
            "requirements": [r.to_dict() for r in self.requirements],
            "interfaces": copy.deepcopy(self.interfaces),
            "assumptions": copy.deepcopy(self.assumptions),
            "open_questions": copy.deepcopy(self.open_questions),
            "audit": [a.to_dict() for a in self.audit],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RequirementsDoc":
        schema = d.get("schema", "")
        if schema != cls.SCHEMA:
            raise ValueError(f"Expected schema {cls.SCHEMA!r}, got {schema!r}.")
        return cls(
            id=d["id"],
            title=d["title"],
            revision=int(d.get("revision", 1)),
            created=d.get("created", ""),
            modified=d.get("modified", ""),
            stakeholder_needs=[
                StakeholderNeed.from_dict(n) for n in d.get("stakeholder_needs", [])
            ],
            requirements=[Requirement.from_dict(r) for r in d.get("requirements", [])],
            interfaces=copy.deepcopy(d.get("interfaces") or []),
            assumptions=copy.deepcopy(d.get("assumptions") or []),
            open_questions=copy.deepcopy(d.get("open_questions") or []),
            audit=[AuditEntry.from_dict(a) for a in d.get("audit", [])],
        )

    # ------------------------------------------------------------------
    # Markdown projection (read-only view, deterministic ordering)
    # ------------------------------------------------------------------

    def to_markdown(self) -> str:
        """Return a deterministic, diff-stable markdown view of this document.

        This is a VIEW only — never parse it back to JSON.
        Ordering: requirement IDs lexicographic (R1, R2, … R10 sorts numerically via
        _sort_key to avoid R10 < R2).
        """
        lines: list[str] = []
        lines.append(f"# {self.title}  (rev {self.revision})")
        lines.append("")
        lines.append(f"*Schema: `{self.SCHEMA}`  |  ID: `{self.id}`*")
        lines.append("")

        # Stakeholder needs
        lines.append("## Stakeholder Needs")
        if self.stakeholder_needs:
            lines.append("")
            lines.append("| ID | Need | Source |")
            lines.append("|---|---|---|")
            for n in sorted(self.stakeholder_needs, key=lambda x: _sort_key(x.id)):
                lines.append(f"| {n.id} | {_esc(n.text)} | {n.source} |")
        else:
            lines.append("")
            lines.append("*(none)*")
        lines.append("")

        # Requirements
        lines.append("## Requirements")
        if self.requirements:
            lines.append("")
            lines.append("| ID | Need | Kind | Requirement | Quantity | Status |")
            lines.append("|---|---|---|---|---|---|")
            for r in sorted(self.requirements, key=lambda x: _sort_key(x.id)):
                qty_str = r.quantity.format_short() if r.quantity else "—"
                lines.append(
                    f"| {r.id} | {r.need} | {r.kind} | {_esc(r.text)} "
                    f"| {_esc(qty_str)} | {r.status} |"
                )
            lines.append("")
            # Verifier detail block
            lines.append("### Verifier detail")
            lines.append("")
            for r in sorted(self.requirements, key=lambda x: _sort_key(x.id)):
                verifiers = ", ".join(
                    (f"`{v.verifier}`" + (f" ({v.args})" if v.args else ""))
                    for v in r.verify_by
                )
                lines.append(f"- **{r.id}**: {verifiers}")
        else:
            lines.append("")
            lines.append("*(none)*")
        lines.append("")

        # Interfaces
        if self.interfaces:
            lines.append("## Interfaces")
            lines.append("")
            for iface in sorted(self.interfaces, key=lambda x: _sort_key(x.get("id", ""))):
                lines.append(f"- **{iface.get('id', '?')}**: {_esc(iface.get('text', ''))}")
            lines.append("")

        # Assumptions
        if self.assumptions:
            lines.append("## Assumptions")
            lines.append("")
            for a in sorted(self.assumptions, key=lambda x: _sort_key(x.get("id", ""))):
                lines.append(f"- **{a.get('id', '?')}**: {_esc(a.get('text', ''))}")
            lines.append("")

        # Open questions
        if self.open_questions:
            lines.append("## Open Questions")
            lines.append("")
            for q in sorted(self.open_questions, key=lambda x: _sort_key(x.get("id", ""))):
                lines.append(f"- **{q.get('id', '?')}**: {_esc(q.get('text', ''))}")
            lines.append("")

        # Audit trail
        lines.append("## Audit Trail")
        lines.append("")
        if self.audit:
            lines.append("| When | Who | Change |")
            lines.append("|---|---|---|")
            for entry in self.audit:
                lines.append(f"| {entry.ts} | {entry.who} | {_esc(entry.change)} |")
        else:
            lines.append("*(empty)*")
        lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _touch(self, who: str, change: str) -> None:
        now = _iso_now()
        self.modified = now
        self.audit.append(AuditEntry(who=who, ts=now, change=change))

    def _get_req(self, req_id: str) -> Requirement:
        for req in self.requirements:
            if req.id == req_id:
                return req
        raise KeyError(f"Requirement {req_id!r} not found.")


# ---------------------------------------------------------------------------
# Private utilities
# ---------------------------------------------------------------------------

_NUMERAL_RE = re.compile(r"\b\d+(?:[.,]\d+)?\b")


def _check_no_numbers_in_text(text: str, *, field_name: str = "text") -> None:
    """Enforce the number-discipline rule: numerals belong in quantity, not text."""
    if _NUMERAL_RE.search(text):
        raise ValueError(
            f"{field_name} must be prose only — move numerals to the 'quantity' field. "
            f"Found: {_NUMERAL_RE.findall(text)!r}"
        )


def _sort_key(id_str: str) -> tuple[str, int, str]:
    """Sort IDs like R1, R2, … R10 numerically, not lexicographically."""
    m = re.match(r"^([A-Za-z]+)(\d+)(.*)$", id_str or "")
    if m:
        return (m.group(1), int(m.group(2)), m.group(3))
    return (id_str, 0, "")


def _esc(text: str) -> str:
    """Escape pipe characters for use in a markdown table cell."""
    return str(text).replace("|", "\\|")
