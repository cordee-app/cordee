"""In-memory event bus for SSE push notifications.

Thread-safe pub/sub: each project_id has a set of subscriber queues.
Emitters call emit(project_id, event_dict); SSE generators subscribe() to get a queue.
Unsubscribe on client disconnect to avoid leaks.

Events are tiny dirty signals: {"type": "task_changed", "task_id": 123}
Clients refetch the affected collection on receipt.
"""

import threading
import queue
from collections import defaultdict
from typing import Dict, Set, Any

_subscribers: Dict[int, Set[queue.Queue]] = defaultdict(set)
_lock = threading.Lock()


def subscribe(project_id: int) -> queue.Queue:
    """Subscribe to events for a project. Returns a queue that will receive events."""
    q = queue.Queue(maxsize=100)  # bounded to prevent memory leak on slow consumers
    with _lock:
        _subscribers[project_id].add(q)
    return q


def unsubscribe(project_id: int, q: queue.Queue) -> None:
    """Unsubscribe a queue from a project's events."""
    with _lock:
        _subscribers[project_id].discard(q)
        if not _subscribers[project_id]:
            del _subscribers[project_id]


def emit(project_id: int, event: Dict[str, Any]) -> None:
    """Emit an event to all subscribers of a project.

    Non-blocking: drops events if a subscriber's queue is full (slow client).
    Safe to call from any thread, including background workers.
    """
    with _lock:
        for q in _subscribers.get(project_id, ()):
            try:
                q.put_nowait(event)
            except queue.Full:
                # Slow client - drop event. They'll catch up on next poll.
                pass


def emit_safe(project_id: int, event: Dict[str, Any]) -> None:
    """Wrapper that swallows all exceptions. Use in DB write paths where failures must not break the write."""
    try:
        emit(project_id, event)
    except Exception:
        pass


def emit_global(event: Dict[str, Any]) -> None:
    """Emit an event to ALL subscribers (all projects). Use for global events like work_session_changed."""
    with _lock:
        for project_id, queues in list(_subscribers.items()):
            for q in queues:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass


def emit_global_safe(event: Dict[str, Any]) -> None:
    """Wrapper that swallows all exceptions for global emit."""
    try:
        emit_global(event)
    except Exception:
        pass
