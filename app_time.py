# app_time.py
"""One place that decides what time the app displays.

WHY THIS EXISTS
The container runs UTC, so the dashboard clock read 03:02:48 while the
operator's machine read 22:30 -- a five-hour gap between the log line and
the wall clock, on a system whose whole job is deciding when to act.

WHAT IT DOES AND DOESN'T CHANGE
Display only. Every timestamp column in schema.sql is TIMESTAMP WITH TIME
ZONE, so stored instants are absolute and unaffected by this; changing the
display zone cannot retroactively reinterpret a row. That distinction is
the entire safety argument for this change:

    STORE in UTC (absolute, unambiguous, DST-free)
    DISPLAY in the operator's zone (readable, matches their wall clock)

Storing local wall-clock time instead would be actively dangerous here.
Eastern repeats the 01:00-02:00 hour every November, so two different
instants would share one timestamp -- and 'which of these two trades came
first' is not a question a trading ledger should ever have to guess at.

DST IS HANDLED, NOT ASSUMED
America/New_York is EDT (UTC-4) in summer and EST (UTC-5) in winter.
zoneinfo applies whichever is correct for the instant being formatted, so a
timestamp from July and one from January both render correctly. A fixed -5
offset would silently be an hour wrong for eight months of the year.
"""
import os
import logging
from datetime import datetime, timezone, tzinfo
from typing import Optional

logger = logging.getLogger("app_time")

# IANA zone name. America/New_York rather than "EST" or a fixed offset,
# precisely so DST transitions are applied automatically.
APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "America/New_York")

_tz_cache: Optional[tzinfo] = None
_tz_warned = False


def get_tz() -> tzinfo:
    """The configured display zone, or UTC if it can't be loaded.

    Falls back rather than raising. A missing tzdata package should make the
    clock read UTC, not take the trading pipeline down -- but it warns once,
    because silently showing the wrong zone is exactly the failure this
    module was written to end.
    """
    global _tz_cache, _tz_warned
    if _tz_cache is not None:
        return _tz_cache
    try:
        from zoneinfo import ZoneInfo
        _tz_cache = ZoneInfo(APP_TIMEZONE)
    except Exception as e:
        if not _tz_warned:
            logger.warning(
                f"Could not load timezone {APP_TIMEZONE!r} ({e}); displaying UTC instead. "
                f"Install the tzdata package in the image to fix this."
            )
            _tz_warned = True
        _tz_cache = timezone.utc
    return _tz_cache


def to_local(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert any datetime to the display zone.

    A NAIVE datetime is assumed to be UTC. That assumption is safe here
    because every stored timestamp is timezone-aware and the container
    clock is UTC -- but it is an assumption, so prefer passing aware
    datetimes wherever you can.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(get_tz())


def now_local() -> datetime:
    """Current time in the display zone, timezone-aware."""
    return datetime.now(timezone.utc).astimezone(get_tz())


def format_local(dt: Optional[datetime] = None, fmt: str = "%H:%M:%S") -> str:
    """Format a datetime (default: now) in the display zone."""
    target = now_local() if dt is None else to_local(dt)
    return target.strftime(fmt) if target else ""


def tz_label(dt: Optional[datetime] = None) -> str:
    """Short zone name for the instant given -- 'EDT' in July, 'EST' in January.

    Shown next to the dashboard clock. A bare time with no zone is what
    caused the confusion in the first place.
    """
    target = now_local() if dt is None else to_local(dt)
    if target is None:
        return ""
    return target.strftime("%Z") or APP_TIMEZONE
