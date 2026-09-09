"""Shift-local scheduling, including shifts crossing midnight."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

def clock_on_shift(value, start):
    if 'T' in value:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            result = result.replace(tzinfo=start.tzinfo)
        return result
    parsed = datetime.strptime(value, '%H:%M').time()
    result = start.replace(hour=parsed.hour,minute=parsed.minute,second=0,microsecond=0)
    if result <= start:
        result += timedelta(days=1)
    return result

def new_shift(timezone, start_text=None, end_text=None):
    current = datetime.now(ZoneInfo(timezone))
    if start_text:
        if 'T' in start_text:
            start = datetime.fromisoformat(start_text)
            if start.tzinfo is None:
                start = start.replace(tzinfo=current.tzinfo)
        else:
            parsed = datetime.strptime(start_text,'%H:%M').time()
            start = current.replace(hour=parsed.hour,minute=parsed.minute,second=0,microsecond=0)
    else:
        start = current
    end = clock_on_shift(end_text,start) if end_text else start + timedelta(hours=9)
    validate_schedule(start,end,None)
    return start,end

def validate_schedule(start,end,lunch):
    if not timedelta(0) < end-start <= timedelta(hours=24):
        raise ValueError('Shift must last between 0 and 24 hours.')
    if lunch and not start < lunch < end:
        raise ValueError('Lunch must be between shift start and end.')
