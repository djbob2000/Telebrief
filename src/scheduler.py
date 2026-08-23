"""Compatibility/status facade over the Procrastinate publication schedule.

Since the Plan 4 cutover this module owns NO clock: durable scheduling is a
Procrastinate periodic dispatcher (:mod:`src.jobs.schedules`) executed by the
worker process. ``DigestScheduler`` survives only so existing bot/help/status
wiring keeps working; ``start()``/``stop()`` change nothing but a flag, and
``get_next_run_time()`` formats the next configured slot from configuration.
"""

from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from src.config_loader import Config


def format_next_configured_digest_time(config: Config, *, now: dt.datetime) -> str:
    """Human-readable next digest slot derived purely from configuration."""
    tz: dt.tzinfo
    try:
        tz = ZoneInfo(config.settings.timezone)
    except Exception:
        tz = dt.timezone.utc
    hour, minute = _parse_schedule_time(config.settings.schedule_time)
    local_now = now.astimezone(tz).replace(second=0, microsecond=0)
    candidate = local_now.replace(hour=hour, minute=minute)
    if candidate <= local_now:
        candidate += dt.timedelta(days=1)
    return candidate.strftime("%Y-%m-%d %H:%M:%S %Z")


def _parse_schedule_time(time_str: str) -> tuple[int, int]:
    """Parse ``HH:MM``; falls back to 08:00 when invalid."""
    try:
        hour, minute = map(int, time_str.split(":"))
    except Exception:
        return 8, 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return 8, 0
    return hour, minute


class DigestScheduler:
    """Status-only compatibility facade; the worker owns the actual clock."""

    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.is_running = False

    def start(self) -> None:
        """Mark scheduling active. Durable dispatch happens in the worker."""
        if self.is_running:
            self.logger.warning("Scheduler already running")
            return
        self.is_running = True
        self.logger.info("Publication scheduling is handled by Procrastinate workers")

    def stop(self) -> None:
        """Mark scheduling inactive in this process."""
        self.is_running = False

    def get_next_run_time(self) -> str:
        """Next configured digest slot for user-facing status text."""
        if not self.is_running:
            return "Scheduler not running"
        return format_next_configured_digest_time(self.config, now=dt.datetime.now(dt.timezone.utc))

    def get_schedule_description(self) -> str:
        """Return configured run times for user-facing status/help text."""
        return f"{self.config.settings.schedule_time} ({self.config.settings.lookback_hours}h)"


async def main() -> None:
    """Status demo entry: print the next configured slot and exit."""
    from src.config_loader import load_config
    from src.utils import setup_logging

    config = load_config()
    logger = setup_logging(config.log_level)

    scheduler = DigestScheduler(config, logger)
    scheduler.start()
    logger.info(f"Next configured digest slot: {scheduler.get_next_run_time()}")
    scheduler.stop()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
