"""tool_guidance.research — curated options for the research tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "fit_calc": {
        "prompt_note": (
            "Call BEFORE any hole/bore sizing decision; if the fit class is "
            "unknown, resolve function first with recommend_fit. A bare "
            "diameter alone returns a teaching error naming both modes."
        ),
    },
    "list_calcs": {
        "prompt_note": (
            "Discovery for research_eng_calc: call once to learn the "
            "registered calc_ids and their input schemas instead of guessing "
            "formula names."
        ),
    },
    "machining_ref_get": {
        "prompt_note": (
            "Fetch one curated record by stable record_id for its full conditions, units "
            "and source citation. Lookup facts are advisory; a delegate_tool record points "
            "to the canonical calculator/tool instead of duplicating its values."
        ),
    },
    "machining_ref_index_pdf": {
        "prompt_note": (
            "Index a user-owned local PDF into the project-local page FTS store; content "
            "is never bundled or uploaded. Set project_root when no project is active; "
            "scanned pages without extractable text are reported honestly, not OCR-guessed."
        ),
        "timeout_s": 300,
    },
    "machining_ref_search": {
        "prompt_note": (
            "Search the curated cited machining dataset with optional topic/material/"
            "process filters. When a hit returns delegate_tool, call that named canonical "
            "tool for the value instead of quoting or copying the reference row."
        ),
    },
    "machining_ref_search_pdf": {
        "prompt_note": (
            "Search only locally indexed user PDFs and retain each hit's source_id, hash "
            "and exact pdf_page/page_index citation. Snippets are bounded evidence; fetch "
            "or inspect the cited page before turning one into a controlled plan value."
        ),
    },
    "recommend_fit": {
        "prompt_note": (
            "Function-to-fit-class step before fit_calc; pass the diameter in "
            "the same call to get the computed band and skip the follow-up "
            "fit_calc entirely."
        ),
    },
    "research_datasheet_fetch": {
        "prompt_note": (
            "The READ tool when the source is a PDF (local path or URL) — "
            "research_fetch_page is for HTML. Degrades to ok:false/"
            "no_pdf_tool when pdfminer.six is not installed."
        ),
        "timeout_s": 180,
    },
    "research_eng_calc": {
        "prompt_note": (
            "Not an arithmetic tool (no add/sum). On an unknown-formula "
            "error, pick from the returned `known` catalogue instead of "
            "retrying spellings; list_calcs shows the schemas up front."
        ),
    },
    "research_fetch_page": {
        "prompt_note": (
            "Read a specific HTML specs/datasheet source after the parts "
            "library cannot supply a needed dimension. Capture its citation "
            "and build; a model page's CAD file belongs in fetch_cad_model."
        ),
        "timeout_s": 120,
    },
    "research_material_lookup": {
        "prompt_note": (
            "Lookup for a NAMED material, not a selector. For 'which alloy "
            "for this job' run research_web_search for candidates first, then "
            "this per alloy for the actual numbers."
        ),
    },
    "research_standard_section": {
        "prompt_note": (
            "Bundled table is small: a handful of AISC W-shapes plus NPS 1-6 "
            "pipe (sch 40/80), imperial units. On not_found it lists the "
            "covered set — fall back to research_web_search."
        ),
    },
    "research_thread_spec": {
        "prompt_note": (
            "Use before modeling or calling out any thread — designation in "
            "(M8x1.25, 1/4-20 UNC), computed minor diameters and tensile "
            "stress area out; never recall these from memory."
        ),
    },
    "research_unit_convert": {
        "prompt_note": (
            "Only length/angle/count families (to mm/deg/count) — stress "
            "units like ksi are out of scope. A unit outside its family is "
            "rejected as bad_unit, never silently coerced."
        ),
    },
    "research_web_search": {
        "prompt_note": (
            "Use for missing specs/datasheets or locating a specific CAD "
            "model URL after checking the library. For a found CAD page/file, "
            "use fetch_cad_model(url), not research_fetch_page as geometry. "
            "Without TAVILY_API_KEY the DuckDuckGo fallback is "
            "labeled confidence:limited."
        ),
        "timeout_s": 120,
        # SCOPE: this cap is per RUN — one whole user request — not per model
        # round-trip (see tool_guidance/__init__.py). It was set to 3 on
        # per-round-trip reasoning ("three searches is enough to triangulate"),
        # which as a per-RUN bound meant three web searches for an entire
        # engineering task. Live 2026-08-27: an open-ended two-product research
        # request was refused mid-work with "I could fetch more technical
        # references" as the model's very next thought. No role card bounds
        # research at any number, so nothing mandated 3.
        # 24 is ~9% of MAIN_CHAT_RUNAWAY_TOOL_CALL_BACKSTOP (256) and well
        # above any observed legitimate chain, so it can now only fire on a
        # genuine loop — while still bounding a rephrase spin at 24 paid
        # searches rather than 250. Identical repeated queries are the loop
        # guard's job (tool_loop_guard.py), not this counter's: this one counts
        # by tool NAME and cannot tell a new subtopic from a retry.
        "max_calls_per_turn": 24,
    },
}
