"""Shift-local scheduling, including shifts crossing midnight."""
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import config


def clock_on_shift(value, start):
    if 'T' in value:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            result = result.replace(tzinfo=start.tzinfo)
        return result
    parsed = datetime.strptime(value, '%H:%M').time()
    result = start.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
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
            parsed = datetime.strptime(start_text, '%H:%M').time()
            start = current.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
    else:
        start = current
    end = clock_on_shift(end_text, start) if end_text else start + timedelta(hours=9)
    validate_schedule(start, end, None)
    return start, end


def validate_schedule(start, end, lunch):
    base_dt = datetime(2026, 1, 1)

    def _to_dt(val, name):
        if val is None:
            return None
        if isinstance(val, datetime):
            return val
        if isinstance(val, time):
            return base_dt.replace(hour=val.hour, minute=val.minute, second=0, microsecond=0)
        if isinstance(val, str):
            parts = val.split(':')
            if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
                raise ValueError(f"Invalid {name} time format: {val!r}")
            h, m = int(parts[0]), int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError(f"Invalid {name} hours/minutes: {val!r}")
            return base_dt.replace(hour=h, minute=m, second=0, microsecond=0)
        raise ValueError(f"Unsupported {name} type: {type(val)}")

    s_dt = _to_dt(start, 'start')
    e_dt = _to_dt(end, 'end')
    l_dt = _to_dt(lunch, 'lunch') if lunch else None

    if e_dt <= s_dt:
        e_dt += timedelta(days=1)

    if not (timedelta(0) < e_dt - s_dt <= timedelta(hours=24)):
        raise ValueError('Shift must last between 0 and 24 hours.')

    if l_dt:
        if l_dt < s_dt:
            l_dt += timedelta(days=1)
        if not (s_dt < l_dt < e_dt):
            raise ValueError('Lunch must be between shift start and end.')


def infer_lunch_time(start_str: str, end_str: str) -> str:
    """Infers a reasonable lunch time (e.g. 4 hours into shift or midpoint) if omitted."""
    s_h, s_m = map(int, start_str.split(':'))
    e_h, e_m = map(int, end_str.split(':'))
    start_minutes = s_h * 60 + s_m
    end_minutes = e_h * 60 + e_m
    if end_minutes <= start_minutes:
        end_minutes += 24 * 60  # Cross midnight
    duration = end_minutes - start_minutes

    # If shift is 8+ hours, place lunch 4 hours after start; else midpoint
    if duration >= 480:
        lunch_minutes = (start_minutes + 240) % (24 * 60)
    else:
        lunch_minutes = (start_minutes + duration // 2) % (24 * 60)

    l_h = lunch_minutes // 60
    l_m = lunch_minutes % 60
    return f'{l_h:02d}:{l_m:02d}'


def assign_template_range(db, template_identifier: str | int, start_date: str, end_date: str,
                          skip_existing_overrides: bool = True, *, start_time: str | None = None,
                          end_time: str | None = None, lunch_time: str | None = None,
                          correlation_id: str | None = None) -> int:
    """
    Assigns a shift template to a range of dates.
    Honors template's active_weekdays.
    If skip_existing_overrides is True, will never overwrite an explicit daily override.
    """
    template = db.get_shift_template(template_identifier)
    if not template:
        raise ValueError(f'Shift template "{template_identifier}" not found.')

    cur_dt = datetime.strptime(start_date, '%Y-%m-%d')
    end_dt = datetime.strptime(end_date, '%Y-%m-%d')
    assigned_count = 0

    allowed_weekdays = None
    if template.get('active_weekdays'):
        try:
            allowed_weekdays = {int(x.strip()) for x in str(template['active_weekdays']).split(',') if x.strip().isdigit()}
        except Exception:
            allowed_weekdays = None

    while cur_dt <= end_dt:
        date_str = cur_dt.strftime('%Y-%m-%d')

        # Check weekday eligibility
        if allowed_weekdays is not None and cur_dt.weekday() not in allowed_weekdays:
            cur_dt += timedelta(days=1)
            continue

        if skip_existing_overrides:
            existing = db.get_shift_calendar_override(date_str)
            # Never overwrite an explicit override
            if existing and (existing.get('is_explicit_override') or existing.get('is_day_off') or existing.get('start_time')):
                cur_dt += timedelta(days=1)
                continue

        db.set_shift_calendar_override(
            date_str=date_str,
            template_id=template['id'],
            start_time=start_time or template['start_time'],
            lunch_time=lunch_time or template['lunch_time'] or infer_lunch_time(
                start_time or template['start_time'], end_time or template['end_time']),
            end_time=end_time or template['end_time'],
            is_day_off=0,
            is_explicit_override=0,
            note=f"Assigned template: {template['name']}",
            correlation_id=correlation_id
        )
        assigned_count += 1
        cur_dt += timedelta(days=1)

    return assigned_count


def preview_calendar_week(db, reference_date: str = None, days: int = 7) -> list[dict]:
    """Returns scheduled shifts for the next N days starting from reference_date."""
    ref = datetime.strptime(reference_date, '%Y-%m-%d') if reference_date else datetime.now(ZoneInfo(config.TIMEZONE))
    results = []
    for i in range(days):
        dt = ref + timedelta(days=i)
        d_str = dt.strftime('%Y-%m-%d')
        info = db.get_shift_for_date(d_str)
        info['weekday_name'] = dt.strftime('%A')
        results.append(info)
    return results


def format_shift_preview(preview_items: list[dict]) -> str:
    """Formats 7-day preview into clean workplace text."""
    lines = ['📅 Shift Schedule (Next 7 Days):']
    for item in preview_items:
        d = item['date']
        day_name = item.get('weekday_name', '')
        if item.get('is_day_off'):
            note = f" ({item['note']})" if item.get('note') else ''
            lines.append(f"• {d} ({day_name}): Day Off{note}")
        else:
            start = item.get('start_time', '10:00')
            lunch = item.get('lunch_time') or 'Flexible'
            end = item.get('end_time', '19:00')
            src = f" [{item.get('source')}]" if item.get('source') else ''
            lines.append(f"• {d} ({day_name}): {start}–{end} (Lunch: {lunch}){src}")
    return '\n'.join(lines)


def check_missing_shift_assignments(db, days_ahead: int = 7) -> list[str]:
    """Identifies upcoming dates that fall back to the generic default schedule."""
    today = datetime.now(ZoneInfo(config.TIMEZONE))
    missing = []
    for i in range(days_ahead):
        dt = today + timedelta(days=i)
        d_str = dt.strftime('%Y-%m-%d')
        info = db.get_shift_for_date(d_str)
        if info.get('source') == 'default':
            missing.append(f"{d_str} ({dt.strftime('%A')})")
    return missing
