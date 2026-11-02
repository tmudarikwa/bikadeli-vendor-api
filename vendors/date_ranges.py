from datetime import date, datetime, time, timedelta

from django.utils import timezone


def local_day_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    """Return aware bounds covering local calendar dates, with an exclusive end."""
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(start_date, time.min), tz)
    end = timezone.make_aware(datetime.combine(end_date + timedelta(days=1), time.min), tz)
    return start, end
