"""tool_guidance.parts — curated options for the parts tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "bom_price": {
        "prompt_note": (
            "Network-bound per bought line and useless without provider "
            "credentials (Nexar/Digi-Key/Mouser) — unpriced lines come back "
            "price-unknown, never guessed. Run bom_rollup first."
        ),
        "timeout_s": 180,
    },
    "bom_rollup": {
        "prompt_note": (
            "Cheap local classification pass — run it before any pricing: made "
            "lines go to cam_cost_estimate, bought lines (with MPNs) to bom_price."
        ),
    },
    "cam_cost_estimate": {
        "prompt_note": (
            "Runs cam_simulate internally for cycle time — the doc just needs "
            "CAM ops, not a pre-run sim. Unknown material returns ok=false; "
            "pass material= to override."
        ),
        "timeout_s": 180,
    },
    "cam_tool_import": {
        "prompt_note": (
            "Needs an active CAM job (cam_new_job first); ops reference the "
            "returned tool_number. Feeds/speeds come from feeds_speeds(), not "
            "the part record."
        ),
    },
    "import_artifact": {
        "prompt_note": (
            "Second step of the scrape lane — only call with an artifact_path "
            "from a done scrape job (scrape_queue_status); afterwards "
            "reverse_engineer(source=doc_id) can recover features."
        ),
    },
    "list_spec_templates": {
        "prompt_note": (
            "Call before spec_sheet_match: required keys gate the build and "
            "are never defaulted, so the datasheet must yield exactly these dims."
        ),
    },
    "match_model": {
        "prompt_note": (
            "For branded products (MPN/brand, no standard designation). On no "
            "confident repo match, follow fallback.how — usually "
            "reverse_engineer_product."
        ),
    },
    "match_part_number": {
        "prompt_note": (
            "First stop for a purchased MPN — strips vendor suffixes (-ND "
            "etc.) and runs fully offline; on matched=false fall through to "
            "match_model, never guess params."
        ),
    },
    "part_get": {
        "prompt_note": (
            "Fetch the full PartRecord after a part_search hit — summary rows "
            "omit the params/specs you need before part_place or cam_tool_import."
        ),
    },
    "part_place": {
        "prompt_note": (
            "doc is the document dict (or its json.dumps), NEVER a doc id; omit "
            "doc for mechanical parts — they create their own document."
        ),
    },
    "part_search": {
        "prompt_note": (
            "Use when you know the designation or category; for a plain-language "
            "need start with part_source instead. Rows are compact — part_get "
            "for the full record."
        ),
    },
    "part_source": {
        "prompt_note": (
            "One call for 'I need X with dims' — each candidate carries an "
            "artifact_plan (part_place / scrape_enqueue+import_artifact / "
            "make_standard_part); follow it, don't re-search."
        ),
    },
    "parts_taxonomy": {
        "prompt_note": (
            "Browse here when the user names a category loosely; a node's "
            "effective facets are exactly the facet keys part_search(facets=...) "
            "accepts there."
        ),
    },
    "request_quote": {
        "prompt_note": (
            "No configured API key (XOMETRY_API_KEY / JLCPCB_KEY) → ok=false "
            "without any network call. xometry takes a mechanical doc "
            "(auto-exports STEP), jlcpcb the Gerber bundle."
        ),
        "timeout_s": 180,
    },
    "reverse_engineer_product": {
        "prompt_note": (
            "The hand-off when match_model misses — needs part_id or url; "
            "harvests page media + datasheets and opens the GUI ReverseEng tab "
            "for a human-in-the-loop session."
        ),
        "timeout_s": 300,
    },
    "scrape_enqueue": {
        "prompt_note": (
            "Returns immediately — the download runs in the background; poll "
            "scrape_queue_status, then import_artifact on done. Site must be "
            "logged in (vendor_login) first."
        ),
    },
    "scrape_queue_status": {
        "prompt_note": (
            "The only way to follow a scrape job — poll this instead of "
            "re-enqueueing; artifact_path (input to import_artifact) exists "
            "only on status=done."
        ),
    },
    "spec_sheet_match": {
        "prompt_note": (
            "Long-tail lane after match_part_number/match_model fail; run "
            "list_spec_templates first — a missing required dim returns "
            "matched=false and builds nothing. LLM call, slow."
        ),
        "timeout_s": 300,
    },
    "vendor_login": {
        "prompt_note": (
            "HEADED browser that blocks up to 300s while the HUMAN logs in — "
            "tell the user before calling and never retry-loop it; check "
            "vendor_status afterwards."
        ),
        "timeout_s": 360,
    },
    "vendor_part_register": {
        "prompt_note": (
            "Registers a completed scrape's price/availability so bom_price can "
            "resolve that supplier + part number — import_artifact handles the "
            "geometry, this handles the money. The price is a SCRAPED figure "
            "stamped with provenance and a fetch timestamp, never a vendor "
            "quote; an ambiguous display price (a range, 'per pack of N') "
            "leaves unit_price null and returns price_unparsed rather than "
            "guessing — pass unit_price= explicitly to resolve it."
        ),
    },
    "vendor_status": {
        "prompt_note": (
            "Fast local read (no network) of adapter keys + login state — "
            "check this before scrape_enqueue to pick an already-logged-in site."
        ),
    },
    "vendors_health": {
        "prompt_note": (
            "Live network canary per adapter (blank sweeps all registered "
            "sites) — slow; prefer vendor_status for a quick answer. "
            "needs_human_login → run vendor_login(site)."
        ),
        "timeout_s": 180,
    },
}
