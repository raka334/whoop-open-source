"""Concurrent dual-source sync.

Pull the official API **and** mine the strap over BLE **at the same time**, into
the same local store, then report coverage on both sides and reconcile where they
overlap.

Why concurrent is safe here: the two sources are independent (different
transports, different rate) and the :class:`~whoop.store.Store` serializes writes
with one re-entrant lock over a single SQLite connection. So neither source
blocks the other, and a failure in one never corrupts or rolls back the other.

Neither source *replaces* the other:

* the **API** gives WHOOP's computed daily metrics (recovery, sleep, strain);
* the **BLE** path gives raw, high-rate data (HR/R-R, motion, raw frames).

Their overlap is a cross-check, which is what :meth:`Store.reconcile` reports.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .api import sync as api_sync
from .config import Config
from .store import Store


@dataclass
class JobResult:
    ok: bool
    data: Any = None
    error: str | None = None


async def _run_sync(fn: Callable[[], Any]) -> JobResult:
    """Run a blocking callable off the event loop, capturing every failure."""
    def guarded() -> JobResult:
        try:
            return JobResult(ok=True, data=fn())
        except BaseException as exc:  # noqa: BLE001 - never let one source kill the run
            return JobResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    return await asyncio.to_thread(guarded)


async def _run_async(fn: Callable[[], Awaitable[Any]]) -> JobResult:
    try:
        return JobResult(ok=True, data=await fn())
    except BaseException as exc:  # noqa: BLE001
        return JobResult(ok=False, error=f"{type(exc).__name__}: {exc}")


@dataclass
class SyncReport:
    api: dict[str, int] | None = None
    ble: dict[str, Any] | None = None
    errors: dict[str, str] = field(default_factory=dict)
    coverage: dict[str, dict[str, Any]] = field(default_factory=dict)
    reconciliation: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


async def sync_all(
    config: Config,
    store: Store,
    *,
    start: str | None = None,
    end: str | None = None,
    resources: tuple[str, ...] | None = None,
    api: bool = True,
    ble: bool = True,
    address: str | None = None,
    family: str | None = None,
    seconds: float = 60.0,
    imu: bool = False,
    api_fn: Callable[[], Any] | None = None,
    ble_fn: Callable[[], Awaitable[Any]] | None = None,
) -> SyncReport:
    """Run the API pull and the BLE mine concurrently, then reconcile.

    ``api_fn`` / ``ble_fn`` exist so the coordinator can be tested without a
    network or a strap; production callers omit them.
    """
    jobs: dict[str, asyncio.Future] = {}

    if api:
        call = api_fn or (
            lambda: api_sync.sync(config, store, start=start, end=end, resources=resources)
        )
        jobs["api"] = asyncio.create_task(_run_sync(call))

    if ble:
        if ble_fn is not None:
            jobs["ble"] = asyncio.create_task(_run_async(ble_fn))
        elif not address:
            jobs["ble"] = asyncio.create_task(
                _run_async(_missing_address)
            )
        else:
            jobs["ble"] = asyncio.create_task(
                _run_async(_ble_capture(store, address, config, family, seconds, imu))
            )

    report = SyncReport()
    results = await asyncio.gather(*jobs.values())
    for name, result in zip(jobs, results):
        if not result.ok:
            report.errors[name] = result.error or "unknown error"
        elif name == "api":
            report.api = result.data
        else:
            report.ble = result.data

    # A shared DB is only useful if we can say what each side now covers.
    report.coverage = store.coverage()
    report.reconciliation = store.reconcile(start=start, end=end)
    return report


async def _missing_address() -> Any:
    raise RuntimeError(
        "no BLE address: set WHOOP_BLE_ADDRESS or pass --address (use --no-ble to skip)"
    )


def _ble_capture(
    store: Store,
    address: str,
    config: Config,
    family: str | None,
    seconds: float,
    imu: bool,
):
    from .ble import stream as ble_stream

    return lambda: ble_stream.capture(
        address,
        family=family,
        seconds=seconds,
        store=store,
        device_id=config.device_id or address,
        imu=imu,
    )
