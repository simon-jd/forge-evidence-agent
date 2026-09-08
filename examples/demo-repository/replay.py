"""Stable pages for reconnecting event consumers.

The public cursor contains both the event timestamp and its unique sequence.
Events may arrive out of order and several events may share one millisecond.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    timestamp_ms: int
    sequence: int
    payload: str


@dataclass(frozen=True)
class Cursor:
    timestamp_ms: int
    sequence: int


def cursor_for(event: Event) -> Cursor:
    return Cursor(event.timestamp_ms, event.sequence)


def replay_page(events: list[Event], after: Cursor | None, limit: int = 50) -> list[Event]:
    """Return a stable page strictly after the complete cursor, without duplicates."""
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    ordered = sorted(events, key=lambda event: (event.timestamp_ms, event.sequence))
    if after is not None:
        ordered = [event for event in ordered if event.timestamp_ms > after.timestamp_ms]
    return ordered[:limit]
