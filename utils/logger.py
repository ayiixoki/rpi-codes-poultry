import time
from firebase_service import write_log

_last_log = {}

# Prevent duplicate logs within 5 minutes
DUPLICATE_INTERVAL = 300


def _write(log_type, title, message):
    now = time.time()

    key = f"{log_type}:{title}:{message}"

    if key in _last_log:
        if now - _last_log[key] < DUPLICATE_INTERVAL:
            return

    _last_log[key] = now

    entry = {
        "type": log_type,
        "title": title,
        "message": message,
        "timestamp": int(now * 1000),
        "is_read": False,
    }

    write_log(entry)


def log_system(message):
    _write("info", "System", message)


def log_feed(amount, triggered_by, percent=None):
    if percent is None:
        message = f"{amount} g ({triggered_by})"
    else:
        message = f"{amount} g ({percent}%) ({triggered_by})"
    _write(
        "feeding",
        "Feed Dispensed",
        message,
    )


def log_water(triggered_by):
    _write(
        "water",
        "Water Dispensed",
        triggered_by,
    )


def log_actuator(device, state, reason=None):
    if reason is None:
        msg = state
    else:
        msg = f"{state} ({reason})"

    _write("climate", device, msg)


def log_alert(title, message):
    _write("alert", title, message)