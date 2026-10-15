"""Concurrent dual-source sync across the two separate databases.

Pull the official API **and** mine the strap over BLE **at the same time** -- but
into their own stores, never into one shared file:

* API records -> :class:`~whoop.store.ApiStore` (``data/whoop_api.sqlite``)
* BLE records -> :class:`~whoop.store.BleStore` (``data/whoop_ble.sqlite``)

Why concurrent is safe here: the two sources are independent (different
transports, different files) and each store serializes its own writes with a
re-entrant lock. Neither source blocks the other, and a failure in one never
touches the other's database.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .api import sync as api_sync
from .config import Config
from .reconcile import Combined
from .store import ApiStore, BleStore


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
    api_store: ApiStore,
    ble_store: BleStore,
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
    """Run the API pull and the BLE mine concurrently, then reconcile read-only."""
    jobs: dict[str, asyncio.Future] = {}

    if api:
        call = api_fn or (
            lambda: api_sync.sync(config, api_store, start=start, end=end, resources=resources)
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
                _run_async(_ble_capture(ble_store, address, config, family, seconds, imu))
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

    combined = Combined(api_store, ble_store)
    report.coverage = combined.coverage()
    report.reconciliation = combined.reconcile(start=start, end=end)
    return report


async def _missing_address() -> Any:
    raise RuntimeError(
        "no BLE address: set WHOOP_BLE_ADDRESS or pass --address (use --no-ble to skip)"
    )


def _ble_capture(
    ble_store: BleStore,
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
        store=ble_store,
        device_id=config.device_id or address,
        imu=imu,
    )
