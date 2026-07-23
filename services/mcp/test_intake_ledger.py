"""test_intake_ledger.py — REAL reopen test for the doc-scoped IntakeLedger (T-BL25).

Contract: contracts/interaction.md §5 scope=doc — answers persist in the document's
IntakeState record, round-tripped as JSON into the .step+, so a dimension confirmed
in one session is never re-asked after the doc is saved and reopened.

This suite drives the REAL engine (libchudware via core_bridge): no meta-store
injection, no path injection — the production lookup (doc_get_meta/doc_set_meta
through the .step+ save/open cycle) is what is exercised.  Critically, doc_open
mints a FRESH doc_id (core/doc/session.cpp new_doc_id is a per-process counter),
and the test asserts replay works anyway because the record travels with the doc.

Run:  CHUDWARE_LIBCHUDWARE=$PWD/core/build-linux/bin/libchudware.so \
      python -m pytest services/mcp/test_intake_ledger.py -v
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

# Same T-V3-hardened path bootstrap as test_cad_tools.py (resolve() first so the
# mcp_server/ symlink and the real services/mcp/ path both land on repo root).
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from mcp_server import core_bridge as cb

pytestmark = pytest.mark.skipif(
    not cb.available(),
    reason="libchudware not staged; engine-backed IntakeLedger reopen test unavailable",
)


def _question():
    return {
        "id": "output_shaft.dia",
        "kind": "dimension",
        "family": "length",
        "prompt": "Output shaft diameter?",
        "suggested": {"value": 8.0, "unit": "mm"},
        "required": False,
    }


def test_real_reopen_never_re_asks(tmp_path):
    """Session 1: record a doc-scoped answer, doc_save.  Session 2: doc_open the
    file (fresh doc_id), construct the ledger FROM THE DOC ALONE, replay."""
    from services.agent.interaction import ask_user
    from services.mcp.intake_ledger import IntakeLedger

    # --- Session 1 ---------------------------------------------------------- #
    r = cb.call("doc_new", {"kind": "part", "name": f"il_{uuid.uuid4().hex[:8]}"})
    doc1 = r["doc_id"]
    q = _question()

    ledger1 = IntakeLedger(doc1)                    # production accessors — no injection
    r1 = ask_user([q], scope="doc", doc_id=doc1, ledger=ledger1, autonomy="auto")
    assert r1["output_shaft.dia"]["source"] == "default"
    assert r1["output_shaft.dia"]["canonical"] == {"value": 8.0, "unit": "mm"}

    path = tmp_path / "part.step+"
    cb.call("doc_save", {"doc_id": doc1, "path": str(path)})
    assert path.exists() and path.stat().st_size > 0

    # --- Session 2: reopen — the engine mints a NEW doc_id ------------------- #
    opened = cb.call("doc_open", {"path": str(path)})
    doc2 = opened["doc_id"]
    assert doc2 != doc1, (
        "doc_open re-used the doc_id — this test exists because it does NOT; "
        "if id semantics changed, revisit the ledger keying rationale"
    )

    transport_calls: list = []

    class BoomTransport:
        """Fails the test if invoked — the reopened doc must replay from its record."""
        def ask(self, batch):  # pragma: no cover
            transport_calls.append(batch.batch_id)
            return {}

    ledger2 = IntakeLedger(doc2)                    # constructed from the doc alone
    r2 = ask_user([q], scope="doc", doc_id=doc2, ledger=ledger2,
                  transport=BoomTransport(), blocking=True, autonomy="approve")
    assert len(transport_calls) == 0, (
        "Transport invoked after reopen — the answer did not travel with the doc"
    )
    assert r2["output_shaft.dia"]["source"] == "resumed"
    assert r2["output_shaft.dia"]["canonical"] == {"value": 8.0, "unit": "mm"}


def test_real_reopen_default_ledger_path(tmp_path):
    """Same reopen guarantee through ask_user's DEFAULT ledger (ledger=None,
    scope=doc): the production wiring users actually hit — nothing injected."""
    from services.agent.interaction import ask_user

    r = cb.call("doc_new", {"kind": "part", "name": f"il_{uuid.uuid4().hex[:8]}"})
    doc1 = r["doc_id"]
    q = _question()

    r1 = ask_user([q], scope="doc", doc_id=doc1, autonomy="auto")   # ledger=None
    assert r1["output_shaft.dia"]["source"] == "default"

    path = tmp_path / "part.step+"
    cb.call("doc_save", {"doc_id": doc1, "path": str(path)})

    doc2 = cb.call("doc_open", {"path": str(path)})["doc_id"]

    r2 = ask_user([q], scope="doc", doc_id=doc2, autonomy="auto")   # ledger=None
    assert r2["output_shaft.dia"]["source"] == "resumed", (
        "ask_user's default doc-scope ledger did not replay after a real reopen"
    )


def test_confirm_dimensions_persists_through_reopen(tmp_path):
    """The MCP call-site route (contract §5 'ask_user/intake call sites'):
    confirm_dimensions with no run_id lands the answer in the doc record, and it
    is still there — replayable — after save + reopen."""
    import json

    from services.mcp.intake_ledger import IntakeLedger
    from services.mcp import cad_tools
    from services.mcp.intake_tools import register_intake_tools
    from mcp.server.fastmcp import FastMCP

    # Register the intake tools on a throwaway FastMCP to reach confirm_dimensions.
    mcp = FastMCP("t")
    register_intake_tools(mcp)

    r = cb.call("doc_new", {"kind": "part", "name": f"il_{uuid.uuid4().hex[:8]}"})
    doc1 = r["doc_id"]

    # Seed a critical-dim ledger so confirm_dimensions has a row to close.
    from services.intake import intake as ik
    mp = ik.new_measured_part("mp_1", "motor", source="photo")
    mp.features = [ik.new_measured_feature("output_shaft", "shaft_in_bore",
                                           dims={"dia": 8.0})]
    ledger = ik.build_ledger(mp, process="fdm")
    cb.call("doc_set_meta", {"doc_id": doc1, "key": "measured_parts",
                             "value": [mp.to_dict()]})
    cb.call("doc_set_meta", {"doc_id": doc1, "key": "crit_dims",
                             "value": ledger.to_dict()})

    import asyncio
    content, structured = asyncio.run(mcp.call_tool("confirm_dimensions", {
        "confirmations": [{"key": "output_shaft.dia", "value_mm": 8.2}],
        "doc_id": doc1,
    }))
    out = json.loads(structured["result"]) if isinstance(structured, dict) else None
    assert out and out.get("ok"), out

    # Save + reopen; the interaction answer must have travelled with the doc.
    path = tmp_path / "part.step+"
    cb.call("doc_save", {"doc_id": doc1, "path": str(path)})
    doc2 = cb.call("doc_open", {"path": str(path)})["doc_id"]

    replay = IntakeLedger(doc2).answered("intake.dim.output_shaft.dia")
    assert replay is not None, (
        "confirm_dimensions answer missing after reopen — scope=doc routing broken"
    )
    assert replay["canonical"] == {"value": 8.2, "unit": "mm"}
