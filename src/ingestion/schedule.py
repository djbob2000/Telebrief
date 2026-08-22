"""Pure collection scheduling policy: when is a source due for a scan.

The policy is deliberately free of I/O, clocks, and queues: the dispatcher
hands it the persisted checkpoint plus Procrastinate's scheduled timestamp and
gets a boolean. Rules, in evaluation order:

1. ``backoff_until > scheduled_at`` suppresses scheduling BEFORE any policy
   evaluation (rate-limited sources are rescheduled by later dispatcher ticks,
   never hammered);
2. no checkpoint, or no successful scan yet, means due immediately;
3. otherwise due iff ``last_success_at + interval <= scheduled_at``, with the
   per-source interval read from
   ``source.collector_options["schedule"]["interval_minutes"]``
   (bootstrap writes it) and a default of 45 minutes when absent or malformed.

Plan 5 extends this policy with Facebook ``daily_times``; interval logic here
stays untouched there.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.domain.sources import Source
from src.ingestion.models import CollectionCheckpoint

DEFAULT_INTERVAL_MINUTES = 45

_SCHEDULE_KEY = "schedule"
_INTERVAL_KEY = "interval_minutes"


class CollectionSchedulePolicy:
    """Decide whether one source is due for collection at a given instant."""

    def is_due(
        self,
        source: Source,
        checkpoint: CollectionCheckpoint | None,
        scheduled_at: datetime,
    ) -> bool:
        """Evaluate backoff suppression first, then the interval rule."""
        if checkpoint is not None and checkpoint.backoff_until is not None:
            backoff_until = _as_utc(checkpoint.backoff_until)
            if backoff_until > _as_utc(scheduled_at):
                return False

        if checkpoint is None or checkpoint.last_success_at is None:
            return True

        last_success_at = _as_utc(checkpoint.last_success_at)
        interval = timedelta(minutes=self.interval_minutes(source))
        return last_success_at + interval <= _as_utc(scheduled_at)

    def interval_minutes(self, source: Source) -> int:
        """Per-source collection interval in minutes; default when unparseable."""
        options = source.collector_options or {}
        schedule = options.get(_SCHEDULE_KEY)
        if isinstance(schedule, dict):
            raw = schedule.get(_INTERVAL_KEY)
            # bool is an int subclass; reject it explicitly so True/False never
            # masquerade as intervals.
            if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                return raw
        return DEFAULT_INTERVAL_MINUTES


def _as_utc(value: datetime) -> datetime:
    """Normalize to aware UTC; naive datetimes are interpreted as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
