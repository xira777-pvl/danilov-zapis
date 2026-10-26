"""Day boundaries in the owner's timezone (Asia/Omsk)."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

OMSK = ZoneInfo("Asia/Omsk")


def omsk_today(now: datetime | None = None) -> date:
    now = now or datetime.now(tz=OMSK)
    return now.astimezone(OMSK).date()


def omsk_day_range(day: date) -> tuple[str, str]:
    start = datetime.combine(day, time(0, 0), tzinfo=OMSK)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()
