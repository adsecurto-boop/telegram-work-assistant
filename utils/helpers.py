# utils/helpers.py

def format_datetime(dt):
    """Simple datetime formatting helper placeholder"""
    return dt.isoformat() if hasattr(dt, 'isoformat') else str(dt)
