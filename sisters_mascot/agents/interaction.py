"""Shared data structures for interactive permission, form, and question prompts."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class InteractionOption:
    """Selectable option for a choice or multiselect field."""

    value: str
    label: str
    description: str = ""
    kind: str = ""


@dataclass
class InteractionField:
    """Single form or question input field presented to the user."""

    key: str
    title: str = ""
    description: str = ""
    field_type: str = "choice"  # "choice" | "multiselect" | "text" | "number" | "boolean" | "external"
    required: bool = True
    options: list[InteractionOption] = field(default_factory=list)
    allow_custom: bool = False
    default: Any = None
    url: str = ""
    placeholder: str = ""
    is_secret: bool = False


@dataclass
class InteractionAction:
    """Direct button action (used mainly for permission approvals)."""

    id: str
    label: str
    style: str = "secondary"  # "success" | "primary" | "danger" | "secondary"


@dataclass
class InteractionResponse:
    """User decision or filled form answers returned to an interactive CLI runner."""

    action_id: str = ""
    answers: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    cancelled: bool = False


@dataclass
class InteractionRequest:
    """Blocking interaction request emitted by an AI CLI runner and resolved by the UI."""

    agent_id: str
    kind: str  # "permission" | "form" | "question"
    title: str
    description: str = ""
    sister_name: str = ""
    tool_name: str = ""
    detail: str = ""
    actions: list[InteractionAction] = field(default_factory=list)
    fields: list[InteractionField] = field(default_factory=list)
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    _done_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _response: InteractionResponse | None = field(default=None, init=False, repr=False)

    def resolve(self, response: InteractionResponse) -> bool:
        """Store `response` and wake any waiting worker thread (idempotent)."""
        with self._lock:
            if self._response is not None:
                return False
            self._response = response
            self._done_event.set()
            return True

    def cancel(self, message: str = "キャンセルされました") -> bool:
        """Resolve this request as cancelled."""
        return self.resolve(InteractionResponse(cancelled=True, message=message))

    def is_resolved(self) -> bool:
        """Return True if a response or cancellation has already been recorded."""
        with self._lock:
            return self._response is not None

    def wait(
        self,
        *,
        cancel_event: threading.Event | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        poll_interval: float = 0.08,
    ) -> InteractionResponse:
        """Block until resolved by the UI or interrupted by cancellation."""
        while not self._done_event.wait(timeout=poll_interval):
            if cancel_event is not None and cancel_event.is_set():
                self.cancel("ユーザーにより中断されました")
                break
            if is_cancelled is not None and is_cancelled():
                self.cancel("ユーザーにより中断されました")
                break
        with self._lock:
            if self._response is not None:
                return self._response
        return InteractionResponse(cancelled=True, message="中断されました")


InteractionCallback = Callable[[InteractionRequest], InteractionResponse]
