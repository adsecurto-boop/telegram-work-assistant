# bot/scheduler.py

from threading import Timer

# simple scheduler placeholder
_scheduled = []

def schedule(delay, fn, *args, **kwargs):
    t = Timer(delay, fn, args=args, kwargs=kwargs)
    t.start()
    _scheduled.append(t)
    return t
