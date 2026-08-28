"""Structured ``context_refs`` folding for the built-in agent turn.

The deleted build123d agent loop no longer lives here.  ``apply_context_refs`` remains
because ``services.agent.agent_turn`` uses it to make GUI-selected model entities visible
to the model.
"""


def _format_context_refs_block(context_refs) -> str:
    """Render GUI/RPC ``context_refs`` as a model-visible block, or ``""``.

    Payload shape (Wave 4): list of ``{kind, id, doc_id, label}``. Absent, empty,
    or fully-malformed input is a no-op so callers can always pass the field
    through. Individual bad entries are skipped.
    """
    if not context_refs or not isinstance(context_refs, (list, tuple)):
        return ""
    lines: list[str] = []
    for raw in context_refs:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "").strip()
        rid = str(raw.get("id") or "").strip()
        if not kind or not rid:
            continue
        label = str(raw.get("label") or "").strip()
        doc_id = str(raw.get("doc_id") or raw.get("docId") or "").strip()
        line = f"- {kind}"
        if label:
            line += f' "{label}"'
        line += f" id={rid}"
        if doc_id:
            line += f" doc_id={doc_id}"
        lines.append(line)
    if not lines:
        return ""
    header = (
        "Context references (user-picked model entities — resolve/measure/target "
        "via describe_part, query_geometry, measure_between, measure_part, hover "
        "using the listed id/doc_id):"
    )
    return header + "\n" + "\n".join(lines)



def apply_context_refs(request: str, context_refs=None) -> str:
    """Append a Context-references block to *request* when refs are present.

    Public so worker/RPC entry points can fold the structured ``context_refs``
    field into the text the model sees (mirrors how file attachments are made
    visible). No-op for absent/empty/malformed refs.

    Also no-ops when the host already inlined a Context references block into
    the request text (C++ agent_run path; host dual-writes structured + text).
    """
    base = request if isinstance(request, str) else str(request or "")
    # Host (S_Composer) may already have inlined a block — don't double-inject.
    if "Context references" in base:
        return base
    block = _format_context_refs_block(context_refs)
    if not block:
        return base
    if not base:
        return block
    return base + "\n\n" + block

