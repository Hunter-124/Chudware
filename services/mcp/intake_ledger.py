"""IntakeLedger — the doc-scoped interaction answer store (contracts/interaction.md §5).

Persists ``ask_user`` Q/A records inside the document's ``IntakeState``
(``services/intake/intake.py``), round-tripped as JSON into the ``.step+``
record via ``doc_set_meta`` / ``doc_get_meta`` — the same opaque-doc-key mechanism
``confirm_dimensions`` uses for the critical-dimension ledger.  Contract §5:
"Same records; different store."

Because the store travels WITH the document (not with a cache path keyed on the
engine's per-process ``doc_id`` — ``core/doc/session.cpp new_doc_id()`` mints a
fresh ``doc-cpp%08llx`` on every open), a dimension confirmed in one session is
never re-asked after the doc is saved and reopened, no matter what id the new
process assigned it.  That is the cross-session no-re-ask guarantee.

This module deliberately does NOT import FastMCP: it must stay importable from
``services/agent/interaction.py``'s lazy default-ledger path without dragging
in the MCP server namespace.  The engine bridge itself is imported lazily at
call time; ``get_meta`` / ``set_meta`` are injectable for engine-less tests.
"""
from __future__ import annotations

import copy
from typing import Any, Callable, Optional

#: doc["meta"] key for the intake resume token — MUST match intake_tools._K_INTAKE.
K_INTAKE = "intake"


def _bridge():
    try:
        from services.mcp import core_bridge as cb  # type: ignore
    except ImportError:
        from services.mcp import core_bridge as cb  # type: ignore
    return cb


def _default_get(doc_id: str, key: str) -> Any:
    try:
        return _bridge().call("doc_get_meta", {"doc_id": doc_id, "key": key}).get("value")
    except Exception:  # noqa: BLE001 - absent meta / engine hiccup reads as empty
        return None


def _default_set(doc_id: str, key: str, value: Any) -> None:
    _bridge().call("doc_set_meta", {"doc_id": doc_id, "key": key, "value": value})


class IntakeLedger:
    """Ledger-protocol adapter over the doc's ``IntakeState.interaction`` record.

    Implements the same protocol as ``FileLedger`` / ``MemoryLedger``
    (``answered`` / ``record_question`` / ``record_answer``) so ``ask_user``
    replays doc-scoped answers exactly like run-scoped ones.

    Every mutation is load-modify-save against the live doc meta, so concurrent
    intake-tool writes to the other ``IntakeState`` fields (``status``, ``needs``,
    ``pending_request_id``) between ledger calls are never clobbered.

    ``get_meta(doc_id, key) -> value`` and ``set_meta(doc_id, key, value)`` are
    injectable for tests; the defaults go through the engine bridge
    (``doc_get_meta`` / ``doc_set_meta``, persisted inside the ``.step+``).
    """

    def __init__(self, doc_id: str, *,
                 get_meta: Optional[Callable[[str, str], Any]] = None,
                 set_meta: Optional[Callable[[str, str, Any], None]] = None) -> None:
        self.doc_id = doc_id
        self._get = get_meta or _default_get
        self._set = set_meta or _default_set

    # -- internal ---------------------------------------------------------- #

    def _state(self) -> dict:
        """The doc's raw intake meta record ({} when none yet)."""
        raw = self._get(self.doc_id, K_INTAKE)
        return copy.deepcopy(raw) if isinstance(raw, dict) else {}

    @staticmethod
    def _interaction(state: dict) -> dict:
        ia = state.get("interaction")
        if not isinstance(ia, dict):
            ia = {}
        ia.setdefault("questions", [])
        ia.setdefault("answers", {})
        state["interaction"] = ia
        return ia

    def _save(self, state: dict) -> None:
        self._set(self.doc_id, K_INTAKE, state)

    # -- Ledger protocol ---------------------------------------------------- #

    def answered(self, qid: str) -> dict | None:
        ans = self._interaction(self._state())["answers"].get(qid)
        return ans if isinstance(ans, dict) else None

    def record_question(self, q: dict) -> None:
        state = self._state()
        self._interaction(state)["questions"].append(dict(q))
        self._save(state)

    def record_answer(self, qid: str, answer: dict) -> None:
        state = self._state()
        self._interaction(state)["answers"][qid] = answer
        self._save(state)
