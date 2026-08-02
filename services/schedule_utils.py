"""
services/schedule_utils.py — IST posting slot utilities.

Note: services/post_manager.py also has a next_available_slot() function.
This module provides:
  - next_available_slot()   → same logic, re-exported so content_pipeline can import it
  - human_readable_slot()   → converts "2026-08-05 08:30" → "Wednesday 5 Aug at 8:30 AM IST"
  - ist_now_str()           → current IST as "YYYY-MM-DD HH:MM"

Posting schedule (master doc Section 12):
  Tuesday   08:30 IST
  Wednesday 12:00 IST
  Thursday  09:00 IST

All times plain IST strings. No UTC conversion. Ever.
"""

from __future__ import annotations

import datetime
import pytz

_IST = pytz.timezone("Asia/Kolkata")


def ist_now() -> datetime.datetime:
    return datetime.datetime.now(_IST)


def ist_now_str() -> str:
    return ist_now().strftime("%Y-%m-%d %H:%M")


def next_available_slot(after: datetime.datetime | None = None) -> str:
    """
    Find the next Tue/Wed/Thu posting slot.
    Delegates to post_manager.next_available_slot() to avoid duplicating logic.
    Falls back to local implementation if import fails.

    Returns IST string like "2026-08-05 08:30".
    """
    try:
        from services.post_manager import next_available_slot as pm_next_slot
        return pm_next_slot(after=after)
    except ImportError:
        return _compute_next_slot(after)


def _compute_next_slot(from_dt: datetime.datetime | None = None) -> str:
    """Direct implementation — used if post_manager import fails."""
    _SLOTS = [(1, 8, 30), (2, 12, 0), (3, 9, 0)]  # (weekday, hour, minute)
    if from_dt is None:
        from_dt = ist_now()
    elif from_dt.tzinfo is None:
        from_dt = _IST.localize(from_dt)

    for delta in range(14):
        candidate_date = (from_dt + datetime.timedelta(days=delta)).date()
        weekday = candidate_date.weekday()
        for wd, hour, minute in _SLOTS:
            if weekday == wd:
                slot_dt = _IST.localize(datetime.datetime(
                    candidate_date.year, candidate_date.month,
                    candidate_date.day, hour, minute
                ))
                if slot_dt > from_dt:
                    return slot_dt.strftime("%Y-%m-%d %H:%M")

    fallback = from_dt + datetime.timedelta(days=7)
    return fallback.strftime("%Y-%m-%d 08:30")


def human_readable_slot(slot_str: str) -> str:
    """
    Convert "2026-08-05 08:30" → "Wednesday 5 Aug at 8:30 AM IST"
    """
    try:
        dt = datetime.datetime.strptime(slot_str, "%Y-%m-%d %H:%M")
        day_name = dt.strftime("%A")
        day = str(dt.day)  # non-padded, cross-platform
        month = dt.strftime("%b")
        hour = dt.hour
        minute = dt.minute
        am_pm = "AM" if hour < 12 else "PM"
        display_hour = hour if hour <= 12 else hour - 12
        if display_hour == 0:
            display_hour = 12
        time_str = f"{display_hour}:{minute:02d} {am_pm}"
        return f"{day_name} {day} {month} at {time_str} IST"
    except Exception:
        return slot_str
