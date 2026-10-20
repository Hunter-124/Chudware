"""Live ``ask_user`` questions of an interactive online turn.

The desktop chat is the human transport of ``contracts/interaction.md`` §8: a question
batch is announced to the GUI as an ``agent_question`` frame (mapped to the
``agent.question`` feed event), the host tool blocks, and the answer comes back over
the worker control lane (``answer``) or as the user's typed reply (a mid-run steer).
Every resolution, including Stop and turn end, is announced as ``agent_answer`` so the
GUI card never stays pending after nothing can answer it.

There is no timeout: a person may take hours. Stop/cancel and turn teardown are the
only ways a wait ends without the user.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

#: Wait slice between cancellation probes.
_POLL_SECONDS = 0.25
_MAX_REPLY_CHARS = 20_000


class QuestionUnavailable(RuntimeError):
    """The batch could not be shown to the user (the feed is gone)."""


@dataclass
class Resolution:
    """How a batch ended: ``user`` | ``reply`` | ``declined`` | ``cancelled``."""

    source: str
    answers: dict[str, Any] = field(default_factory=dict)
    reply: str | None = None
    steer_id: str = ""


@dataclass
class _Pending:
    batch_id: str
    agent_id: str | None
    done: threading.Event = field(default_factory=threading.Event)
    resolution: Resolution | None = None


class TurnQuestions:
    """Pending question batches of ONE run, keyed by ``batch_id``.

    ``emit`` delivers a raw frame to the turn's feed; ``is_cancelled`` is the run's
    Stop probe. ``ask`` is called on host-tool threads (top-level agent and Chudcode
    task sub-agents alike); ``answer``/``reply``/``close`` come from the control lane
    or the turn's own teardown.
    """

    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None],
        *,
        run_id: str,
        session_id: str,
        is_cancelled: Callable[[], bool],
    ) -> None:
        self._emit = emit
        self._run_id = run_id
        self._session_id = session_id
        self._is_cancelled = is_cancelled
        self._lock = threading.Lock()
        self._pending: OrderedDict[str, _Pending] = OrderedDict()
        self._closed = False

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._pending)

    def ask(
        self,
        batch: Mapping[str, Any],
        *,
        agent_id: str | None = None,
        cancel: Any = None,
    ) -> Resolution:
        """Show *batch* (contract §3 envelope) and block until it resolves."""
        batch_id = str(batch.get("batch_id") or "").strip()
        questions = batch.get("questions")
        if not batch_id or not isinstance(questions, list) or not questions:
            raise ValueError("question batch needs a batch_id and questions")
        pending = _Pending(batch_id=batch_id, agent_id=agent_id)
        with self._lock:
            if self._closed or self._is_cancelled():
                return Resolution(source="cancelled")
            if batch_id in self._pending:
                raise ValueError(f"question batch {batch_id!r} is already pending")
            self._pending[batch_id] = pending
        frame: dict[str, Any] = {
            "type": "agent_question",
            "run_id": self._run_id,
            "session_id": self._session_id,
            "batch_id": batch_id,
            "questions": [dict(q) for q in questions if isinstance(q, Mapping)],
        }
        if agent_id:
            frame["agent_id"] = agent_id
        try:
            self._emit(frame)
        except Exception as exc:  # noqa: BLE001 - a question nobody can see must not block
            with self._lock:
                self._pending.pop(batch_id, None)
            raise QuestionUnavailable(f"the question could not be shown: {exc}") from exc
        try:
            while not pending.done.wait(_POLL_SECONDS):
                if self._is_cancelled() or _cancelled(cancel):
                    self._resolve(batch_id, Resolution(source="cancelled"))
        finally:
            with self._lock:
                self._pending.pop(batch_id, None)
        resolution = pending.resolution or Resolution(source="cancelled")
        self._announce(batch_id, resolution)
        return resolution

    def answer(
        self,
        batch_id: Any,
        answers: Any = None,
        *,
        declined: bool = False,
    ) -> dict[str, Any]:
        """Resolve a pending batch from the GUI form (control action ``answer``)."""
        key = str(batch_id or "").strip()
        if not key:
            return {"ok": False, "reason": "batch_id is required"}
        if declined:
            resolution = Resolution(source="declined")
        else:
            if not isinstance(answers, Mapping):
                return {"ok": False, "batch_id": key, "reason": "answers must be an object"}
            resolution = Resolution(
                source="user", answers={str(k): v for k, v in answers.items()}
            )
        if not self._resolve(key, resolution):
            return {
                "ok": False,
                "batch_id": key,
                "answered": False,
                "reason": "question is no longer pending",
            }
        return {"ok": True, "batch_id": key, "answered": True}

    def reply(self, text: str, *, steer_id: str = "") -> str | None:
        """Resolve the OLDEST pending batch with the user's typed chat reply."""
        with self._lock:
            batch_id = next(iter(self._pending), None)
        if batch_id is None:
            return None
        resolution = Resolution(source="reply", reply=text[:_MAX_REPLY_CHARS], steer_id=steer_id)
        return batch_id if self._resolve(batch_id, resolution) else None

    def close(self) -> None:
        """Stop or turn teardown: nothing will answer any more; wake every waiter."""
        with self._lock:
            self._closed = True
            pending = list(self._pending)
        for batch_id in pending:
            self._resolve(batch_id, Resolution(source="cancelled"))

    def _resolve(self, batch_id: str, resolution: Resolution) -> bool:
        with self._lock:
            pending = self._pending.get(batch_id)
            if pending is None or pending.done.is_set():
                return False
            pending.resolution = resolution
            pending.done.set()
            return True

    def _announce(self, batch_id: str, resolution: Resolution) -> None:
        frame: dict[str, Any] = {
            "type": "agent_answer",
            "run_id": self._run_id,
            "session_id": self._session_id,
            "batch_id": batch_id,
            "source": resolution.source,
        }
        if resolution.answers:
            frame["answers"] = resolution.answers
        if resolution.reply is not None:
            frame["reply"] = resolution.reply
        if resolution.steer_id:
            frame["steer_id"] = resolution.steer_id
        try:
            self._emit(frame)
        except Exception:  # noqa: BLE001 - the tool result still reaches the model
            pass


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
            except TypeError:
                continue
        if value is not None:
            return bool(value)
    if callable(cancel):
        try:
            return bool(cancel())
        except TypeError:
            return False
    return False
