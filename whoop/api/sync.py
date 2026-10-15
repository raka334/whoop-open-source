"""Pull official API collections into the unified store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..config import Config
from ..store import Store
from .client import WhoopClient

PROVIDER = "whoop"


@dataclass(frozen=True)
class Resource:
    name: str
    path: str
    upsert: Callable[[Store, list[dict]], int]


def _resources() -> tuple[Resource, ...]:
    return (
        Resource("cycles", "/cycle", lambda s, r: s.upsert_cycles(r)),
        Resource("recoveries", "/recovery", lambda s, r: s.upsert_recoveries(r)),
        Resource("sleeps", "/activity/sleep", lambda s, r: s.upsert_sleeps(r)),
        Resource("workouts", "/activity/workout", lambda s, r: s.upsert_workouts(r)),
    )


def sync(
    config: Config,
    store: Store,
    *,
    start: str | None = None,
    end: str | None = None,
    resources: tuple[str, ...] | None = None,
) -> dict[str, int]:
    """Fetch and upsert API collections. Returns row counts per resource."""
    config.require_api()
    client = WhoopClient(config, store)
    counts: dict[str, int] = {}

    for res in _resources():
        if resources and res.name not in resources:
            continue
        records = list(client.paginate(res.path, start=start, end=end))
        counts[res.name] = res.upsert(store, records)
        store.set_cursor("api", res.name, end)
    return counts
