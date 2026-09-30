import time
import socket
import queue
import threading
from collections import deque
import firebase_admin
from firebase_admin import credentials, db, messaging

SERVICE_KEY_PATH = "/home/pi/poultrycare/serviceAccountKey.json"
DATABASE_URL = "https://poultrycare-f816d-default-rtdb.asia-southeast1.firebasedatabase.app"

# ---------------- Connectivity state ----------------
# Every Firebase call made while the Pi has no internet blocks for seconds
# (Google auth retries). To keep the main loop fast offline, a background
# thread checks the internet every few seconds and all Firebase helpers
# below return immediately while it is down.
CONNECTIVITY_HOST = ("8.8.8.8", 53)
CONNECTIVITY_CHECK_INTERVAL = 1     # seconds between checks

_online = None          # None = not checked yet, True/False afterwards
_monitor_started = False
_state_lock = threading.Lock()
_pending_logs = deque(maxlen=200)   # logs made while offline, sent when back online
_pending_notifications = deque(maxlen=50)   # notifications made while offline
_pending_state = {}                 # path -> merged dict of state writes made offline
_pending_state_lock = threading.Lock()
_online_epoch = 0                   # goes up every time the connection comes back
_writes_since_trim = 0
TRIM_LOGS_EVERY = 20                # trim_logs downloads ALL logs, so don't do it on every write


def is_online():
    return bool(_online)


def get_online_epoch():
    """Changes every time the Pi reconnects. main.py watches it so it can
    refresh thresholds/schedules right away instead of waiting for the timer."""
    return _online_epoch


def _check_internet(timeout=1.5):
    try:
        socket.create_connection(CONNECTIVITY_HOST, timeout=timeout).close()
        return True
    except OSError:     # includes DNS failure and timeouts
        return False


def _set_online(state):
    global _online, _online_epoch
    with _state_lock:
        previous = _online
        if state == previous:
            return
        _online = state
        if state:
            _online_epoch += 1
    if previous is None:
        return          # first check at startup - nothing to announce or flush
    if state:
        print("Internet back - syncing with Firebase")
        threading.Thread(target=_on_reconnect, daemon=True).start()
    else:
        print("Internet lost - running offline")


def _recheck_after_failure():
    """A Firebase call just failed - find out in ~1s if it's because the
    internet is gone, so later calls skip instantly instead of hanging."""
    _set_online(_check_internet(timeout=1))


def _monitor_loop():
    while True:
        # poll faster while offline so we notice the internet coming back sooner
        time.sleep(CONNECTIVITY_CHECK_INTERVAL if _online else 0.5)
        try:
            _set_online(_check_internet())
        except Exception as e:
            print(f"Connectivity monitor error: {e}")


def _flush_pending_logs():
    sent = False
    while _online:
        try:
            entry = _pending_logs.popleft()
        except IndexError:
            break
        try:
            db.reference("logs").push(entry)
            sent = True
        except Exception as e:
            _pending_logs.appendleft(entry)
            print(f"Log sync failed, will retry later: {type(e).__name__}")
            break
    if sent:
        try:
            trim_logs()
        except Exception as e:
            print(f"Error trimming logs: {e}")


def _stash_state(path, data):
    """Remember a state write made while offline (newest value per key wins)."""
    with _pending_state_lock:
        _pending_state.setdefault(path, {}).update(data)


def _flush_pending_state():
    with _pending_state_lock:
        items = list(_pending_state.items())
        _pending_state.clear()
    for path, data in items:
        try:
            db.reference(path).update(data)
        except Exception as e:
            print(f"State sync failed ({path}), will retry later: {type(e).__name__}")
            with _pending_state_lock:
                merged = dict(data)
                merged.update(_pending_state.get(path, {}))   # anything newer wins
                _pending_state[path] = merged
            _recheck_after_failure()
            return


def _flush_pending_notifications():
    while _online:
        try:
            payload = _pending_notifications.popleft()
        except IndexError:
            break
        try:
            db.reference("/notifications").push(payload)
        except Exception as e:
            _pending_notifications.appendleft(payload)
            print(f"Notification sync failed, will retry later: {type(e).__name__}")
            _recheck_after_failure()
            break


def _on_reconnect():
    """Runs once each time the internet comes back. Quickest things first,
    the (possibly long) log backlog last."""
    _flush_pending_state()
    _flush_pending_notifications()
    _flush_pending_logs()


# ---------------- Sensor data push worker (latest-only) ----------------
_latest_push = None
_push_lock = threading.Lock()
_push_event = threading.Event()
_push_worker_started = False


def _push_worker():
    global _latest_push
    while True:
        _push_event.wait()
        _push_event.clear()
        with _push_lock:
            job = _latest_push
            _latest_push = None
        if job is not None:
            args, kwargs = job
            push_sensor_data(*args, **kwargs)


def push_sensor_data_async(*args, **kwargs):
    """Hand the latest reading to the worker thread and return immediately.
    If the worker is stuck on a slow request, older readings are dropped
    and only the newest is sent."""
    global _latest_push
    with _push_lock:
        _latest_push = (args, kwargs)
    _push_event.set()


# ---------------- Ordered write queue ----------------
# Main-thread writes are handed to ONE background worker, so the main loop
# never waits on the network and ON/OFF ordering is preserved.
_write_queue = queue.Queue(maxsize=100)
_write_worker_started = False


def _write_worker():
    while True:
        fn, args = _write_queue.get()
        if not is_online():
            stash = _OFFLINE_STASH.get(fn)
            if stash:
                stash(*args)              # keep it and send when internet returns
            continue                      # everything else is re-sent by the 1s sensor push
        try:
            fn(*args)
        except Exception as e:
            print(f"Background write error: {type(e).__name__}")


def _enqueue_write(fn, *args):
    if not is_online():
        return False
    try:
        _write_queue.put_nowait((fn, args))
    except queue.Full:
        pass
    return True


# ---------------- Init ----------------
def init_firebase():
    global _monitor_started, _push_worker_started, _write_worker_started

    # Local setup only - needs NO internet. If this fails it is a real
    # config problem (bad key path / URL), so let it raise.
    if not firebase_admin._apps:
        cred = credentials.Certificate(SERVICE_KEY_PATH)
        firebase_admin.initialize_app(cred, {
            "databaseURL": DATABASE_URL,
            "httpTimeout": 5,   # default is 120s, this is what caused the 2-3 min delay
        })

    _set_online(_check_internet())
    if not _monitor_started:
        threading.Thread(target=_monitor_loop, daemon=True).start()
        _monitor_started = True

    if not _push_worker_started:
        threading.Thread(target=_push_worker, daemon=True).start()
        _push_worker_started = True

    if not _write_worker_started:
        threading.Thread(target=_write_worker, daemon=True).start()
        _write_worker_started = True

    if not is_online():
        print("No internet at startup - running in offline mode")
        return

    try:
        db.reference('/sensor_data/system_online').set(True)
        print("Firebase initialized successfully")
    except Exception as e:
        print(f"Firebase reachable but system_online write failed: {type(e).__name__}")


# ---------------- Writers (queued, silently skipped while offline) ----------------
def _safe_update_now(path, data):
    try:
        db.reference(path).update(data)
    except Exception as e:
        print(f"Firebase update failed ({path}): {type(e).__name__}")
        _recheck_after_failure()
        if not is_online():
            _stash_state(path, data)


def safe_update(path, data):
    if not is_online():
        _stash_state(path, data)          # e.g. last_feed_time / feeding_progress done offline
        return False
    return _enqueue_write(_safe_update_now, path, data)


def _push_notification_now(payload):
    try:
        db.reference("/notifications").push(payload)
    except Exception as e:
        print(f"Notification push failed: {type(e).__name__}")
        _recheck_after_failure()
        if not is_online():
            _pending_notifications.append(payload)


def push_notification(payload):
    if not is_online():
        _pending_notifications.append(payload)
        return False
    return _enqueue_write(_push_notification_now, payload)


# What the write worker keeps (instead of dropping) if the internet is
# lost while a job is still waiting in the queue.
_OFFLINE_STASH = {
    _safe_update_now: _stash_state,
    _push_notification_now: _pending_notifications.append,
}


def _update_actuator_state_now(actuator_name, state):
    try:
        db.reference(f"/actuators/{actuator_name}").set(state)
    except Exception as e:
        print(f"Error updating {actuator_name}: {e}")
        _recheck_after_failure()


def update_actuator_state(actuator_name, state):
    _enqueue_write(_update_actuator_state_now, actuator_name, state)


def _update_feed_alert_state_now(is_low):
    try:
        db.reference("/sensor_data/feed_low_active").set(is_low)
    except Exception as e:
        print(f"Error updating feed_low_active: {e}")


def update_feed_alert_state(is_low):
    _enqueue_write(_update_feed_alert_state_now, is_low)


def push_sensor_data(temperature, humidity, feed_weight_grams, water_level, feed_percent=None, extra_updates=None):
    if not is_online():
        return
    try:
        update_data = {
            "sensor_data/temperature": temperature,
            "sensor_data/humidity": humidity,
            "sensor_data/feed_level": feed_weight_grams,
            "sensor_data/water_level": water_level.upper(),
            "sensor_data/system_online": True,
            "sensor_data/last_updated": int(time.time() * 1000),
        }
        if feed_percent is not None:
            update_data["sensor_data/feed_percent"] = feed_percent
        if extra_updates:
            update_data.update(extra_updates)

        db.reference("/").update(update_data)   # ONE round trip, root-level multi-path update
    except Exception as e:
        print(f"Error pushing sensor data: {type(e).__name__}")
        _recheck_after_failure()    # only go offline if the internet is really gone


def set_feed_max(capacity_grams):
    if not is_online():
        return
    try:
        db.reference("/sensor_data/feed_max").set(capacity_grams)
        print(f"Feed max set to {capacity_grams}g")
    except Exception as e:
        print(f"Error setting feed_max: {e}")


def update_relay_state(relay_name, is_on):
    if not is_online():
        return
    try:
        db.reference(f"/sensor_data/{relay_name}").set(is_on)
    except Exception as e:
        print(f"Error updating {relay_name}: {e}")


def update_servo_state(servo_name, is_active):
    if not is_online():
        return
    try:
        db.reference(f"/sensor_data/{servo_name}").set(is_active)
    except Exception as e:
        print(f"Error updating {servo_name}: {e}")


def set_last_feed_time():
    if not is_online():
        return
    try:
        db.reference("/sensor_data/last_feed_time").set(int(time.time() * 1000))
    except Exception as e:
        print(f"Error updating last_feed_time: {e}")


# ---------------- Readers (raise while offline so main.py uses its file cache) ----------------
def get_thresholds():
    if not is_online():
        raise ConnectionError("offline")
    try:
        thresholds = db.reference("/thresholds").get()
        return thresholds if thresholds else {}
    except Exception as e:
        print(f"Error reading thresholds: {type(e).__name__}")
        raise


def get_schedules():
    if not is_online():
        raise ConnectionError("offline")
    try:
        schedules = db.reference("/schedules").get()
        return schedules if schedules else {}
    except Exception as e:
        print(f"Error reading schedules: {type(e).__name__}")
        raise


# ---------------- Logs ----------------
def write_log(entry):
    global _writes_since_trim
    if not is_online():
        _pending_logs.append(entry)     # keep (max 200) and send when internet returns
        return

    try:
        db.reference("logs").push(entry)
    except Exception as e:
        print(f"Error writing log: {type(e).__name__}")
        _pending_logs.append(entry)
        _recheck_after_failure()
        return

    _writes_since_trim += 1
    if _writes_since_trim >= TRIM_LOGS_EVERY:
        _writes_since_trim = 0
        try:
            trim_logs()
        except Exception as e:
            print(f"Error trimming logs: {e}")


def trim_logs(limit=200):
    logs = db.reference("logs").get()

    if not logs:
        return

    if len(logs) <= limit:
        return

    # Skip any malformed entries instead of crashing the whole sort.
    valid_entries = [
        (key, val) for key, val in logs.items()
        if isinstance(val, dict) and "timestamp" in val
    ]

    ordered = sorted(valid_entries, key=lambda x: x[1]["timestamp"])

    for key, _ in ordered[:-limit]:
        db.reference(f"logs/{key}").delete()


# ---------------- Push notifications ----------------
def _send_alert_push_now(title, body):
    """Sends a push notification to every registered device token."""
    try:
        tokens_snapshot = db.reference("/device_tokens").get()
        if not tokens_snapshot:
            print("No registered device tokens - skipping push")
            return

        tokens = list(tokens_snapshot.keys())

        message = messaging.MulticastMessage(
            notification=messaging.Notification(title=title, body=body),
            tokens=tokens,
        )
        response = messaging.send_each_for_multicast(message)
        print(f"Push sent: {response.success_count} succeeded, {response.failure_count} failed")

        # Remove dead tokens (e.g. app uninstalled)
        if response.failure_count > 0:
            for idx, result in enumerate(response.responses):
                if not result.success:
                    dead_token = tokens[idx]
                    db.reference(f"/device_tokens/{dead_token}").delete()
                    print(f"Removed dead token: {dead_token[:20]}...")

    except Exception as e:
        print(f"Error sending push notification: {type(e).__name__}")


def send_alert_push(title, body):
    _enqueue_write(_send_alert_push_now, title, body)