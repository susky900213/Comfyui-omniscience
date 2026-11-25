"""Per-task session lifecycle (DESIGN.md section 7).

The HTTP surface must never hold a request open for the duration of an agent
turn. One :class:`TaskTurnController` serializes turns per task: a message
either starts a turn immediately or is queued as a follow-up. Cancellation
raises the session generation so a turn that is already unwinding cannot
start queued work from before the cancel.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("Comfyui-omniscience")


@dataclass
class TaskSession:
    task_id: str
    generation: int = 0
    turn: "asyncio.Future[None] | None" = None
    queue: list[dict[str, Any]] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.turn is not None and not self.turn.done()


class TaskTurnController:
    """Fire-and-forget turn scheduling on top of a persistent agent."""

    def __init__(self, agent: Any):
        self.agent = agent
        self._sessions: dict[str, TaskSession] = {}

    def _session(self, task_id: str) -> TaskSession:
        session = self._sessions.get(task_id)
        if session is None:
            session = TaskSession(task_id=task_id)
            self._sessions[task_id] = session
        return session

    def validate(self, content: str, skill_ids: Any) -> None:
        """Fail fast on inputs the agent would reject mid-request."""
        if not str(content or "").strip():
            raise ValueError("Message cannot be empty.")
        self.agent._validated_skill_ids(skill_ids if skill_ids is not None else [])

    def submit(
        self,
        task_id: str,
        content: str,
        context: str = "",
        skill_ids: Any = None,
        locale: str = "",
    ) -> dict[str, Any]:
        self.agent.task(task_id)
        session = self._session(task_id)
        payload = {
            "content": str(content),
            "context": str(context or ""),
            "skill_ids": list(skill_ids or []),
            "locale": str(locale or ""),
        }
        queued = session.running
        cursor = self.agent.store.last_event_seq(task_id)
        if queued:
            session.queue.append(payload)
        else:
            self._start(session, payload)
        return {"accepted": True, "queued": queued, "task_id": task_id, "cursor": cursor}

    def _start(self, session: TaskSession, payload: dict[str, Any]) -> None:
        session.turn = asyncio.get_running_loop().create_task(
            self._run(session, session.generation, payload)
        )

    async def _run(self, session: TaskSession, generation: int, payload: dict[str, Any]) -> None:
        try:
            await self.agent.chat(
                session.task_id,
                payload["content"],
                payload.get("context", ""),
                payload.get("skill_ids"),
                locale=payload.get("locale", ""),
            )
        except asyncio.CancelledError:
            logger.info("Turn for task %s was cancelled", session.task_id)
        except Exception as exc:  # Already persisted as task_failed by the runtime.
            logger.warning("Turn for task %s failed: %s", session.task_id, exc)
        finally:
            # A cancel raised the generation: drop pre-cancel queued work and
            # leave the session idle for the next explicit submission.
            if session.generation == generation and (session.turn is asyncio.current_task()):
                if session.queue:
                    self._start(session, session.queue.pop(0))
                else:
                    session.turn = None

    def drain(self, task_id: str) -> list[dict[str, Any]]:
        """Hand queued follow-ups to the running turn at a step boundary."""
        session = self._sessions.get(task_id)
        if session is None or not session.running:
            return []
        items, session.queue = session.queue, []
        return items

    def abort(self, task_id: str) -> dict[str, Any]:
        """Cancel the active turn and discard queued follow-ups (fence)."""
        session = self._session(task_id)
        session.generation += 1
        session.queue.clear()
        turn = session.turn
        session.turn = None
        result = self.agent.cancel_task(task_id)
        if turn is not None and not turn.done():
            turn.cancel()
        return result

    def is_running(self, task_id: str) -> bool:
        session = self._sessions.get(task_id)
        return bool(session and session.running)

    def pending(self, task_id: str) -> int:
        session = self._sessions.get(task_id)
        return len(session.queue) if session else 0
