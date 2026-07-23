"""APScheduler orchestration for periodic lifecycle scans."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from apscheduler.job import Job
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.triggers.interval import IntervalTrigger

ScanTask = Callable[[], object]


class Clock(Protocol):
    """Clock used to make schedule creation deterministic."""

    def now(self) -> datetime:
        """Return a timezone-aware current instant."""


class SystemClock:
    """UTC wall clock used outside tests."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class SentinelScheduler:
    """Own periodic scan jobs and expose deterministic lifecycle operations."""

    def __init__(
        self,
        *,
        scheduler: BaseScheduler | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._scheduler = scheduler if scheduler is not None else BackgroundScheduler(timezone=UTC)
        self._clock = clock if clock is not None else SystemClock()
        self._tasks: dict[str, ScanTask] = {}
        self._started = bool(self._scheduler.running)
        self._closed = False

    @property
    def running(self) -> bool:
        """Whether this wrapper has started its scheduler."""

        return self._started

    @property
    def closed(self) -> bool:
        """Whether a started scheduler has been permanently shut down."""

        return self._closed

    def schedule_scan(
        self,
        *,
        target_id: str,
        interval_minutes: int,
        task: ScanTask,
        start_immediately: bool = False,
    ) -> Job:
        """Add or replace one interval job for a target."""

        if self._closed:
            raise RuntimeError("cannot schedule work after scheduler shutdown")
        normalized_target = _validate_target_id(target_id)
        interval = _validate_interval(interval_minutes)
        if not callable(task):
            raise TypeError("task must be callable")

        now = _clock_now(self._clock)
        start_date = now if start_immediately else now + interval
        trigger = IntervalTrigger(
            minutes=interval_minutes,
            start_date=start_date,
            timezone=UTC,
        )
        job_id = scan_job_id(normalized_target)
        misfire_grace_time = max(60, min(interval_minutes * 60, 3600))
        # APScheduler defers ``replace_existing`` until start for pending jobs.
        # Remove a pending/running predecessor first so callers always observe one job.
        if self._scheduler.get_job(job_id) is not None:
            self._scheduler.remove_job(job_id)
        job = self._scheduler.add_job(
            task,
            trigger=trigger,
            id=job_id,
            name=f"Lifecycle scan: {normalized_target}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=misfire_grace_time,
        )
        self._tasks[job_id] = task
        return job

    def job_for(self, target_id: str) -> Job | None:
        """Return the configured APScheduler job for a target."""

        return self._scheduler.get_job(scan_job_id(_validate_target_id(target_id)))

    def run_now(self, target_id: str) -> object:
        """Run one configured scan synchronously and propagate task failures."""

        job_id = scan_job_id(_validate_target_id(target_id))
        task = self._tasks.get(job_id)
        if task is None:
            raise KeyError(f"no scan job is configured for target {target_id!r}")
        return task()

    def start(self, *, paused: bool = False) -> bool:
        """Start once; return false when already running."""

        if self._closed:
            raise RuntimeError("cannot restart a scheduler after shutdown")
        if self._started:
            return False
        self._scheduler.start(paused=paused)
        self._started = True
        return True

    def shutdown(self, *, wait: bool = True) -> bool:
        """Shut down a running scheduler once without raising on repeats."""

        if not self._started:
            return False
        self._scheduler.shutdown(wait=wait)
        self._started = False
        self._closed = True
        return True


def scan_job_id(target_id: str) -> str:
    """Return a stable, scheduler-safe job id for a target."""

    normalized_target = _validate_target_id(target_id)
    digest = hashlib.sha256(normalized_target.encode("utf-8")).hexdigest()[:20]
    return f"sunset-sentinel.scan.{digest}"


def _validate_target_id(target_id: str) -> str:
    if not target_id or target_id != target_id.strip():
        raise ValueError("target_id must be a non-empty trimmed string")
    if len(target_id) > 256:
        raise ValueError("target_id must not exceed 256 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in target_id):
        raise ValueError("target_id must not contain control characters")
    return target_id


def _validate_interval(interval_minutes: int) -> timedelta:
    if isinstance(interval_minutes, bool) or not isinstance(interval_minutes, int):
        raise TypeError("interval_minutes must be an integer")
    if interval_minutes < 1:
        raise ValueError("interval_minutes must be at least one")
    if interval_minutes > 525_600:
        raise ValueError("interval_minutes must not exceed one year")
    return timedelta(minutes=interval_minutes)


def _clock_now(clock: Clock) -> datetime:
    value = clock.now()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock.now() must return a timezone-aware datetime")
    return value.astimezone(UTC).replace(microsecond=0)
