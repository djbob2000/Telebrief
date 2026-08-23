"""Pure collection scheduling policy: when is a source due for a scan (Plan 2 & Plan 5)."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from src.domain.sources import Source
from src.ingestion.models import CollectionCheckpoint

DEFAULT_INTERVAL_MINUTES = 45

_SCHEDULE_KEY = "schedule"
_INTERVAL_KEY = "interval_minutes"
_DAILY_TIMES_KEY = "daily_times"
_TIMEZONE_KEY = "timezone"


class CollectionSchedulePolicy:
    """Decide whether one source is due for collection at a given instant."""

    def is_due(
        self,
        source: Source,
        checkpoint: CollectionCheckpoint | None,
        scheduled_at: datetime,
    ) -> bool:
        """Evaluate backoff suppression first, then daily_times or interval rule."""
        if checkpoint is not None and checkpoint.backoff_until is not None:
            backoff_until = _as_utc(checkpoint.backoff_until)
            if backoff_until > _as_utc(scheduled_at):
                return False

        if checkpoint is None or checkpoint.last_success_at is None:
            return True

        options = source.collector_options or {}
        schedule = options.get(_SCHEDULE_KEY) or {}

        # Facebook / daily_times policy
        if (
            schedule.get("type") == "daily_times"
            or _DAILY_TIMES_KEY in schedule
            or source.platform == "facebook"
        ):
            daily_times_raw = schedule.get(_DAILY_TIMES_KEY) or ["08:00", "12:00", "16:00", "19:30"]
            tz_str = schedule.get(_TIMEZONE_KEY) or "UTC"
            return self._is_daily_time_due(
                daily_times_raw=daily_times_raw,
                timezone_str=tz_str,
                last_success_at=checkpoint.last_success_at,
                scheduled_at=scheduled_at,
            )

        # Standard interval policy
        last_success_at = _as_utc(checkpoint.last_success_at)
        interval = timedelta(minutes=self.interval_minutes(source))
        return last_success_at + interval <= _as_utc(scheduled_at)

    def _is_daily_time_due(
        self,
        *,
        daily_times_raw: list[str],
        timezone_str: str,
        last_success_at: datetime,
        scheduled_at: datetime,
    ) -> bool:
        try:
            tz = ZoneInfo(timezone_str)
        except (KeyError, ValueError, TypeError):
            tz = ZoneInfo("UTC")

        sched_local = _as_utc(scheduled_at).astimezone(tz)
        last_local = _as_utc(last_success_at).astimezone(tz)

        # Find any slot today (or yesterday) that has passed sched_local but last_success is before it
        slots: list[time] = []
        for t_str in daily_times_raw:
            if not isinstance(t_str, str) or ":" not in t_str:
                continue
            parts = t_str.strip().split(":")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                h, m = int(parts[0]), int(parts[1])
                if 0 <= h <= 23 and 0 <= m <= 59:
                    slots.append(time(hour=h, minute=m))

        if not slots:
            slots = [time(8, 0), time(12, 0), time(16, 0), time(19, 30)]

        slots.sort()

        # Check today's slots up to sched_local
        for slot in slots:
            slot_dt = datetime.combine(sched_local.date(), slot, tzinfo=tz)
            if slot_dt <= sched_local and last_local < slot_dt:
                return True

        return False

    def interval_minutes(self, source: Source) -> int:
        """Per-source collection interval in minutes; default when unparseable."""
        options = source.collector_options or {}
        schedule = options.get(_SCHEDULE_KEY)
        if isinstance(schedule, dict):
            raw = schedule.get(_INTERVAL_KEY)
            if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                return raw
        return DEFAULT_INTERVAL_MINUTES


def _as_utc(value: datetime) -> datetime:
    """Normalize to aware UTC; naive datetimes are interpreted as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
