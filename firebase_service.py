import time
import socket
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
CONNECTIVITY_HOST = ("oauth2.googleapis.com", 443)
CONNECTIVITY_CHECK_INTERVAL = 5     # seconds between checks

_online = None          # None = not checked yet, True/False afterwards
_monitor_started = False
_state_lock = threading.Lock()
_pending_logs = deque(maxlen=200)   # logs made while offline, sent when back online


def is_online():
    return bool(_online)


def _check_internet(timeout=3):
    try:
        socket.create_connection(CONNECTIVITY_HOST, timeout=timeout).close()
        return True
    except OSError:     # includes DNS failure and timeouts
        return False


def _set_online(state):
    global _online
    with _state_lock:
        previous = _online
        if state == previous:
            return
        _online = state
    if previous is None:
        return          # first check at startup - nothing to announce or flush
    if state:
        print("Internet back - syncing with Firebase")
        threading.Thread(target=_flush_pending_logs, daemon=True).start()
    else:
        print("Internet lost - running offline")


def _monitor_loop():
    while True:
        time.sleep(CONNECTIVITY_CHECK_INTERVAL)
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


# ---------------- Init ----------------
def init_firebase():
    global _monitor_started

    # Local setup only - needs NO internet. If this fails it is a real
    # config problem (bad key path / URL), so let it raise.
    if not firebase_admin._apps:
        cred = credentials.Certificate(SERVICE_KEY_PATH)
        firebase_admin.initialize_app(cred, {"databaseURL": DATABASE_URL})

    _set_online(_check_internet())
    if not _monitor_started:
        threading.Thread(target=_monitor_loop, daemon=True).start()
        _monitor_started = True

    if not is_online():
        print("No internet at startup - running in offline mode")
        return

    try:
        db.reference('/sensor_data/system_online').set(True)
        print("Firebase initialized successfully")
    except Exception as e:
        print(f"Firebase reachable but system_online write failed: {type(e).__name__}")


# ---------------- Writers (silently skipped while offline) ----------------
def safe_update(path, data):
    """db.reference(path).update(data) that never blocks or raises offline."""
    if not is_online():
        return False
    try:
        db.reference(path).update(data)
        return True
    except Exception as e:
        print(f"Firebase update failed ({path}): {type(e).__name__}")
        return False


def push_notification(payload):
    if not is_online():
        return False
    try:
        db.reference("/notifications").push(payload)
        return True
    except Exception as e:
        print(f"Notification push failed: {type(e).__name__}")
        return False


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
        _set_online(False)      # stop further attempts until the monitor sees internet again


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


def update_actuator_state(actuator_name, state):
    if not is_online():
        return
    try:
        db.reference(f"/actuators/{actuator_name}").set(state)
    except Exception as e:
        print(f"Error updating {actuator_name}: {e}")


def update_feed_alert_state(is_low):
    if not is_online():
        return
    try:
        db.reference("/sensor_data/feed_low_active").set(is_low)
    except Exception as e:
        print(f"Error updating feed_low_active: {e}")


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
    if not is_online():
        _pending_logs.append(entry)     # keep (max 200) and send when internet returns
        return

    try:
        db.reference("logs").push(entry)
    except Exception as e:
        print(f"Error writing log: {type(e).__name__}")
        _pending_logs.append(entry)
        return  # don't attempt trim if the write itself failed

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
def send_alert_push(title, body):
    """Sends a push notification to every registered device token."""
    if not is_online():
        return
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