"""ask_user — the one interaction core (Pillar 9, contract: contracts/interaction.md).

Typed questions with suggested defaults, batched (<=4), a machine-readable answer
schema, an answer ledger that replays on resume (a resumed agent never re-asks), and a
set of interchangeable transports (in-app dialog, MCP elicitation, CLI). Normalization,
provenance, persistence, replay and defaulting live HERE; a transport only turns a batch
into raw answers and nothing else.

This module is transport-agnostic and stdlib-only; the MCP transport lazy-imports nothing
at module load. It is the reference implementation of the frozen contract.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

SCHEMA_BATCH = "aicad.interaction/1"
SCHEMA_ANSWERS = "aicad.interaction.answers/1"
MAX_QUESTIONS = 4  # the proven `clarify` cap

KINDS = {"choice", "dimension", "text", "file_request", "approval"}


class InteractionError(ValueError):
    """A contract violation (bad batch, unanswerable required question, bad unit)."""


# --------------------------------------------------------------------------- #
# Unit normalization (contract §6). No prior normalizer existed (intake hard-  #
# coded mm); canonical units mm/deg/count are the repo-wide invariant.        #
# --------------------------------------------------------------------------- #

_LENGTH_TO_MM = {
    "mm": 1.0, "cm": 10.0, "m": 1000.0, "um": 0.001, "µm": 0.001,
    "in": 25.4, "inch": 25.4, "inches": 25.4, '"': 25.4,
    "mil": 0.0254, "thou": 0.0254, "ft": 304.8, "'": 304.8,
}
_ANGLE_TO_DEG = {"deg": 1.0, "°": 1.0, "degree": 1.0, "degrees": 1.0,
                 "rad": 180.0 / math.pi, "radian": 180.0 / math.pi, "radians": 180.0 / math.pi}
_CANONICAL = {"length": "mm", "angle": "deg", "count": "count"}


def normalize_dimension(value: float, unit: str, family: str = "length") -> dict:
    """Return ``{value: <canonical>, unit: <canonical>}`` for a typed dimension.

    Raises :class:`InteractionError` for an unknown family or a unit that does not
    belong to the family — a mismatch is never silently coerced (contract §6).
    """
    if family not in _CANONICAL:
        raise InteractionError(f"unknown dimension family: {family!r}")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise InteractionError(f"dimension value not numeric: {value!r}")
    u = (unit or "").strip()
    if family == "count":
        if u not in ("", "count"):
            raise InteractionError(f"count takes no unit, got {unit!r}")
        return {"value": v, "unit": "count"}
    table = _LENGTH_TO_MM if family == "length" else _ANGLE_TO_DEG
    key = u.lower() if u not in ('"', "'", "°", "µm") else u
    if key not in table:
        raise InteractionError(f"unit {unit!r} is not a {family} unit")
    return {"value": v * table[key], "unit": _CANONICAL[family]}


# --------------------------------------------------------------------------- #
# Question / batch model (contract §1–§3)                                      #
# --------------------------------------------------------------------------- #

@dataclass
class Question:
    id: str
    kind: str
    prompt: str
    suggested: Any                       # typed default; same shape as the answer (§2)
    required: bool = False
    why: str | None = None
    # kind-specific
    family: str = "length"               # dimension
    options: list[dict] | None = None    # choice: [{value, label?}]
    multi: bool = False                  # choice
    multiline: bool = False              # text
    media: str = "file"                  # file_request: file|image
    accept: list[str] | None = None      # file_request
    proposal: str | None = None          # approval
    evidence: dict | None = None         # proactive verify→ask (§9)

    def to_dict(self) -> dict:
        d = {"id": self.id, "kind": self.kind, "prompt": self.prompt,
             "suggested": self.suggested, "required": self.required}
        if self.why:
            d["why"] = self.why
        if self.kind == "dimension":
            d["family"] = self.family
        if self.kind == "choice":
            d["options"] = self.options or []
            d["multi"] = self.multi
        if self.kind == "text":
            d["multiline"] = self.multiline
        if self.kind == "file_request":
            d["media"] = self.media
            if self.accept:
                d["accept"] = self.accept
            d["multiple"] = self.multi
        if self.kind == "approval":
            d["proposal"] = self.proposal or self.prompt
        if self.evidence:
            d["evidence"] = self.evidence
        return d

    @staticmethod
    def from_dict(d: dict) -> "Question":
        # legacy alias: `ask` -> `prompt`
        prompt = d.get("prompt") or d.get("ask") or ""
        return Question(
            id=d["id"], kind=d["kind"], prompt=prompt, suggested=d.get("suggested"),
            required=bool(d.get("required", False)), why=d.get("why"),
            family=d.get("family", "length"), options=d.get("options"),
            multi=bool(d.get("multi", d.get("multiple", False))),
            multiline=bool(d.get("multiline", False)), media=d.get("media", "file"),
            accept=d.get("accept"), proposal=d.get("proposal"), evidence=d.get("evidence"))


@dataclass
class QuestionBatch:
    questions: list[Question]
    scope: str = "run"                   # run | doc
    blocking: bool = True
    run_id: str | None = None
    doc_id: str | None = None
    revision: int | None = None
    batch_id: str = field(default_factory=lambda: "b_" + uuid.uuid4().hex[:12])

    def to_dict(self) -> dict:
        d = {"schema": SCHEMA_BATCH, "batch_id": self.batch_id, "scope": self.scope,
             "blocking": self.blocking, "questions": [q.to_dict() for q in self.questions]}
        if self.scope == "run":
            d["run_id"] = self.run_id
        else:
            d["doc_id"] = self.doc_id
            d["revision"] = self.revision
        return d


def _validate(batch: QuestionBatch) -> None:
    if not batch.questions:
        raise InteractionError("batch has no questions")
    if len(batch.questions) > MAX_QUESTIONS:
        raise InteractionError(f"batch has {len(batch.questions)} questions; cap is {MAX_QUESTIONS}")
    seen = set()
    for q in batch.questions:
        if q.kind not in KINDS:
            raise InteractionError(f"unknown question kind: {q.kind!r}")
        if not q.id or q.id in seen:
            raise InteractionError(f"question id missing or duplicated: {q.id!r}")
        seen.add(q.id)
        if q.suggested is None:
            raise InteractionError(f"question {q.id!r} has no suggested default (contract §2)")
        if q.kind == "choice" and not q.options:
            raise InteractionError(f"choice {q.id!r} has no options")
    if batch.scope == "run" and not batch.run_id:
        raise InteractionError("run-scoped batch needs run_id")
    if batch.scope == "doc" and not batch.doc_id:
        raise InteractionError("doc-scoped batch needs doc_id")


# --------------------------------------------------------------------------- #
# Ledger (contract §5) — persist-then-return; replay on resume                #
# --------------------------------------------------------------------------- #

def cache_root() -> Path:
    """Resolve ``<appdata>/AI-CAD/cache`` the way the C++ run machine does
    (``core/agent/paths.cpp``): APPDATA on Windows, ``$XDG_DATA_HOME`` else
    ``~/.local/share`` on Linux/macOS. ``AICAD_CACHE_ROOT`` overrides (tests)."""
    override = os.environ.get("AICAD_CACHE_ROOT")
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "AI-CAD" / "cache"


def run_dir(run_id: str) -> Path:
    d = cache_root() / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


class Ledger(Protocol):
    def answered(self, qid: str) -> dict | None: ...
    def record_question(self, q: dict) -> None: ...
    def record_answer(self, qid: str, answer: dict) -> None: ...


class MemoryLedger:
    """In-memory ledger (tests, and the substrate a doc-scoped store wraps)."""

    def __init__(self) -> None:
        self.answers: dict[str, dict] = {}
        self.questions: list[dict] = []

    def answered(self, qid: str) -> dict | None:
        return self.answers.get(qid)

    def record_question(self, q: dict) -> None:
        self.questions.append(q)

    def record_answer(self, qid: str, answer: dict) -> None:
        self.answers[qid] = answer


class FileLedger:
    """Run-scoped JSONL ledger at ``<cache>/runs/<run_id>/interaction.jsonl``.

    One record per line: ``{"t": "question"|"answer", ...}``. Answers replay by ``id``.
    """

    def __init__(self, run_id: str, path: Path | None = None) -> None:
        self.run_id = run_id
        self.path = path or (run_dir(run_id) / "interaction.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._answers: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("t") == "answer":
                self._answers[rec["id"]] = rec["answer"]

    def _append(self, rec: dict) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def answered(self, qid: str) -> dict | None:
        return self._answers.get(qid)

    def record_question(self, q: dict) -> None:
        self._append({"t": "question", **q})

    def record_answer(self, qid: str, answer: dict) -> None:
        self._answers[qid] = answer
        self._append({"t": "answer", "id": qid, "answer": answer})


# --------------------------------------------------------------------------- #
# Transports (contract §8) — dumb: batch in, raw answers out                   #
# --------------------------------------------------------------------------- #

class Transport(Protocol):
    def ask(self, batch: QuestionBatch) -> dict[str, Any]:
        """Return ``{question_id: raw_answer}`` (answer shapes per contract §1)."""


class CLITransport:
    """Headless stdin/stdout transport with a re-ask loop for bad input."""

    def __init__(self, in_stream=None, out_stream=None) -> None:
        self._in = in_stream or sys.stdin
        self._out = out_stream or sys.stdout

    def _w(self, s: str) -> None:
        self._out.write(s)
        self._out.flush()

    def ask(self, batch: QuestionBatch) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for q in batch.questions:
            out[q.id] = self._ask_one(q)
        return out

    def _ask_one(self, q: Question) -> Any:
        hint = f" [{json.dumps(q.suggested)}]"
        while True:
            self._w(f"\n{q.prompt}{hint}\n")
            if q.kind == "choice":
                for i, opt in enumerate(q.options or []):
                    self._w(f"  {i + 1}. {opt.get('label', opt['value'])}\n")
            raw = self._in.readline()
            if raw == "":  # EOF
                return None
            raw = raw.strip()
            if not raw:  # empty ⇒ accept default (core turns it into source=default)
                return None
            try:
                return self._parse(q, raw)
            except (InteractionError, ValueError) as e:
                self._w(f"  ! {e}\n")

    @staticmethod
    def _parse(q: Question, raw: str) -> Any:
        if q.kind == "dimension":
            parts = raw.split()
            val = float(parts[0])
            unit = parts[1] if len(parts) > 1 else (q.suggested or {}).get("unit", "mm")
            normalize_dimension(val, unit, q.family)  # validate now
            return {"value": val, "unit": unit}
        if q.kind == "choice":
            opts = q.options or []
            if raw.isdigit() and 1 <= int(raw) <= len(opts):
                return opts[int(raw) - 1]["value"]
            values = [o["value"] for o in opts]
            if raw in values:
                return raw
            raise InteractionError(f"pick 1..{len(opts)} or one of {values}")
        if q.kind == "approval":
            return raw.lower() in ("y", "yes", "true", "1", "approve", "ok")
        if q.kind == "file_request":
            return {"paths": [p.strip() for p in raw.split(",") if p.strip()]}
        return raw  # text


class InAppTransport:
    """In-app dialog transport. The ImGui host feeds answers through a provider
    callback ``provider(batch_dict) -> {id: raw_answer}``; the C++ side builds a
    PropertyManager-style modal from ``batch_dict`` (ui/abstract + ui/imgui) and
    returns the collected answers. Purely a courier — no persistence/normalization."""

    def __init__(self, provider: Callable[[dict], dict[str, Any]]) -> None:
        self._provider = provider

    def ask(self, batch: QuestionBatch) -> dict[str, Any]:
        return self._provider(batch.to_dict()) or {}


class MCPElicitationTransport:
    """MCP elicitation transport (mcp 1.27.2 ``Context.elicit``). External clients
    prompt their own user. ``elicit`` is injectable so the round-trip is testable
    without a live client; by default it calls ``ctx.elicit(message, schema)``."""

    def __init__(self, ctx=None, elicit: Callable[[str, dict], dict] | None = None) -> None:
        self._ctx = ctx
        self._elicit = elicit or self._default_elicit

    def _default_elicit(self, message: str, schema: dict) -> dict:  # pragma: no cover
        # mcp 1.27.2: ctx.elicit returns an object with .action and .data
        res = self._ctx.elicit(message=message, schema=schema)
        action = getattr(res, "action", "accept")
        if action != "accept":
            return {}
        data = getattr(res, "data", None) or {}
        return data if isinstance(data, dict) else dict(data)

    @staticmethod
    def schema_for(q: Question) -> dict:
        """A flat elicitation object schema (primitive properties only, per spec)."""
        if q.kind == "dimension":
            return {"type": "object", "properties": {
                "value": {"type": "number"},
                "unit": {"type": "string"}}, "required": ["value", "unit"]}
        if q.kind == "choice":
            return {"type": "object", "properties": {
                "value": {"type": "string", "enum": [o["value"] for o in (q.options or [])]}},
                "required": ["value"]}
        if q.kind == "approval":
            return {"type": "object", "properties": {"value": {"type": "boolean"}},
                    "required": ["value"]}
        if q.kind == "file_request":
            return {"type": "object", "properties": {"paths": {"type": "string"}}}
        return {"type": "object", "properties": {"value": {"type": "string"}}}

    def ask(self, batch: QuestionBatch) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for q in batch.questions:
            data = self._elicit(q.prompt, self.schema_for(q))
            if not data:
                continue  # declined/cancelled ⇒ core falls back to default
            if q.kind == "dimension":
                out[q.id] = {"value": data.get("value"), "unit": data.get("unit")}
            elif q.kind == "file_request":
                paths = data.get("paths", "")
                out[q.id] = {"paths": [p.strip() for p in str(paths).split(",") if p.strip()]}
            else:
                out[q.id] = data.get("value")
        return out


# --------------------------------------------------------------------------- #
# The core: ask_user (contract §2 answers, §5 replay, §6 normalize, §7 events) #
# --------------------------------------------------------------------------- #

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _finalize_answer(q: Question, raw: Any, source: str) -> dict:
    """Build the durable answer object (contract §4), normalizing dimensions."""
    ans: dict = {"value": raw, "source": source, "at": _now()}
    if q.kind == "dimension":
        val, unit = raw["value"], raw["unit"]
        ans["canonical"] = normalize_dimension(val, unit, q.family)
    return ans


def _emit(on_event, ledger, kind: str, data: dict) -> None:
    """Persist-then-callback (contract §7). The ledger write already happened;
    the event is best-effort and never breaks the run."""
    if on_event is None:
        return
    try:
        on_event(event=kind, data=data)
    except Exception:  # pragma: no cover - a UI sink failure must not break the run
        pass


def make_batch(questions: Iterable, **kw) -> QuestionBatch:
    qs = [q if isinstance(q, Question) else Question.from_dict(q) for q in questions]
    return QuestionBatch(questions=qs, **kw)


def ask_user(questions, *, scope: str = "run", run_id: str | None = None,
             doc_id: str | None = None, revision: int | None = None,
             blocking: bool = True, transport: Transport | None = None,
             ledger: Ledger | None = None, on_event: Callable | None = None,
             autonomy: str = "approve", batch_id: str | None = None) -> dict[str, dict]:
    """Ask the human a typed batch (<=4) and return ``{id: answer}`` (contract §4).

    Replays already-answered questions from the ledger (never re-asks). Under
    ``autonomy='auto'`` or a non-blocking batch, unanswered/optional questions accept
    their ``suggested`` default. Persists every Q and A before returning.
    """
    batch = questions if isinstance(questions, QuestionBatch) else make_batch(
        questions, scope=scope, run_id=run_id, doc_id=doc_id, revision=revision,
        blocking=blocking, **({"batch_id": batch_id} if batch_id else {}))
    _validate(batch)

    if ledger is None:
        ledger = FileLedger(batch.run_id) if batch.scope == "run" else MemoryLedger()

    results: dict[str, dict] = {}
    to_ask: list[Question] = []
    for q in batch.questions:
        prior = ledger.answered(q.id)
        if prior is not None:
            replayed = dict(prior, source="resumed")
            results[q.id] = replayed
            _emit(on_event, ledger, "ask_answer",
                  {"batch_id": batch.batch_id, "id": q.id,
                   "value": replayed.get("value"), "canonical": replayed.get("canonical"),
                   "source": "resumed"})
        else:
            to_ask.append(q)

    if not to_ask:
        return results

    ask_batch = QuestionBatch(
        questions=to_ask, scope=batch.scope, blocking=batch.blocking,
        run_id=batch.run_id, doc_id=batch.doc_id, revision=batch.revision,
        batch_id=batch.batch_id)

    for q in to_ask:
        ledger.record_question(q.to_dict())
        _emit(on_event, ledger, "ask_question",
              {"batch_id": batch.batch_id, "id": q.id, "kind": q.kind,
               "prompt": q.prompt, "suggested": q.suggested,
               "scope": batch.scope, "blocking": batch.blocking})

    use_transport = batch.blocking and autonomy != "auto"
    raw: dict[str, Any] = {}
    if use_transport:
        if transport is None:
            raise InteractionError("blocking batch needs a transport")
        raw = transport.ask(ask_batch) or {}

    for q in to_ask:
        provided = raw.get(q.id)
        if provided is not None:
            answer = _finalize_answer(q, provided, "user")
        elif not q.required or autonomy == "auto" or not batch.blocking:
            answer = _finalize_answer(q, q.suggested, "default")
        else:
            raise InteractionError(
                f"required question {q.id!r} was not answered by the transport")
        ledger.record_answer(q.id, answer)
        results[q.id] = answer
        _emit(on_event, ledger, "ask_answer",
              {"batch_id": batch.batch_id, "id": q.id, "value": answer.get("value"),
               "canonical": answer.get("canonical"), "source": answer["source"]})

    return results
