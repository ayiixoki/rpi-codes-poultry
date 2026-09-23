import time
import firebase_admin
from firebase_admin import credentials, db, messaging

def init_firebase():
    try:
        cred = credentials.Certificate("/home/pi/poultrycare/serviceAccountKey.json")
        firebase_admin.initialize_app(cred, {
            "databaseURL": "https://poultrycare-f816d-default-rtdb.asia-southeast1.firebasedatabase.app"
        })
        
        # On successful connection, set system_online to True
        db.reference('/sensor_data/system_online').set(True)
        print("? Firebase initialized successfully")
        
    except Exception as e:
        print(f"? Firebase init error: {e}")
        raise


def push_sensor_data(temperature, humidity, feed_weight_grams, water_level, feed_percent=None, extra_updates=None):
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
            update_data.update(extra_updates)  # e.g. actuator states this tick

        db.reference("/").update(update_data)   # ONE round trip, root-level multi-path update
    except Exception as e:
        print(f"Error pushing sensor data: {e}")


def set_feed_max(capacity_grams):
    
    try:
        db.reference("/sensor_data/feed_max").set(capacity_grams)
        print(f"? Feed max set to {capacity_grams}g")
    except Exception as e:
        print(f"? Error setting feed_max: {e}")


def update_relay_state(relay_name, is_on):
   
    try:
        db.reference(f"/sensor_data/{relay_name}").set(is_on)
    except Exception as e:
        print(f"? Error updating {relay_name}: {e}")


def update_servo_state(servo_name, is_active):
    
    try:
        db.reference(f"/sensor_data/{servo_name}").set(is_active)
    except Exception as e:
        print(f"? Error updating {servo_name}: {e}")


def set_last_feed_time():
   
    try:
        db.reference("/sensor_data/last_feed_time").set(int(time.time() * 1000))
    except Exception as e:
        print(f"? Error updating last_feed_time: {e}")


def get_thresholds():
    
    try:
        thresholds = db.reference("/thresholds").get()
        return thresholds if thresholds else {}
    except Exception as e:
        print(f"? Error reading thresholds: {e}")
        return {}


def get_schedules():
    
    try:
        schedules = db.reference("/schedules").get()
        return schedules if schedules else {}
    except Exception as e:
        print(f"? Error reading schedules: {e}")
        return {}

def write_log(entry):
    try:
        db.reference("logs").push(entry)
    except Exception as e:
        print(f"Error writing log: {e}")
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
        
def trim_logs(limit=200):
    logs = db.reference("logs").get()

    if not logs:
        return

    if len(logs) <= limit:
        return

    ordered = sorted(
        logs.items(),
        key=lambda x: x[1]["timestamp"]
    )

    for key, _ in ordered[:-limit]:
        db.reference(f"logs/{key}").delete()

def send_alert_push(title, body):
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

        # Clean up dead tokens (e.g. app was uninstalled) so the list
        # doesn't grow forever with tokens that will never work again.
        if response.failure_count > 0:
            for idx, result in enumerate(response.responses):
                if not result.success:
                    dead_token = tokens[idx]
                    db.reference(f"/device_tokens/{dead_token}").delete()
                    print(f"Removed dead token: {dead_token[:20]}...")

    except Exception as e:
        print(f"Error sending push notification: {e}")

def update_actuator_state(actuator_name, state):
    try:
        db.reference(f"/actuators/{actuator_name}").set(state)
    except Exception as e:
        print(f"Error updating {actuator_name}: {e}")
