from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from sunset_sentinel_api.services.scheduler import SentinelScheduler, scan_job_id

NOW = datetime(2030, 1, 1, tzinfo=UTC)


class FrozenClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


def test_schedule_configures_real_apscheduler_interval_job() -> None:
    backend = BackgroundScheduler(timezone=UTC)
    scheduler = SentinelScheduler(scheduler=backend, clock=FrozenClock(NOW))

    job = scheduler.schedule_scan(
        target_id="payments",
        interval_minutes=15,
        task=lambda: None,
    )

    assert job.id == scan_job_id("payments")
    assert job.name == "Lifecycle scan: payments"
    assert job.max_instances == 1
    assert job.coalesce is True
    assert job.misfire_grace_time == 900
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval == timedelta(minutes=15)
    assert job.trigger.start_date == NOW + timedelta(minutes=15)
    assert scheduler.job_for("payments") is job


def test_schedule_replace_uses_stable_id_and_single_job() -> None:
    backend = BackgroundScheduler(timezone=UTC)
    scheduler = SentinelScheduler(scheduler=backend, clock=FrozenClock(NOW))

    first = scheduler.schedule_scan(
        target_id="payments",
        interval_minutes=15,
        task=lambda: "first",
    )
    second = scheduler.schedule_scan(
        target_id="payments",
        interval_minutes=30,
        task=lambda: "second",
    )

    assert first.id == second.id == scan_job_id("payments")
    assert len(backend.get_jobs()) == 1
    assert isinstance(second.trigger, IntervalTrigger)
    assert second.trigger.interval == timedelta(minutes=30)
    assert scheduler.run_now("payments") == "second"


def test_start_immediately_uses_injected_clock_without_running_thread() -> None:
    scheduler = SentinelScheduler(
        scheduler=BackgroundScheduler(timezone=UTC),
        clock=FrozenClock(NOW),
    )

    job = scheduler.schedule_scan(
        target_id="payments",
        interval_minutes=60,
        task=lambda: None,
        start_immediately=True,
    )

    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.start_date == NOW


def test_run_now_calls_task_exactly_once_and_returns_value() -> None:
    calls: list[str] = []
    scheduler = SentinelScheduler(
        scheduler=BackgroundScheduler(timezone=UTC),
        clock=FrozenClock(NOW),
    )

    def scan() -> dict[str, int]:
        calls.append("scan")
        return {"records": 3}

    scheduler.schedule_scan(
        target_id="payments",
        interval_minutes=60,
        task=scan,
    )

    assert scheduler.run_now("payments") == {"records": 3}
    assert calls == ["scan"]


def test_run_now_propagates_task_failure() -> None:
    scheduler = SentinelScheduler(
        scheduler=BackgroundScheduler(timezone=UTC),
        clock=FrozenClock(NOW),
    )

    def fail() -> None:
        raise RuntimeError("scan failed")

    scheduler.schedule_scan(target_id="payments", interval_minutes=60, task=fail)

    with pytest.raises(RuntimeError, match="scan failed"):
        scheduler.run_now("payments")
    with pytest.raises(KeyError, match="missing"):
        scheduler.run_now("missing")


def test_start_and_shutdown_are_idempotent_at_lifecycle_edges() -> None:
    scheduler = SentinelScheduler(
        scheduler=BackgroundScheduler(timezone=UTC),
        clock=FrozenClock(NOW),
    )

    assert scheduler.shutdown(wait=False) is False
    assert scheduler.start(paused=True) is True
    running_after_start = scheduler.running
    assert running_after_start is True
    assert scheduler.start(paused=True) is False
    assert scheduler.shutdown(wait=False) is True
    running_after_stop = scheduler.running
    closed_after_stop = scheduler.closed
    assert running_after_stop is False
    assert closed_after_stop is True
    assert scheduler.shutdown(wait=False) is False
    with pytest.raises(RuntimeError, match="restart"):
        scheduler.start()
    with pytest.raises(RuntimeError, match="shutdown"):
        scheduler.schedule_scan(
            target_id="payments",
            interval_minutes=60,
            task=lambda: None,
        )


@pytest.mark.parametrize("interval", [0, -1, 525_601])
def test_invalid_interval_values_are_rejected(interval: int) -> None:
    scheduler = SentinelScheduler(
        scheduler=BackgroundScheduler(timezone=UTC),
        clock=FrozenClock(NOW),
    )

    with pytest.raises(ValueError):
        scheduler.schedule_scan(
            target_id="payments",
            interval_minutes=interval,
            task=lambda: None,
        )


def test_non_integer_interval_and_invalid_target_are_rejected() -> None:
    scheduler = SentinelScheduler(
        scheduler=BackgroundScheduler(timezone=UTC),
        clock=FrozenClock(NOW),
    )

    with pytest.raises(TypeError):
        scheduler.schedule_scan(
            target_id="payments",
            interval_minutes=True,
            task=lambda: None,
        )
    for target in ("", " payments", "payments\nadmin"):
        with pytest.raises(ValueError):
            scheduler.schedule_scan(
                target_id=target,
                interval_minutes=60,
                task=lambda: None,
            )


def test_naive_clock_is_rejected_before_job_creation() -> None:
    backend = BackgroundScheduler(timezone=UTC)
    scheduler = SentinelScheduler(
        scheduler=backend,
        clock=FrozenClock(datetime(2030, 1, 1)),
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        scheduler.schedule_scan(
            target_id="payments",
            interval_minutes=60,
            task=lambda: None,
        )
    assert backend.get_jobs() == []
