"""Adapter from Chudware's effective CAD tool surface to Chudcode host tools.

The adapter deliberately owns no CAD tool catalog.  Names, descriptions, schemas,
policy decisions, and dispatch all come from the same live registries used by
:mod:`services.agent.agent_turn`.
"""

from __future__ import annotations

import inspect
import json
import logging
import math
import os
import threading
import time
import types
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Union, get_args, get_origin

from services.agent import request_context as tenancy
from services.agent.tool_images import (
    ImageUnavailable,
    image_ref,
    image_refs,
    image_result,
    load_model_image,
)

_MISSING = object()
_LOG = logging.getLogger(__name__)
# Run records (``tool_calls``) are shared by every agent in a turn, including
# Chudcode task sub-agents calling concurrently on their own host-call threads.
_RECORDS_LOCK = threading.Lock()


def _debug_fallback(area: str, exc: BaseException) -> None:
    """Record an optional-path failure without emitting startup noise."""
    _LOG.debug("chudcode tool adapter fallback in %s: %s", area, exc, exc_info=exc)


# Chudcode owns these names.  A CAD registry entry may opt back in explicitly via
# ``cad_owned``/``owns_name``, and embedders may replace or extend both sets in the
# run context.  This is a namespace guard, not a second CAD tool catalog.
_CHUDCODE_BUILTINS = frozenset(
    {
        "read",
        "write",
        "edit",
        "bash",
        "grep",
        "glob",
        "task",
        "wait",
        "ask",
        "find",
        "lsp",
        "checkpoint",
        "rewind",
        "context_notes",
        "new_context",
        "learn",
        "manage_skill",
        "todo",
        "web_search",
        "eval",
        "yield",
        "security_scan",
        "ast_grep",
        "ast_edit",
        "debug",
        "github",
        "image_analyze",
        "cpp_static",
        "cpp_runtime",
        "binary_inspect",
        "firmware",
        "memory_edit",
        "retain",
        "recall",
        "reflect",
    }
)


@dataclass(frozen=True)
class HostToolBinding:
    """One Chudcode host-tool definition plus its run-scoped CAD binding."""

    name: str
    description: str
    input_schema: dict[str, Any]
    load_mode: str = "always"
    handler: Callable[..., Any] | None = field(default=None, repr=False, compare=False)
    context: Any = field(default=None, repr=False, compare=False)
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    trusted_context: dict[str, Any] = field(
        default_factory=tenancy.current_context, repr=False, compare=False,
    )
    project_root: str = field(
        default_factory=lambda: tenancy.root_value("CHUDWARE_PROJECT_DIR"), repr=False, compare=False,
    )
    save_root: str = field(
        default_factory=lambda: tenancy.root_value("CHUDWARE_PROJECT_SAVE_DIR"), repr=False, compare=False,
    )

    def as_dict(self) -> dict[str, Any]:
        """Return the dependency-free binding definition."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": _json_safe(self.input_schema),
            "load_mode": self.load_mode,
        }

    def to_host_tool(self, factory: Callable[..., Any]) -> Any:
        """Lazily construct a ``chudcode_rpc.host_tool`` without importing it."""
        if not callable(factory):
            raise TypeError("host-tool factory must be callable")

        def execute_bound(params: Mapping[str, Any] | None, host_context: Any) -> Any:
            send_update = getattr(host_context, "send_update", None)
            progress = (
                (lambda payload: send_update(host_tool_result(payload)))
                if callable(send_update)
                else None
            )
            cancel = lambda: bool(getattr(host_context, "cancelled", False))
            # A Chudcode task sub-agent inherits these bindings and calls them over
            # the parent's client; chudcode_rpc names it on the context (None for
            # the top-level agent, and on runtimes that predate the field).
            agent_id = getattr(host_context, "agent_id", None)
            tool_call_id = getattr(host_context, "tool_call_id", None)
            report = _nested_call_reporter(self, tool_call_id, agent_id, params)
            try:
                raw = dispatch_host_tool(
                    self,
                    params,
                    progress,
                    cancel,
                    agent_id=agent_id,
                    parent_agent_id=getattr(host_context, "parent_agent_id", None),
                )
                # eval's own code reads this text (json.loads), so a skill body in
                # front of the JSON broke every parse, and the model never sees the
                # text anyway. The first-touch injection waits for a direct call.
                prefix = (
                    ""
                    if _eval_bridged(tool_call_id)
                    else _skill_prefix(self, agent_id=agent_id)
                )
                prefix_bytes = len(prefix.encode("utf-8"))
                result = host_tool_result(
                    raw,
                    max_bytes=max(_RESULT_MIN_BYTES, _RESULT_TEXT_BYTES - prefix_bytes),
                    spill=lambda value: _spill_full_result(self, value),
                )
            except Exception as exc:
                report(None, exc)
                raise
            report(result, None)
            return _prefixed(self, result, prefix)

        def execute(params: Mapping[str, Any] | None, host_context: Any) -> Any:
            refusal = _binding_context_refusal(self)
            if refusal is not None:
                return host_tool_result(refusal)
            with tenancy.bind_context(self.trusted_context), tenancy.bind_roots(
                self.project_root, self.save_root,
            ):
                return execute_bound(params, host_context)

        return factory(
            name=self.name,
            description=self.description,
            parameters=_json_safe(self.input_schema),
            execute=execute,
            label=self.metadata.get("label"),
            hidden=bool(self.metadata.get("hidden", False)),
        )

    @property
    def definition(self) -> dict[str, Any]:
        return self.as_dict()


#: Chudcode spills any tool result whose text exceeds ``tools.artifactSpillThreshold``
#: (50 KB default) to an artifact, keeping a 20 KB head and tail, and the
#: ``write xd://`` device then REPLACES the host tool's ``details`` with its
#: truncation meta. The model got a head and a tail of broken JSON; eval code got
#: text no ``json.loads`` could parse and no structured fallback (c211-c230: dozens
#: of cells writing ``[describe_assembly result]``/artifact/line-number strippers,
#: a 615 KB describe_part). Results are bounded below that threshold here instead:
#: still valid JSON, the full value saved to a file the text names. The budget
#: covers any skill body prefixed to the same text.
_RESULT_TEXT_BYTES = 46_000
#: Floor for a result that shares the budget with a skill body.
_RESULT_MIN_BYTES = 16_000
#: Shortening rounds before falling back to the marker alone; each round halves every
#: node within 2x of the best saving, so real results converge in a few dozen.
_SHORTEN_ROUNDS = 200
#: Strings keep at least this many chars; objects with more keys than this are maps.
_MIN_STRING_CHARS = 80
_MAP_KEYS = 16
_TRUNCATED_KEY = "_truncated"
_SHORTENED_LISTED = 12
_TRAILING_NOTE_CHARS = 2000
#: Spilled results kept per conversation.
_SPILL_KEEP = 100


def host_tool_result(
    result: Any,
    *,
    max_bytes: int = _RESULT_TEXT_BYTES,
    spill: Callable[[Any], str | None] | None = None,
) -> dict[str, Any]:
    """Wrap a CAD tool's return value in the envelope Chudcode's RPC requires.

    A ``host_tool_result`` whose ``result`` lacks ``content`` NEVER resolves inside
    Chudcode: the ``write xd://<tool>`` dispatch waits forever, the worker's
    ``wait_for_idle`` waits with it, and the GUI shows "Thinking..." until the 2-hour
    cap (verified live with ``chud --mode rpc`` 0.3.0 for ``{"ok": false, ...}``,
    ``{"ok": true, ...}`` and ``{"docs": [...]}``; a bare string or a
    ``{"content": [...]}`` mapping completes immediately). CAD tools return
    JSON values, JSON text or plain guidance text. This is the single place the
    envelope is applied: ``details`` keeps the result for the event mapper,
    progress watchdog and eval code. Plain text stays a string in ``details``;
    JSON-text objects/arrays are parsed, so a failed ``{"ok": false}`` stays a
    failure for structured consumers.

    The text is the one copy the model pays for, on every later request of the
    conversation, so JSON goes out in :func:`model_json` form, and a result over
    *max_bytes* (UTF-8) is shortened to fit (see :func:`_bounded_result`), the
    complete value going to ``spill``'s file.
    Over-budget plain text uses the same bounded JSON envelope: its shortened
    string is in ``details.items``, with ``_truncated.full_result`` naming the
    complete JSON string. No success flag is invented for guidance text.

    Images a tool returned (render tools' ``[Image, caption]``) follow the text as
    ``{"type": "image", "mimeType", "data"}`` blocks — Chudcode passes result content
    through to the model unchanged, so this is where the pixels must be. The text keeps
    each image's path reference and the caption; ``details`` never carries base64.
    Before this, ``_json_safe`` stringified the ``Image`` object and every render the
    model asked for arrived as ``"<mcp.server.fastmcp.utilities.types.Image object at
    0x…>"`` (c205, c213, c217: the model re-rendered with ``save_path`` and ``read``
    the PNG to see anything).

    An image result goes out as the mapping :func:`tool_images.image_result` makes
    (``ok``, ``caption``, ``images``, ``saved_path``), in the text and in ``details``,
    so eval code reads a render like every other CAD result (c250: ``r.items()`` on
    ``render_part``'s list failed). Image notes go in its ``image_notes`` list, so
    the text stays one JSON value for ``json.loads``.
    """
    if isinstance(result, str):
        parsed = _json_text_value(result)
        if parsed is None:
            if _fits(result, max_bytes):
                return {"content": [{"type": "text", "text": result}], "details": result}
            return _bounded_result(result, [], [], max_bytes, spill)
        value, trailing = parsed
        safe = _json_safe(value)
        text = _compact_json_text(result)
        if _fits(text, max_bytes):
            return {"content": [{"type": "text", "text": text}], "details": safe}
        return _bounded_result(safe, [trailing], [], max_bytes, spill)
    safe = _json_safe(result)
    shaped = image_result(safe)
    if shaped is not None:
        safe = shaped
    images, notes = _model_image_blocks(safe)
    if shaped is not None and notes:
        shaped["image_notes"] = notes
        notes = []
    text = "\n".join((model_json(safe), *notes))
    if _fits(text, max_bytes):
        return {"content": [{"type": "text", "text": text}, *images], "details": safe}
    return _bounded_result(safe, notes, images, max_bytes, spill)


def _json_text_value(text: str) -> tuple[Any, str] | None:
    """(value, trailing prose) of a tool's JSON object/array text, else None."""
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        value, end = json.JSONDecoder().raw_decode(stripped)
    except ValueError:
        return None
    return value, stripped[end:].strip()


def _fits(text: str, max_bytes: int) -> bool:
    return len(text.encode("utf-8")) <= max_bytes


def _bounded_result(
    value: Any,
    notes: Sequence[str],
    images: list[dict[str, str]],
    max_bytes: int,
    spill: Callable[[Any], str | None] | None,
) -> dict[str, Any]:
    """Shorten an over-budget result to valid JSON that names where the rest is.

    Top-level keys always survive (``ok``/``error``/``doc_id`` are what a caller
    branches on). Below them, whatever shrinks the text most is halved first — the
    long list, the huge string, the id-keyed map — so small records stay whole: a
    57-component list gets shorter, each component's 16-number transform does not.
    Every cut string ends with a visible ``…[cut: …]`` marker (:func:`_cut_marker`),
    so no reader takes a cut string for the whole text. A ``_truncated`` key records
    the shortened paths with their original sizes and ``full_result``, a JSON file
    holding the complete value. ``details`` carries the same shortened value, so
    structured consumers, eval-bridged calls and the text never disagree.
    """
    full_bytes = len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    path: str | None = None
    if spill is not None:
        try:
            path = spill(value)
        except Exception as exc:  # noqa: BLE001 - a failed save never fails the call
            _debug_fallback("tool result spill", exc)
    tail = [
        note if len(note) <= _TRAILING_NOTE_CHARS else note[:_TRAILING_NOTE_CHARS] + " …"
        for note in notes
        if note
    ]
    holder = [json.loads(json.dumps(value, ensure_ascii=False, default=str))]
    shortened: dict[str, int] = {}
    kept: dict[str, str] = {}
    for _round in range(_SHORTEN_ROUNDS):
        body = _with_truncation_marker(holder[0], shortened, full_bytes, max_bytes, path)
        text = "\n".join((model_json(body), *tail))
        if _fits(text, max_bytes):
            return {"content": [{"type": "text", "text": text}, *images], "details": body}
        if not _shorten_largest(holder, shortened, kept, saved=path is not None):
            break
    body = _with_truncation_marker(None, {"$": full_bytes}, full_bytes, max_bytes, path)
    return {"content": [{"type": "text", "text": model_json(body)}, *images], "details": body}


def _shorten_largest(
    holder: list[Any], shortened: dict[str, int], kept: dict[str, str], *, saved: bool
) -> bool:
    """Halve every node whose halving saves at least half the best saving; False when
    nothing can shrink. Lists keep their head (≥1 item), strings their start, and only
    map-like objects (more than ``_MAP_KEYS`` keys, below the top level) lose keys.

    A cut string keeps its start plus a :func:`_cut_marker` suffix. *kept* maps each
    cut string's path to the start it kept, so a later round halves that start and
    writes a fresh marker instead of cutting into the old one. *saved* says whether
    ``_truncated.full_result`` names a file the marker can point to."""
    candidates: list[tuple[int, Any, Any, str, Any]] = []

    def size(node: Any, container: Any, key: Any, where: str, depth: int) -> int:
        if isinstance(node, str):
            text = kept.get(where, node)
            if len(text) > _MIN_STRING_CHARS:
                start = max(_MIN_STRING_CHARS, len(text) // 2)
                marker = _cut_marker(start, shortened.get(where, len(node)), saved)
                if start + len(marker) < len(node):
                    candidates.append(
                        (len(node) - start - len(marker), container, key, where, text)
                    )
            return len(node) + 2
        if isinstance(node, list):
            sizes = [
                size(item, node, index, f"{where}[{index}]", depth + 1)
                for index, item in enumerate(node)
            ]
            if len(node) > 1:
                candidates.append((sum(sizes[len(node) // 2:]), container, key, where, node))
            return 2 + sum(sizes) + len(sizes)
        if isinstance(node, dict):
            sizes = [
                size(item, node, name, f"{where}.{name}", depth + 1) + len(name) + 4
                for name, item in node.items()
            ]
            if depth > 0 and len(node) > _MAP_KEYS:
                candidates.append((sum(sizes[len(node) // 2:]), container, key, where, node))
            return 2 + sum(sizes)
        return len(json.dumps(node, default=str))

    size(holder[0], holder, 0, "$", 0)
    if not candidates:
        return False
    best = max(gain for gain, *_ in candidates)
    for gain, container, key, where, node in candidates:
        if gain * 2 < best:
            continue
        shortened.setdefault(where, len(node))
        if isinstance(node, str):
            start = node[: max(_MIN_STRING_CHARS, len(node) // 2)]
            kept[where] = start
            container[key] = start + _cut_marker(len(start), shortened[where], saved)
        elif isinstance(node, list):
            container[key] = node[: len(node) // 2]
        else:
            container[key] = dict(list(node.items())[: len(node) // 2])
    return True


def _cut_marker(kept_chars: int, total_chars: int, saved: bool) -> str:
    """Visible suffix of a cut string: what it kept, and where the full text is."""
    where = (
        "full text in _truncated.full_result"
        if saved
        else "full text not saved; narrow the call"
    )
    return f" …[cut: kept {kept_chars} of {total_chars} characters; {where}]"


def _with_truncation_marker(
    body: Any,
    shortened: Mapping[str, int],
    full_bytes: int,
    max_bytes: int,
    path: str | None,
) -> dict[str, Any]:
    listed = dict(list(shortened.items())[:_SHORTENED_LISTED])
    marker: dict[str, Any] = {
        "full_result": path,
        "full_bytes": full_bytes,
        "shortened": listed,
        "note": (
            f"This result is {full_bytes} bytes, over the {max_bytes}-byte tool-result "
            "budget, so the lists/objects/strings listed under 'shortened' (path: "
            "original size) keep only their first entries. Each cut string ends with "
            "a '…[cut: …]' marker. Do not parse a cut string as complete text. "
            + (
                "The complete JSON is saved at the full_result path: load it with "
                "json.load(open(path)) in eval, or read it with a line range."
                if path
                else "The complete result could not be saved; narrow the call instead."
            )
        ),
    }
    if len(shortened) > len(listed):
        marker["shortened_paths_total"] = len(shortened)
    if isinstance(body, Mapping):
        return {**body, _TRUNCATED_KEY: marker}
    if body is None:
        return {_TRUNCATED_KEY: marker}
    return {"items": body, _TRUNCATED_KEY: marker}


def _spill_full_result(binding: HostToolBinding, value: Any) -> str | None:
    """Write a complete over-budget result under the conversation's cache dir."""
    from services import paths

    conversation = str(
        _ctx(binding.context, "session_id", None)
        or _ctx(binding.context, "run_id", None)
        or "adhoc"
    )
    safe_conversation = "".join(
        ch if ch.isalnum() or ch in "._-" else "_" for ch in conversation
    )[:80] or "adhoc"
    folder = (
        tenancy.actor_state_root() / "cache" / "tool_results"
        if tenancy.in_cell()
        else Path(paths.ROOT) / "cache" / "tool_results" / safe_conversation
    )
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    target = folder / f"{stamp}-{binding.name}-{uuid.uuid4().hex[:8]}.json"
    staging = target.with_suffix(".json.part")
    staging.write_text(
        json.dumps(value, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    os.replace(staging, target)
    kept = sorted(folder.glob("*.json"), key=lambda item: item.stat().st_mtime)
    for stale in kept[:-_SPILL_KEEP]:
        try:
            stale.unlink()
        except OSError as exc:
            _debug_fallback("tool result spill prune", exc)
    return str(target)


def _eval_bridged(tool_call_id: Any) -> bool:
    """True for a CAD call made from eval code (``tool.write("xd://…")``).

    Chudcode's eval bridge mints ``js-<tool>-<uuid>`` call ids for the Python and
    JS kernels alike; model tool calls carry the provider's id.
    """
    return isinstance(tool_call_id, str) and tool_call_id.startswith("js-")


def _model_image_blocks(safe: Any) -> tuple[list[dict[str, str]], list[str]]:
    """Image content blocks for every image reference in *safe*, plus model notes.

    An image that cannot be loaded is named in a note rather than silently missing,
    and one re-encoded to fit the limits says so, so the model never reasons about
    pixels it did not get.
    """
    blocks: list[dict[str, str]] = []
    notes: list[str] = []
    for ref in image_refs(safe):
        path = ref["path"]
        try:
            image = load_model_image(path)
        except ImageUnavailable as exc:
            notes.append(f"[image not attached: {exc}]")
            continue
        blocks.append(image.block())
        if image.reencoded:
            notes.append(image.note(path))
    return blocks, notes


def _nested_call_reporter(
    binding: HostToolBinding, tool_call_id: Any, agent_id: Any, params: Any
) -> Callable[[Any, BaseException | None], None]:
    """Report one host call to the turn's observer as start/end tool frames.

    The observer (bound by ``ChudcodeSessionHost.run_turn``) forwards only calls no
    visible Chudcode tool row covers, i.e. ones made from inside eval. A task
    sub-agent's calls already arrive as its own ``subagent_event`` rows.
    """
    observer = _ctx(binding.context, "host_call_observer", None)
    if not callable(observer) or agent_id or not isinstance(tool_call_id, str):
        return lambda _result, _error: None
    args = dict(params) if isinstance(params, Mapping) else {}
    started = time.monotonic()

    def send(frame: dict[str, Any]) -> None:
        try:
            observer(frame)
        except Exception as exc:  # noqa: BLE001 - a feed row never fails the CAD call
            _debug_fallback("nested host-call report", exc)

    send({"type": "host_tool_start", "toolCallId": tool_call_id,
          "toolName": binding.name, "args": args})

    def finish(result: Any, error: BaseException | None) -> None:
        frame: dict[str, Any] = {
            "type": "host_tool_end",
            "toolCallId": tool_call_id,
            "toolName": binding.name,
            "args": args,
            "durationMs": int((time.monotonic() - started) * 1000),
        }
        if error is not None:
            frame.update(isError=True, error=f"{type(error).__name__}: {error}")
        else:
            details = result.get("details") if isinstance(result, Mapping) else None
            frame.update(result=result,
                         ok=not (isinstance(details, Mapping) and details.get("ok") is False))
        send(frame)

    return finish


#: A JSON container that fits on one line this wide stays on one line.
_MODEL_JSON_WIDTH = 100


def model_json(value: Any) -> str:
    """Render JSON for a model: containers that fit ``_MODEL_JSON_WIDTH`` stay inline.

    ``indent=2`` spends a line (and its indent) on every scalar, so a feature tree's
    ``[[1, 41], [1, 43], …]`` topology pairs ran to 4,000 lines and the 65 KB
    ``get_part`` result spilled to an artifact the model then had to page (c219).
    Fully compact JSON would be one giant line, and Chudcode's head/tail elision keeps
    lines, so a spilled one-line result loses its head. This form is valid JSON, keeps
    one line per record, and on the c219 results is 0.4–0.8x the ``indent=2`` size.
    """
    return _model_json_node(value, 0)[1]


def _model_json_node(value: Any, depth: int) -> tuple[str, str]:
    """(one-line form, rendered form) of ``value`` nested ``depth`` levels deep."""
    if isinstance(value, Mapping) and value:
        items = [
            (json.dumps(str(key), ensure_ascii=False), _model_json_node(item, depth + 1))
            for key, item in value.items()
        ]
        inline = "{" + ",".join(f"{key}:{node[0]}" for key, node in items) + "}"
        if depth + len(inline) <= _MODEL_JSON_WIDTH:
            return inline, inline
        pad = " " * (depth + 1)
        body = ",\n".join(f"{pad}{key}:{node[1]}" for key, node in items)
        return inline, "{\n" + body + "\n" + " " * depth + "}"
    if isinstance(value, (list, tuple)) and value:
        nodes = [_model_json_node(item, depth + 1) for item in value]
        inline = "[" + ",".join(node[0] for node in nodes) + "]"
        if depth + len(inline) <= _MODEL_JSON_WIDTH:
            return inline, inline
        pad = " " * (depth + 1)
        body = ",\n".join(pad + node[1] for node in nodes)
        return inline, "[\n" + body + "\n" + " " * depth + "]"
    scalar = json.dumps(value, ensure_ascii=False, default=str)
    return scalar, scalar


def _compact_json_text(text: str) -> str:
    """Re-render a tool's JSON text (plus any trailing prose notes) in model form.

    Text that is not a JSON object/array, or that would not get shorter, is returned
    unchanged — this is a size optimisation, never a content change.
    """
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        return text
    try:
        value, end = json.JSONDecoder().raw_decode(stripped)
    except ValueError:
        return text
    rendered = model_json(value) + stripped[end:]
    return rendered if len(rendered) < len(text) else text


def _skill_prefix(binding: HostToolBinding, *, agent_id: str | None = None) -> str:
    """Trigger-matched skill bodies for this tool's FIRST result, else ``""``.

    The offline loop injects a skill's body when a turn is about to reach one of
    its ``triggers.tools`` (``agent_turn`` calls ``skills.activate_for_prompt``
    with ``tools_about_to_run``). Online, the model's turn is inside Chudcode and
    the only CAD chokepoint left is this dispatch, so the body rides back on the
    tool's own result — the first time that conversation touches the tool, once,
    within ``skills.SKILL_BODY_CHAR_LIMIT``. Activation marks the body delivered,
    so call this only for a result that will carry it. A Chudcode task sub-agent
    (``agent_id``) has its own context window, so it gets its own first-touch
    injection rather than inheriting the parent's.
    """
    conversation = str(
        _ctx(binding.context, "session_id", None)
        or _ctx(binding.context, "run_id", None)
        or ""
    )
    if conversation and agent_id:
        conversation = f"{conversation}:{agent_id}"
    try:
        from services.agent import skills

        bodies = skills.activate_for_prompt(
            "",
            session_id=conversation or None,
            project_root=binding.project_root or None,
            tools_about_to_run=[binding.name],
            role=_ctx(binding.context, "actor", None),
            _aggregate_char_limit=skills.SKILL_BODY_CHAR_LIMIT,
        )
    except Exception as exc:  # noqa: BLE001 — a broken skills dir never fails a call
        _debug_fallback(f"skill injection for {binding.name}", exc)
        return ""
    return "\n\n".join(bodies) if bodies else ""


def _prefixed(
    binding: HostToolBinding, envelope: dict[str, Any], prefix: str
) -> dict[str, Any]:
    """Put *prefix* (skill bodies) in front of the result's first text block.

    ``details`` is untouched, so the event mapper and every structured consumer
    see exactly the tool's own result.
    """
    content = envelope.get("content")
    if not prefix or not isinstance(content, list) or not content:
        return envelope
    first = content[0]
    if not isinstance(first, dict) or first.get("type") != "text":
        return envelope
    merged = dict(envelope)
    merged["content"] = [
        {
            **first,
            "text": f"{prefix}\n\n[{binding.name} result]\n{first.get('text') or ''}",
        },
        *content[1:],
    ]
    return merged

# --------------------------------------------------------------------------- context


def _ctx(context: Any, name: str, default: Any = None) -> Any:
    if isinstance(context, Mapping):
        return context.get(name, default)
    return getattr(context, name, default)


def _ctx_present(context: Any, name: str) -> bool:
    if isinstance(context, Mapping):
        return name in context
    return hasattr(context, name)


def _context_roots(context: Any) -> tuple[str, str]:
    """Capture the exact selected roots before the turn ContextVars are bound."""
    raw_project = _ctx(context, "project_dir", None)
    if raw_project is None:
        raw_project = _ctx(context, "workspace_dir", None)
    explicit_project = raw_project is not None
    if not explicit_project:
        raw_project = (
            tenancy.root_value("CHUDWARE_PROJECT_DIR")
            or tenancy.root_value("CHUDWARE_FS_ROOT")
        )
    raw_project = str(raw_project or "").strip()
    if not raw_project:
        return "", ""
    project = str(Path(raw_project).expanduser().resolve())
    raw_save = _ctx(context, "save_dir", None)
    if raw_save is None:
        raw_save = _ctx(_ctx(context, "options", None), "save_dir", None)
    if raw_save is None:
        raw_save = (
            project if explicit_project
            else tenancy.root_value("CHUDWARE_PROJECT_SAVE_DIR")
        )
    save = Path(str(raw_save or project)).expanduser()
    if not save.is_absolute():
        save = Path(project) / save
    return project, str(save.resolve())


def _ordered_unique(values: Sequence[Any] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or ():
        name = str(raw or "").strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _role_card(context: Any) -> Any | None:
    card = _ctx(context, "role_card", _ctx(context, "role", None))
    if card is None:
        card = _ctx(context, "role_id", None)
    if isinstance(card, str) and card.strip():
        from services.agent.roles import load_role

        return load_role(card.strip())
    return card


def _role_policy_tools(role: Any) -> list[str] | None:
    """Resolve a role through ``RoleCard.apply_tool_policy`` without mutating globals."""
    apply_policy = getattr(role, "apply_tool_policy", None)
    if not callable(apply_policy):
        return list(getattr(role, "concrete_tools", ()) or ())

    class _Capture:
        value: Any = _MISSING

        def set_policy(self, allow=None, **_kwargs):
            self.value = allow

    capture = _Capture()
    try:
        tokens = apply_policy(capture)
    except Exception as exc:  # noqa: BLE001 - role/config plug-ins are heterogeneous
        _debug_fallback("role policy", exc)
        return list(getattr(role, "concrete_tools", ()) or ())
    if capture.value is None:
        return None

    resolved: list[str] = []
    try:
        from services.agent.tool_categories import _expand_token

        for token in tokens or ():
            values = sorted(_expand_token(str(token)))
            resolved.extend(
                values or ([str(token)] if not str(token).startswith("family:") else [])
            )
    except Exception as exc:  # noqa: BLE001 - optional catalog family expansion
        _debug_fallback("role family expansion", exc)
        resolved = [
            str(token) for token in tokens or () if not str(token).startswith("family:")
        ]
    # ``concrete_tools`` supplies the role module's curated family fallback when
    # the MCP catalog is intentionally cold (unit tests and early worker start).
    return _ordered_unique([*resolved, *(getattr(role, "concrete_tools", ()) or ())])


def _effective_names(context: Any, available: Sequence[str]) -> list[str]:
    """Finalize the run allowlist with the live role/mode/category helpers.

    The three allowlist states remain distinct: ``None`` is unrestricted, ``[]``
    is restricted to the live meta/config floors, and a non-empty sequence is an
    ordinary restriction.

    Order matters: on-demand loads (``enable_tools`` tokens in ``categories``)
    are unioned BEFORE the mode policy runs, so a user-picked read-only mode
    (ask/plan) stays read-only no matter what the model loads mid-turn. The
    policy is a user choice; the loads are the model's.

    The mode policy and its floors are not optional: an import or evaluation
    error propagates (the caller refuses the turn) instead of advertising the
    un-narrowed surface.
    """
    role = _role_card(context)
    explicit = _ctx_present(context, "allowed_tools")
    raw_allow = _ctx(context, "allowed_tools", None)
    restricted = explicit and raw_allow is not None

    if explicit:
        names = None if raw_allow is None else _ordered_unique(raw_allow)
    elif role is not None:
        names = _role_policy_tools(role)
        restricted = names is not None
    else:
        names = None

    categories = _ctx(context, "categories", _ctx(context, "enabled_categories", ()))
    if categories:
        from services.agent.tool_categories import META_TOOLS, resolve_tool_requests

        expanded, _unknown = resolve_tool_requests(categories)
        if names is None:
            names = []
            restricted = True
        names = _ordered_unique([*names, *sorted(META_TOOLS), *sorted(expanded)])

    mode = _ctx(context, "mode", None)
    plan_only = _ctx(context, "plan_only", None)
    from services.agent.config import bugreport_floor
    from services.agent.copilot_mode_tools import (
        apply_meta_tool_floor,
        apply_mode_tools,
    )
    from services.agent.guardrails import MUTATING_TOOLS

    # Plan narrows the SAME set agent would get (minus mutators + the shared
    # floor) rather than the contract's broad PLAN_TOOLS list, so the main
    # chat's Plan mode is "Agent, read-only" and costs no extra context.
    narrowed = apply_mode_tools(
        mode,
        names,
        plan_only=plan_only,
        subagent=bool(
            _ctx(context, "subagent_id", None) or _ctx(context, "subagent", False)
        ),
        preferences=_ctx(context, "turn_preferences", None),
    )
    if narrowed is not None:
        names = _ordered_unique(narrowed)
        restricted = True

    if names is not None:
        floored = apply_meta_tool_floor(names)
        if floored is not None:
            names = _ordered_unique(floored)
        names = _ordered_unique([*names, *bugreport_floor()])
        if set(names).intersection(MUTATING_TOOLS):
            names = _ordered_unique([*names, "verify_battery"])
    preferences = _ctx(context, "turn_preferences", None)
    if preferences is not None:
        from services.agent.copilot_mode_tools import apply_preference_tools

        return apply_preference_tools(names if names is not None else available,
                                      preferences, mode=mode)

    if names is None and not restricted:
        # Unrestricted: the whole live registry. `enable_tools` and
        # `list_tool_categories` are turn-loop primitives, not registry entries, so an
        # unrestricted surface is the ONE case that loses them -- the meta floor above is
        # guarded on `names is not None`. That is backwards: they are the model's on-ramp
        # for managing a large tool surface, and a roleless `agent` turn (allowed_tools
        # explicitly None, which is what agent_admin_turn sends) is exactly where the
        # surface is largest. Live symptom: the model reasoned "I'll enable the modeling
        # and sketch tool categories", found no such tool, and fell back to `bash` and
        # `write` for the rest of the turn without ever touching CAD geometry.
        unrestricted = _ordered_unique(available)
        floored = apply_meta_tool_floor(unrestricted)
        if floored is not None:
            # Append only what the floor ADDS. Live registry order is this list's
            # contract (it is the order the model sees), and the floor helper returns
            # its own ordering, so adopting it wholesale would reshuffle the catalog.
            present = set(unrestricted)
            unrestricted = [
                *unrestricted,
                *(name for name in floored if name not in present),
            ]
        return unrestricted
    return _ordered_unique(names or ())


# --------------------------------------------------------------------------- registry


def _entry_value(entry: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(entry, Mapping) and name in entry:
            return entry[name]
        if hasattr(entry, name):
            return getattr(entry, name)
    return default


def _tool_manager_entries(owner: Any) -> Mapping[str, Any] | None:
    if owner is None:
        return None
    if isinstance(owner, Mapping):
        return owner
    manager = getattr(owner, "_tool_manager", None)
    for candidate in (
        getattr(manager, "_tools", None),
        getattr(owner, "_tools", None),
        getattr(owner, "tools", None),
    ):
        if isinstance(candidate, Mapping):
            return candidate
    return None


def _load_registry_entries(context: Any) -> dict[str, Any]:
    injected = _ctx(context, "tool_entries", _MISSING)
    if injected is _MISSING:
        injected = _ctx(context, "tool_registry", _MISSING)
    entries = _tool_manager_entries(injected) if injected is not _MISSING else None
    if entries is None:
        # No injected registry: the live MCP server is the registry. An import
        # failure propagates — an empty surface is not a successful build.
        from services.mcp.server import mcp

        entries = _tool_manager_entries(mcp)
    return {str(name): entry for name, entry in (entries or {}).items()}


def _mission_names(context: Any) -> list[str]:
    mission = _ctx(context, "mission_dispatch", None)
    if mission is None:
        return []
    for attr in ("handlers", "_handlers", "tools", "tool_names"):
        value = getattr(mission, attr, None)
        if isinstance(value, Mapping):
            return _ordered_unique(list(value))
        if isinstance(value, (list, tuple, set, frozenset)):
            return _ordered_unique(list(value))
    return []


def _resolver(context: Any) -> Callable[[str], Any]:
    supplied = _ctx(context, "resolver", None)
    if callable(supplied):
        return supplied
    from services.agent.agent_turn import resolver_for

    return resolver_for(_ctx(context, "mission_dispatch", None))


def _policy_refusal(name: str, context: Any) -> dict[str, Any] | None:
    """The CAD dispatch-policy refusal for *name*, or None when it may run.

    ``policy_registry`` in the context is the injection seam; otherwise the live
    ``tool_registry`` decides. Import or evaluation errors propagate, so a
    broken policy never advertises or dispatches the tool.
    """
    registry = _ctx(context, "policy_registry", None)
    if registry is None:
        from services.mcp import tool_registry as registry
    deny = getattr(registry, "deny_reason", None)
    if not callable(deny):
        allowed = getattr(registry, "is_allowed", None)
        if callable(allowed) and not allowed(name):
            return {"ok": False, "reason": f"tool {name!r} is not permitted"}
        return None
    reason = deny(name)
    if not reason:
        return None
    refusal = getattr(registry, "refusal", None)
    if callable(refusal):
        return _json_safe(refusal(name))
    return {"ok": False, "reason": str(reason)}


# --------------------------------------------------------------------------- schemas


def _annotation_schema(annotation: Any) -> dict[str, Any]:
    if annotation in (inspect.Parameter.empty, Any, object, None):
        return {}
    if isinstance(annotation, str):
        low = annotation.lower()
        if "bool" in low:
            return {"type": "boolean"}
        if "int" in low:
            return {"type": "integer"}
        if "float" in low or "number" in low:
            return {"type": "number"}
        if "list" in low or "tuple" in low or "set" in low:
            return {"type": "array"}
        if "dict" in low or "mapping" in low:
            return {"type": "object"}
        return {"type": "string"}
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, types.UnionType):
        variants = [_annotation_schema(arg) for arg in args if arg is not type(None)]
        if len(variants) == 1:
            return variants[0]
        return {"anyOf": variants} if variants else {}
    if origin in (list, tuple, set, frozenset, Sequence):
        return {"type": "array", "items": _annotation_schema(args[0]) if args else {}}
    if origin in (dict, Mapping):
        return {"type": "object"}
    if annotation is str:
        return {"type": "string"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation in (dict, Mapping):
        return {"type": "object"}
    if annotation in (list, tuple, set, frozenset):
        return {"type": "array"}
    return {}


def _schema_from_callable(fn: Any) -> dict[str, Any]:
    if not callable(fn):
        return {"type": "object", "properties": {}}
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        _debug_fallback("callable schema inspection", exc)
        return {"type": "object", "properties": {}}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in signature.parameters.values():
        if parameter.name in {"self", "cls", "ctx", "context", *_HOST_INJECTED_PARAMS}:
            continue
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        schema = _annotation_schema(parameter.annotation)
        if parameter.default is not inspect.Parameter.empty:
            schema = dict(schema)
            schema["default"] = _json_safe(parameter.default)
        else:
            required.append(parameter.name)
        properties[parameter.name] = schema
    out: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        out["required"] = required
    return out


def _var_param_names(fn: Any) -> frozenset[str]:
    """Names of *fn*'s ``*args`` / ``**kwargs`` catch-alls.

    A catch-all is not a parameter a caller can name, so it must never appear in
    the tool schema handed to the model.
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        _debug_fallback("catch-all probe", exc)
        return frozenset()
    return frozenset(
        parameter.name
        for parameter in signature.parameters.values()
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD)
    )


#: Callback parameters supplied only by the host, never model arguments.
_HOST_INJECTED_PARAMS = ("progress", "cancel", "ask_human", "wake")


def _without_catch_alls(schema: dict[str, Any], fn: Any) -> dict[str, Any]:
    """Drop catch-all and host-injected pseudo-properties from a registry schema.

    FastMCP derives a tool's ``inputSchema`` from its signature through Pydantic,
    which models a ``**extra: Any`` catch-all as a field named ``extra`` — and,
    with no default to give it, marks that field REQUIRED. Chudcode validates
    every host call against the schema it was given, so the advertised contract
    made a satisfiable call impossible: ``task_list`` failed 100% of the time
    with ``Validation failed for tool "task_list": - extra: is required``, and
    the live plan panel therefore never published a single item. Thirty-four
    further tools advertised the same phantom argument as optional, inviting the
    model to fill it with junk that lands in the tool's user-intent channel.
    A declared ``progress``/``cancel``/``wake`` is the host's callable, never model input.
    """
    names = _var_param_names(fn) | {
        name for name in _HOST_INJECTED_PARAMS if callable(fn) and _accepts(fn, name)
    }
    if not names:
        return schema
    out = dict(schema)
    properties = out.get("properties")
    if isinstance(properties, Mapping):
        kept = {
            key: value for key, value in properties.items() if key not in names
        }
        if len(kept) != len(properties):
            out["properties"] = kept
    required = out.get("required")
    if isinstance(required, Sequence) and not isinstance(required, (str, bytes)):
        kept_required = [item for item in required if item not in names]
        if len(kept_required) != len(required):
            if kept_required:
                out["required"] = kept_required
            else:
                out.pop("required", None)
    return out


def _entry_schema(entry: Any, fn: Any) -> dict[str, Any]:
    schema = _entry_value(
        entry,
        "input_schema",
        "inputSchema",
        "parameters",
        "parameter_schema",
        default=None,
    )
    if callable(schema):
        try:
            schema = schema()
        except TypeError as exc:
            _debug_fallback("callable schema", exc)
            schema = None
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            _debug_fallback("JSON schema decoding", exc)
            schema = None
    if not isinstance(schema, Mapping):
        dump = getattr(schema, "model_dump", None)
        json_schema = getattr(schema, "model_json_schema", None)
        try:
            if callable(dump):
                schema = dump(mode="json")
            elif callable(json_schema):
                schema = json_schema()
        except Exception as exc:  # noqa: BLE001 - supports Pydantic v1/v2 schema objects
            _debug_fallback("schema object conversion", exc)
            schema = None
    if isinstance(schema, Mapping):
        return _without_catch_alls(_json_safe(schema), fn)
    return _schema_from_callable(fn)


def _entry_description(entry: Any, fn: Any) -> str:
    description = _entry_value(entry, "description", "help", default="")
    if not description and callable(fn):
        description = inspect.getdoc(fn) or ""
    return str(description or "").strip()


def _auto_skill_tools(project_root: str, role: str | None) -> dict[str, list[str]]:
    """Derive description pointers from the selected project's live skill catalog."""
    if not project_root:
        return {}
    try:
        from services.agent import skills

        by_tool: dict[str, list[str]] = {}
        for skill in skills.discover_skills(project_root=project_root):
            if skill.inject != "auto" or (skill.roles and role not in skill.roles):
                continue
            for tool in skill.triggers.tools:
                by_tool.setdefault(tool, []).append(skill.name)
        return by_tool
    except Exception as exc:  # noqa: BLE001 — optional skills never hide CAD tools
        _debug_fallback("skill description pointers", exc)
        return {}


def _description_guidance(name: str, description: str, skill_names: Sequence[str]) -> str:
    """Keep the registry summary first, then expose the effective dispatch guidance."""
    from services.agent.config import get_tool_options, tool_timeout_s_default

    options = get_tool_options(None, name) or {}
    sections = [description]
    note = str(options.get("prompt_note") or "").strip()
    if note:
        sections.append(f"Note: {note}")
    timeout = options.get("timeout_s")
    if timeout is None:
        timeout = tool_timeout_s_default()
    try:
        timeout_s = float(timeout or 0)
    except (TypeError, ValueError):
        timeout_s = 0
    if math.isfinite(timeout_s) and timeout_s > 0:
        sections.append(
            f"Wait timeout: {timeout_s:g} s per invocation. Expiry returns ok:false "
            "with timed_out:true and status:still_running; native work may still "
            "complete or mutate state and is not cancelled. Repeat the exact same "
            "invocation (arguments, document and agent) in this run to wait up to "
            "the same bound again or retrieve its original result, without starting "
            "duplicate work. Recovery does not survive the end of the run."
        )
    if skill_names:
        sections.append("Skill packs: " + "; ".join(
            f"cad_skill(name={json.dumps(skill_name)})" for skill_name in skill_names
        ))
    return "\n\n".join(sections)


def _entry_load_mode(name: str, entry: Any) -> str:
    from services.agent.config import get_tool_options

    value = _entry_value(entry, "load_mode", "loadMode", default=None)
    options = get_tool_options(None, name) or {}
    return str(options.get("load_mode", value) or "always")


def _collision_allowed(name: str, entry: Any, context: Any) -> bool:
    builtins = set(_ctx(context, "chudcode_builtin_names", _CHUDCODE_BUILTINS) or ())
    builtins.update(_ctx(context, "additional_builtin_names", ()) or ())
    owned = set(
        _ctx(context, "cad_owned_names", _ctx(context, "cad_owned_collisions", ()))
        or ()
    )
    owns_entry = bool(_entry_value(entry, "cad_owned", "owns_name", default=False))
    return name not in builtins or name in owned or owns_entry


def _meta_handler(name: str) -> Callable[..., Any] | None:
    # These two calls are turn-loop primitives rather than MCP registrations.
    # Giving them real callables lets the host schema path treat them exactly
    # like the rest of the effective surface without inventing registry entries.
    #
    # The docstrings are load-bearing: with no registry entry to read a
    # description from, _entry_description falls back to inspect.getdoc, and
    # Chudcode rejects a whole set_host_tools call that carries a tool with an
    # empty description. Both names sit on the shared floor of every mode, so an
    # undocumented handler here failed EVERY turn with
    # `provider_error - set_host_tools: Host tool "enable_tools" must provide a
    # non-empty description` before a single tool ran.
    if name == "list_tool_categories":

        def list_tool_categories() -> dict[str, Any]:
            """List every registered CAD tool grouped by area.

            Returns each group's name, what it covers, and the exact tool names
            in it. The same names are what enable_tools accepts, as a group or
            one tool at a time.
            """
            from services.agent.agent_turn import _tool_category_catalog_result

            return _tool_category_catalog_result()

        return list_tool_categories
    if name == "enable_tools":

        def enable_tools(
            categories: list[str] | None = None, tools: list[str] | None = None
        ) -> dict[str, Any]:
            """Load more CAD tools into this turn's callable tool set.

            `tools` takes exact tool names from the MORE TOOLS menu (or
            list_tool_categories); `categories` takes whole group names. The
            schemas arrive immediately and stay for the rest of the turn.
            Unknown names are reported back, never silently dropped.
            """
            from services.agent.tool_categories import resolve_tool_requests

            tokens = [*(categories or ()), *(tools or ())]
            added, unknown = resolve_tool_requests(tokens)
            return {
                "ok": True,
                "enabled": _ordered_unique(tokens),
                "added": sorted(added),
                "unknown": unknown,
            }

        return enable_tools
    return None


# --------------------------------------------------------------------------- public API


def build_host_tools(context: Any) -> list[HostToolBinding]:
    """Build Chudcode definitions from the effective live CAD tool registry."""
    entries = _load_registry_entries(context)
    available = _ordered_unique([*entries.keys(), *_mission_names(context)])
    names = _effective_names(context, available)
    project_root, save_root = _context_roots(context)
    skill_tools = _auto_skill_tools(project_root, _ctx(context, "actor", None))
    from services.agent.tool_categories import AGENT_DENIED_TOOLS
    resolve = _resolver(context)
    call_counts = _ctx(context, "tool_call_counts", None)
    if call_counts is None:
        call_counts = {}
    from services.agent.agent_turn import _ToolCallRecovery

    recovery = _ctx(context, "tool_call_recovery", None)
    if recovery is None:
        recovery = _ToolCallRecovery()
        if isinstance(context, dict):
            recovery = context.setdefault("tool_call_recovery", recovery)
        elif context is not None:
            try:
                setattr(context, "tool_call_recovery", recovery)
            except (AttributeError, TypeError):
                pass  # Immutable contexts retain the run-owned metadata reference.
    call_records = _ctx(context, "tool_calls", _ctx(context, "calls", None))
    if call_records is None:
        call_records = []
    out: list[HostToolBinding] = []
    for name in names:
        entry = entries.get(name)
        if name in AGENT_DENIED_TOOLS:
            continue
        if not _collision_allowed(name, entry, context):
            continue
        refusal = _policy_refusal(name, context)
        if refusal is not None:
            continue
        try:
            fn = resolve(name)
        except Exception as exc:  # noqa: BLE001 - injected resolvers may be plug-ins
            _debug_fallback(f"resolver for {name}", exc)
            fn = None
        if fn is None and entry is not None:
            candidate = _entry_value(entry, "fn", "handler", "callable", default=None)
            try:
                from services.agent.agent_turn import _directly_callable

                fn = candidate if _directly_callable(candidate) else None
            except Exception as exc:  # noqa: BLE001 - live resolver import is optional
                _debug_fallback(f"callability check for {name}", exc)
                fn = (
                    candidate
                    if callable(candidate)
                    and not inspect.iscoroutinefunction(candidate)
                    else None
                )
        if fn is None:
            fn = _meta_handler(name)
        # An advertised but unbound name is not a host tool.  This matches the
        # live resolver and prevents Chudcode from receiving a schema for a call
        # that CAD would reject as known-but-unbound.
        try:
            from services.agent.agent_turn import _directly_callable

            if not _directly_callable(fn):
                continue
        except Exception as exc:  # noqa: BLE001 - live resolver import is optional
            _debug_fallback(f"final callability check for {name}", exc)
            if not callable(fn) or inspect.iscoroutinefunction(fn):
                continue
        description = _entry_description(entry, fn)
        preferences = _ctx(context, "turn_preferences", None)
        if preferences is not None and preferences.trim_tool_schemas:
            from services.agent.tool_schemas import _description_line

            description = _description_line(description, trim=True)
        description = _description_guidance(name, description, skill_tools.get(name, ()))
        out.append(
            HostToolBinding(
                name=name,
                description=description,
                input_schema=_entry_schema(entry, fn),
                load_mode=_entry_load_mode(name, entry),
                handler=fn,
                context=context,
                project_root=project_root,
                save_root=save_root,
                metadata={
                    "registry_entry": entry,
                    "tool_call_counts": call_counts,
                    "tool_call_recovery": recovery,
                    "tool_calls": call_records,
                },
            )
        )
    return out


def _cancelled(cancel: Any) -> bool:
    if cancel is None:
        return False
    if isinstance(cancel, bool):
        return cancel
    for attr in ("is_set", "cancelled", "is_cancelled"):
        value = getattr(cancel, attr, None)
        if callable(value):
            try:
                return bool(value())
            except TypeError as exc:
                _debug_fallback(f"cancellation probe {attr}", exc)
                continue
        if value is not None:
            return bool(value)
    if callable(cancel):
        return bool(cancel())
    return False


def _accepts(fn: Callable[..., Any], name: str) -> bool:
    """True only when *fn* DECLARES *name* as a keyword-passable parameter.

    A ``**kwargs`` catch-all deliberately does NOT count. Several CAD tools —
    ``add_features`` above all — end in ``**extra`` and treat every unrecognised
    keyword as USER INTENT: a feature parameter, a design-intent note, or a hard
    error naming the real parameter set. Letting a catch-all stand in for a
    declaration meant the harness posted ``actor``, ``run_id``, ``node_id``,
    ``parent_id``, ``subagent_id``, ``progress`` and ``cancel`` into that
    channel, so THE core modeling tool rejected every call it was given. Live
    2026-09-03, in the model's own words: "add_features consistently rejects the
    harness-injected kwargs like actor, cancel, progress, and run_id ...
    add_features is unusable through this device and I need to find an
    alternative route to apply document operations" — after which it spent
    thirteen minutes writing STEP files by hand through `eval`. A tool that
    wants run context asks for it by name; resource selectors mark the same
    names with ``model_selectors`` so omission remains meaningful.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        _debug_fallback(f"signature probe for {name}", exc)
        return False
    parameter = sig.parameters.get(name)
    if parameter is None:
        return False
    return parameter.kind in (
        parameter.POSITIONAL_OR_KEYWORD,
        parameter.KEYWORD_ONLY,
    )


def _document_args(
    fn: Callable[..., Any] | None, args: dict[str, Any], context: Any,
    *, agent_id: str | None = None,
) -> dict[str, Any]:
    out = dict(args)
    if not callable(fn):
        return out
    selectors = getattr(fn, "_cad_model_selectors", ())
    # A tool that takes its documents from another argument (slice_part's parts=[...])
    # declares it; the run's doc_id would read as a second, conflicting target.
    replaced = any(
        out.get(key) is not None for key in getattr(fn, "_cad_context_doc_unless", ())
    )
    for name in ("doc_id", "actor", "run_id", "node_id", "parent_id", "subagent_id"):
        value = _ctx(context, name, None)
        if name == "doc_id" and agent_id is not None:
            from services.mcp.cad_common import current_doc_id

            value = current_doc_id() or value
        if name == "doc_id" and replaced:
            continue
        if (
            value is not None and name not in out and name not in selectors
            and _accepts(fn, name)
        ):
            out[name] = value
    return out


def _call_run_dispatcher(
    dispatcher: Callable[..., Any],
    name: str,
    args: dict[str, Any],
    context: Any,
    progress: Any,
    cancel: Any,
) -> Any:
    kwargs: dict[str, Any] = {}
    if _accepts(dispatcher, "progress"):
        kwargs["progress"] = progress
    if _accepts(dispatcher, "cancel"):
        kwargs["cancel"] = cancel
    if _accepts(dispatcher, "context"):
        kwargs["context"] = context
    return dispatcher(name, args, **kwargs)


def _enable_tokens(args: Mapping[str, Any]) -> list[str]:
    """Flatten an ``enable_tools`` call's ``categories``/``category``/``tools``
    arguments into one ordered token list (groups, packs, ``family:``, names)."""
    tokens: list[str] = []
    for key in ("categories", "category", "tools", "tool"):
        raw = args.get(key)
        if raw is None:
            continue
        items = [raw] if isinstance(raw, str) else list(raw or ())
        tokens.extend(str(item) for item in items if str(item or "").strip())
    return _ordered_unique(tokens)


def _dispatch_meta(binding: HostToolBinding, args: dict[str, Any]) -> Any:
    if binding.name == "list_tool_categories":
        from services.agent.agent_turn import _tool_category_catalog_result

        return _tool_category_catalog_result()
    if binding.name != "enable_tools":
        return _MISSING
    tokens = _enable_tokens(args)
    callback = _ctx(binding.context, "enable_categories", None)
    if callable(callback):
        result = callback(tokens)
        if result is not None:
            return result
    from services.agent.tool_categories import resolve_tool_requests

    added, unknown = resolve_tool_requests(tokens)
    return {
        "ok": True,
        "enabled": tokens,
        "added": sorted(added),
        "unknown": unknown,
    }


def _mount_skill_tools(binding: HostToolBinding, args: Mapping[str, Any], result: Any) -> Any:
    """After ``cad_skill(name)`` loads a pack, mount the tools that pack teaches.

    A skill body names its tools (frontmatter ``triggers.tools``) and the model calls
    them next; when they were not loaded yet every such call was Chudcode's
    "No such tool: xd://piping_solve" (six in one cooling-water run, 2026-10-08).
    Mounting goes through the same ``enable_categories`` path as ``enable_tools``, so
    the mode policy still narrows it. Returns *result* with a one-line note appended
    when something new was mounted.
    """
    callback = _ctx(binding.context, "enable_categories", None)
    name = args.get("name")
    if not callable(callback) or not isinstance(name, str) or args.get("path"):
        return result
    if not isinstance(result, str) or not result.startswith("[SKILL:"):
        return result
    try:
        from services.agent.skills import get_skill

        skill = get_skill(name, project_root=_ctx(binding.context, "workspace_dir", None))
    except Exception as exc:  # noqa: BLE001 - a missing pack leaves the body as is
        _debug_fallback(f"skill tools for {name}", exc)
        return result
    tools = list(getattr(getattr(skill, "triggers", None), "tools", ()) or ())
    if not tools:
        return result
    enabled = callback(tools)
    added = sorted((enabled or {}).get("added") or ()) if isinstance(enabled, Mapping) else []
    if not added:
        return result
    return f"{result}\n\n[This skill's tools are now loaded: {', '.join(added)}]"


def _guardrail_block(binding: HostToolBinding, args: dict[str, Any]) -> str | None:
    context = binding.context
    ledger = _ctx(context, "ledger", None)
    actor = str(_ctx(context, "actor", None) or "chudcode_host")
    calls = _ctx(context, "tool_calls", _ctx(context, "calls", None))
    if calls is None:
        calls = binding.metadata.get("tool_calls", [])
    # Parallel sub-agent calls append to the same run records; guards read a snapshot.
    with _RECORDS_LOCK:
        calls = list(calls)

    custom = _ctx(context, "pre_dispatch_guardrail", None)
    if callable(custom):
        block = custom(binding.name, args, ledger, actor)
        if block is not None:
            return str(block)

    capability = _ctx(context, "capability", None)
    if capability is not None:
        try:
            from services.agent import scaffolds

            block = scaffolds.pre_dispatch_scaffolds(
                binding.name,
                args,
                profile=capability,
                ledger=ledger,
                calls=calls,
                actor=actor,
                effective_allow_set=None,
            )
            if block is not None:
                return str(block)
        except Exception as exc:  # noqa: BLE001 - scaffold subsystem is optional
            _debug_fallback(f"scaffold guard for {binding.name}", exc)

    from services.agent.agent_turn import (
        _drawing_export_review_block,
        _pre_dispatch_guardrail,
    )

    block = _pre_dispatch_guardrail(binding.name, args, ledger, actor)
    if block is None and binding.name == "export_drawing":
        block = _drawing_export_review_block(calls, args)
    return str(block) if block is not None else None


def _mode_refusal(name: str, context: Any) -> dict[str, Any] | None:
    """Refuse a mutator under the user's read-only mode at dispatch time.

    ``_effective_names`` already hides mutators from an Ask/Plan turn's
    advertised set; this re-applies the same ``apply_mode_tools`` policy to the
    call itself, so a stale binding or a sub-agent inheriting the parent's
    bindings can never mutate under a mode the user chose to be read-only.
    A policy import or evaluation error propagates: the call is never run
    unchecked.
    """
    from services.agent.config import bugreport_floor
    from services.agent.copilot_mode_tools import (
        META_TOOL_FLOOR,
        apply_mode_tools,
        normalize_mode,
    )

    mode = _ctx(context, "mode", None)
    plan_only = _ctx(context, "plan_only", None)
    preferences = _ctx(context, "turn_preferences", None)
    effective = "plan" if plan_only else normalize_mode(mode)
    if getattr(preferences, "mode", None) == "ask":
        effective = "ask"
    if effective not in {"ask", "plan"}:
        return None
    allowed = apply_mode_tools(
        mode,
        [name],
        plan_only=plan_only,
        subagent=bool(
            _ctx(context, "subagent_id", None) or _ctx(context, "subagent", False)
        ),
        preferences=preferences,
    )
    if allowed is None or name in allowed or name in META_TOOL_FLOOR:
        return None
    if name in bugreport_floor():
        return None
    return {
        "ok": False,
        "status": "mode_refused",
        "policy_refusal": True,
        "executed": False,
        "error": (
            f"{name!r} is not available in {effective.title()} mode, which is "
            "read-only. Describe the change instead, or ask the user to switch to "
            "Agent mode."
        ),
    }


def _binding_context_refusal(binding: HostToolBinding) -> dict[str, Any] | None:
    """A captured binding may restore a cell owner, never replace an active one."""
    active = tenancy.current_context()
    trusted = binding.trusted_context
    if tenancy.in_cell() and (
        not trusted.get("cell") or (active.get("cell") and active != trusted)
    ):
        return {"ok": False, "reason": "not_found", "code": "not_found"}
    if trusted.get("cell") and not binding.project_root:
        return {
            "ok": False, "reason": "workspace_root_unavailable",
            "error": "The owning turn has no project root; ambient roots are not available.",
        }
    return None


def dispatch_host_tool(
    binding: HostToolBinding,
    args: Mapping[str, Any] | None,
    progress: Any,
    cancel: Any,
    *,
    agent_id: str | None = None,
    parent_agent_id: str | None = None,
) -> Any:
    """Rebind the trusted owner and explicit roots on SDK host-call threads."""
    if not isinstance(binding, HostToolBinding):
        raise TypeError("binding must be a HostToolBinding")
    refusal = _binding_context_refusal(binding)
    if refusal is not None:
        return refusal
    with tenancy.bind_context(binding.trusted_context), tenancy.bind_roots(
        binding.project_root, binding.save_root,
    ):
        return _dispatch_host_tool(
            binding, args, progress, cancel,
            agent_id=agent_id, parent_agent_id=parent_agent_id,
        )


def _dispatch_host_tool(
    binding: HostToolBinding,
    args: Mapping[str, Any] | None,
    progress: Any,
    cancel: Any,
    *,
    agent_id: str | None = None,
    parent_agent_id: str | None = None,
) -> Any:
    """Dispatch one host call through CAD's run-scoped dispatch chokepoint.

    Policy refusals are returned in their existing machine-readable shape.  Tool
    exceptions — and policy/mode evaluation errors, which fail closed before the
    tool body runs — deliberately propagate so Chudcode records a tool error
    rather than a fabricated successful result.

    ``agent_id``/``parent_agent_id`` name the Chudcode task sub-agent making the
    call (None for the top-level agent). Sub-agents run concurrently on their own
    RPC host-call threads against the same run context: per-tool call caps are
    reserved atomically in ``_dispatch_tool`` and run records are appended under
    ``_RECORDS_LOCK``; the CAD tool body itself runs unlocked.
    """
    if not isinstance(binding, HostToolBinding):
        raise TypeError("binding must be a HostToolBinding")
    if args is None:
        supplied: dict[str, Any] = {}
    elif isinstance(args, Mapping):
        supplied = dict(args)
    else:
        raise TypeError("tool arguments must be a JSON object")
    if "helper_runtime" in supplied:
        return {"ok": False, "error": "helper_runtime is host-owned", "executed": False}
    # Validate input without reordering its keys.
    json.dumps(_json_safe(supplied), ensure_ascii=False, allow_nan=False)
    if _cancelled(cancel):
        return {
            "ok": False,
            "status": "cancelled",
            "cancelled": True,
            "executed": False,
        }

    from services.mcp.cad_common import agent_doc_context
    from services.mcp.daemon_bridge import turn_connection_scope

    owner = agent_id or _ctx(binding.context, "agent_id", None)
    scope = _ctx(binding.context, "run_id", None) or _ctx(binding.context, "session_id", None)
    with agent_doc_context(owner, scope=scope), turn_connection_scope(
        _ctx(binding.context, "daemon_connections", None)
    ):
        refusal = _policy_refusal(binding.name, binding.context) or _mode_refusal(
            binding.name, binding.context
        )
        if refusal is not None:
            return refusal

        meta = _dispatch_meta(binding, supplied)
        if meta is not _MISSING:
            return _json_safe(meta)

        mission = _ctx(binding.context, "mission_dispatch", None)
        mission_resolve = getattr(mission, "resolve", None)
        mission_fn = mission_resolve(binding.name) if callable(mission_resolve) else None
        dispatcher = _ctx(
            binding.context, "tool_dispatch", _ctx(binding.context, "dispatcher", None)
        )
        target = mission_fn if callable(mission_fn) else binding.handler
        call_args = _document_args(target, supplied, binding.context, agent_id=agent_id)

        blocked = _guardrail_block(binding, call_args)
        if blocked is not None:
            return {
                "ok": False,
                "status": "guardrail_blocked",
                "policy_refusal": True,
                "executed": False,
                "error": blocked,
            }
        # Retain the original owning pool even after terminal cleanup removes it
        # from the mutable run context. Active leases drain when the worker ends.
        connections = _ctx(binding.context, "daemon_connections", None)

        def inner(name: str, merged: dict[str, Any]) -> Any:
            # Tool timeouts can move the body to another thread. Socket leases
            # are thread-local, so install the owning turn's pool there too.
            with turn_connection_scope(connections):
                return invoke(name, merged)

        def invoke(name: str, merged: dict[str, Any]) -> Any:
            actual = _document_args(target, merged, binding.context, agent_id=agent_id)
            if not callable(mission_fn) and callable(dispatcher):
                return _call_run_dispatcher(
                    dispatcher, name, actual, binding.context, progress, cancel
                )
            if not callable(target):
                raise TypeError(f"tool {name!r} is no longer bound in this run")
            # The offline resolver path reconciles keyword arguments against the
            # tool's real signature (synonyms, dropped labels, teaching errors); this
            # path used to call straight through, so a model's `plm_save(message=…)`
            # surfaced as "register_plm_tools.<locals>.plm_save() got an unexpected
            # keyword argument 'message'" — a raised error that also charged the cap.
            model_args = {k: v for k, v in actual.items() if k not in _HOST_INJECTED_PARAMS}
            kwargs, notes, arg_error = _reconcile_kwargs(name, target, model_args)
            if arg_error is not None:
                return arg_error
            # Host callables, never a model-supplied value (`"cancel": true` would be
            # called as a function).
            if _accepts(target, "progress"):
                kwargs["progress"] = progress
            if _accepts(target, "cancel"):
                kwargs["cancel"] = cancel
            asker = _ctx(binding.context, "ask_human", None)
            if callable(asker) and _accepts(target, "ask_human"):

                def ask_human(batch: Mapping[str, Any]) -> Any:
                    return asker(batch, agent_id=agent_id, cancel=cancel)

                kwargs["ask_human"] = ask_human
            # A pure wait (wait_job) is the one tool a queued user message may cut short:
            # the steer only reaches the model at the next tool boundary, so a 15-minute
            # wait otherwise holds the user's message for 15 minutes. Real work is never
            # woken — `cancel` stays the Stop probe.
            steer_pending = _ctx(binding.context, "user_steer_pending", None)
            if callable(steer_pending) and _accepts(target, "wake"):
                kwargs["wake"] = lambda: "user_message" if steer_pending() else None
            result = target(**kwargs)
            return _attach_arg_notes(result, notes) if notes else result

        from services.agent.agent_turn import (
            _attach_arg_notes,
            _dispatch_tool,
            _reconcile_kwargs,
            _ToolCallRecovery,
        )

        call_counts = _ctx(binding.context, "tool_call_counts", None)
        if call_counts is None and isinstance(binding.context, dict):
            call_counts = binding.context.setdefault("tool_call_counts", {})
        if call_counts is None:
            call_counts = binding.metadata.get("tool_call_counts")
        recovery = _ctx(
            binding.context, "tool_call_recovery",
            binding.metadata.get("tool_call_recovery"),
        )
        if recovery is None and isinstance(binding.context, dict):
            recovery = binding.context.setdefault("tool_call_recovery", _ToolCallRecovery())
        if recovery is not None and isinstance(binding.metadata, dict):
            binding.metadata.setdefault("tool_call_recovery", recovery)
        from services.mcp.cad_common import current_doc_id

        recovery_scope = {
            name: _ctx(binding.context, name, None)
            for name in ("run_id", "session_id", "actor", "node_id", "subagent_id")
        }
        # Only document-scoped signatures depend on the ambient current document.
        # Creating/opening tools can change it before returning (including saving),
        # and their exact retries must still identify the original invocation.
        document_target = call_args.get("doc_id")
        if (
            not document_target and callable(target) and _accepts(target, "doc_id")
            and not any(
                call_args.get(name) is not None
                for name in getattr(target, "_cad_context_doc_unless", ())
            )
        ):
            document_target = current_doc_id() or _ctx(binding.context, "doc_id", None)
        recovery_scope.update(
            agent_id=owner, parent_agent_id=parent_agent_id, doc_id=document_target,
        )
        from services.agent.online_turn import bind_helper_runtime

        with bind_helper_runtime(
            _ctx(binding.context, "helper_runtime", None),
            session_id=_ctx(binding.context, "session_id", None),
        ):
            result = _dispatch_tool(
                binding.name,
                call_args,
                _call_counts=call_counts,
                _inner=inner,
                _loop_guard=_ctx(binding.context, "tool_loop_guard", None),
                _recovery=recovery,
                _recovery_scope=recovery_scope,
                _cancel=lambda: _cancelled(cancel),
            )
        if binding.name == "cad_skill":
            result = _mount_skill_tools(binding, supplied, result)
        safe_result = _json_safe(result)
        records = _ctx(binding.context, "tool_calls", _ctx(binding.context, "calls", None))
        if records is None:
            records = binding.metadata.get("tool_calls")
        if isinstance(records, list):
            structured = safe_result
            if isinstance(safe_result, str):
                parsed = _json_text_value(safe_result)
                structured = parsed[0] if parsed is not None else None
            ok = not (isinstance(structured, Mapping) and structured.get("ok") is False)
            record = {
                "tool": binding.name,
                "args": _json_safe(call_args),
                "ok": ok,
                "result": safe_result,
            }
            if agent_id:
                record["agent_id"] = agent_id
                if parent_agent_id:
                    record["parent_agent_id"] = parent_agent_id
            with _RECORDS_LOCK:
                records.append(record)
                del records[:-64]
        return safe_result


# --------------------------------------------------------------------------- JSON boundary


def _json_safe(value: Any, _seen: set[int] | None = None) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    seen = _seen if _seen is not None else set()
    if isinstance(value, Mapping):
        ident = id(value)
        if ident in seen:
            return "<recursive>"
        seen.add(ident)
        try:
            return {str(key): _json_safe(item, seen) for key, item in value.items()}
        finally:
            seen.remove(ident)
    if isinstance(value, (list, tuple, set, frozenset)):
        ident = id(value)
        if ident in seen:
            return ["<recursive>"]
        seen.add(ident)
        try:
            return [_json_safe(item, seen) for item in value]
        finally:
            seen.remove(ident)
    # A render tool's Image object becomes the path reference host_tool_result turns
    # into an image block; str() of it was the placeholder the model used to receive.
    image = image_ref(value)
    if image is not None:
        return image
    return str(value)
