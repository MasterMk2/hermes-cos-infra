#!/usr/bin/env python3
"""Attention Engine — time, quiet hours and the billing month.

Every clock read in the engine goes through `Clock`, and the reason is
section 133: quiet-hours behaviour has to be testable without changing the
host's clock. A service that can only be verified by setting the system time
backwards is a service whose most important rule is never verified.

Two conventions, both deliberate:

* **Stored timestamps are UTC, ISO-8601, with an offset.** Comparing them is
  then string-safe and unambiguous. Nothing in the database is naive.
* **Decisions are made in the policy timezone.** "23:00" means 23:00 where
  the owner is, and "how many days are left this month" means days left in
  the month the owner is living in. Doing that arithmetic in UTC is off by
  nine hours here, which at a month boundary is a whole day of budget.

`zoneinfo` is used rather than a fixed +09:00 offset. Japan has no daylight
saving today, so the two agree — but writing the offset in would be a claim
about the future, and a timezone-aware datetime costs nothing (section 123).
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ISO_Z = "%Y-%m-%dT%H:%M:%S%z"

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(value: str) -> time:
    m = _HHMM.match(str(value or "").strip())
    if not m:
        raise ValueError(f"expected HH:MM, got {value!r}")
    return time(int(m.group(1)), int(m.group(2)))


def to_utc(dt: datetime) -> datetime:
    """Any aware datetime as UTC; a naive one is read as UTC.

    Naive input is a bug at the call site, but the safe reading here is UTC
    because that is what everything this engine stores is. Guessing local
    time would silently shift a stored deadline by nine hours.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_iso(value) -> datetime | None:
    """Lenient ISO-8601 parse for values that come from other components.

    The Event Layer writes offsets ('+09:00'), the jobs supervisor writes
    UTC with '+00:00', and some rows carry a trailing 'Z'. All three have to
    land on the same axis, and a value that will not parse must produce None
    rather than an exception: a single malformed row in someone else's table
    must not stop the whole scan.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return to_utc(datetime.fromisoformat(s))
    except ValueError:
        # A few rows in the wild are 'YYYY-MM-DD HH:MM:SS' with a space.
        try:
            return to_utc(datetime.fromisoformat(s.replace(" ", "T", 1)))
        except ValueError:
            return None


def iso(dt: datetime) -> str:
    return to_utc(dt).isoformat()


@dataclass(frozen=True)
class Clock:
    """The engine's only source of "now".

    `fixed` exists for tests and for the `--at` diagnostic, which is how the
    quiet-hours boundaries in section 123 are checked: by asking the policy
    what it would do at 22:59 and at 23:00, not by moving the host's clock.
    """

    tz_name: str = "Asia/Tokyo"
    fixed: datetime | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)

    def now(self) -> datetime:
        return to_utc(self.fixed) if self.fixed else datetime.now(timezone.utc)

    def local(self) -> datetime:
        return self.now().astimezone(self.tz)

    def now_iso(self) -> str:
        return iso(self.now())

    def at(self, when: datetime) -> "Clock":
        return Clock(tz_name=self.tz_name, fixed=to_utc(when))

    def local_of(self, dt: datetime) -> datetime:
        return to_utc(dt).astimezone(self.tz)

    def day_key(self, dt: datetime | None = None) -> str:
        """YYYY-MM-DD in the policy timezone — the daily budget's bucket."""
        return self.local_of(dt or self.now()).strftime("%Y-%m-%d")

    def month_key(self, dt: datetime | None = None) -> str:
        return self.local_of(dt or self.now()).strftime("%Y-%m")


# ------------------------------------------------------------ quiet hours


@dataclass(frozen=True)
class QuietHours:
    """A window in the policy timezone, possibly crossing midnight."""

    start: time
    end: time
    enabled: bool = True

    @classmethod
    def parse(cls, start, end, enabled=True) -> "QuietHours":
        return cls(parse_hhmm(start), parse_hhmm(end), bool(enabled))

    def contains(self, local_dt: datetime) -> bool:
        """Half-open: [start, end). 23:00 is inside, 08:00 is not.

        Half-open rather than closed because the two boundaries mean opposite
        things — one begins the silence and one ends it — and a closed
        interval makes 08:00 both. Section 123 checks 22:59 / 23:00 / 07:59 /
        08:00 for exactly this.
        """
        if not self.enabled:
            return False
        t = local_dt.timetz().replace(tzinfo=None)
        if self.start == self.end:
            return False                      # a zero-width window is "off"
        if self.start < self.end:             # e.g. 01:00-05:00
            return self.start <= t < self.end
        return t >= self.start or t < self.end    # wraps midnight

    def next_end(self, local_dt: datetime) -> datetime:
        """When the current (or next) quiet window ends, in local time.

        Used to schedule a DEFER: a notification held at 23:40 is released at
        08:00, not "in eight hours", so several deferred items land together
        as one catch-up rather than trickling in (section 57).
        """
        candidate = local_dt.replace(
            hour=self.end.hour, minute=self.end.minute,
            second=0, microsecond=0,
        )
        if candidate <= local_dt:
            candidate += timedelta(days=1)
        return candidate


# -------------------------------------------------------- billing month


def days_remaining_in_month(local_dt: datetime) -> int:
    """Days left in the calendar month, counting today. Never below 1.

    LINE's allowance is monthly and resets on the calendar month, so the
    denominator of "how much may I spend today" is this. Counting today keeps
    the figure from going to zero on the last day, which would either divide
    by zero or hand the whole remaining balance to one day.
    """
    last = calendar.monthrange(local_dt.year, local_dt.month)[1]
    return max(1, last - local_dt.day + 1)


def month_bounds(local_dt: datetime) -> tuple:
    """(first day, last day) of the local month, as dates."""
    last = calendar.monthrange(local_dt.year, local_dt.month)[1]
    return (date(local_dt.year, local_dt.month, 1),
            date(local_dt.year, local_dt.month, last))


def humanize_delta(seconds: float) -> str:
    """A short Japanese duration for a notification body.

    Rounded deliberately: "約14時間" is the right precision for "should I do
    this tonight?", and a notification that says "13時間47分" invites the
    reader to check the arithmetic instead of the deadline (section 69).
    """
    seconds = int(seconds)
    if seconds < 0:
        return "期限超過"
    if seconds < 3600:
        return f"約{max(1, seconds // 60)}分"
    if seconds < 86400:
        return f"約{seconds // 3600}時間"
    return f"約{seconds // 86400}日"


def overdue_by(seconds: float) -> str:
    seconds = int(abs(seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}分"
    if seconds < 86400:
        return f"{seconds // 3600}時間"
    return f"{seconds // 86400}日"
