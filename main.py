import signal
import sys
import time
import json
import os
import statistics
import threading
from datetime import datetime, timedelta
import RPi.GPIO as GPIO
from collections import deque

from firebase_service import (
    init_firebase, push_sensor_data, push_sensor_data_async, get_thresholds,
    get_schedules, update_actuator_state, write_log, send_alert_push,
    update_feed_alert_state, is_online, safe_update, push_notification,
    get_online_epoch,
)
from sensors.dht22 import read_dht22
from sensors.load_cell import setup_hx711, read_grams, update_stable_grams, reset_stable_grams, get_last_raw
from sensors.ultrasonic import setup_ultrasonic, read_feed_bin_percent, get_last_distance
from sensors.float_sensor import setup_float_sensor, read_water_level, read_container_level
from actuators.relay_control import (
    setup_relay, turn_on_heating_lamp, turn_off_heating_lamp,
    turn_on_exhaust_fan, turn_off_exhaust_fan, cleanup
)
from actuators.servo_control import (
    setup_servos, open_feed, close_feed,
    open_water, close_water, cleanup_servos
)
from utils.logger import log_actuator, log_feed, log_water, log_alert, log_system
import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def save_json_atomic(path, data):
    # write to a temp file first, so a power cut can't leave a half-written file
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


import firebase_admin
from firebase_admin import db

# Global State
is_dispensing_feed = False
is_dispensing_water = False
heating_lamp_on = False
exhaust_fan_on = False
is_dispensing = False


last_feeding_progress = {
    "percent": None,
    "grams": None,
    "target_grams": None,
    "triggered_by": None,
    "timestamp": None
}

latest_feed_weight = None
latest_water_level = None

# Component health tracking (reported to the LCD "System Check" card)
last_dht_ok_time = None
last_load_cell_ok_time = None
water_refill_failed = False
STARTUP_TIME = time.time()
NO_READING_LIMIT = getattr(config, "SENSOR_NO_READING_SECONDS",
                           {"dht22": 120, "load_cell": 60, "feed_bin": 120})
STUCK_LIMIT = getattr(config, "SENSOR_STUCK_SECONDS",
                      {"dht22": 2 * 3600, "load_cell": 600, "feed_bin": 6 * 3600})
value_last = {}          # key -> last value seen
value_changed_at = {}    # key -> when the value last changed
fault_reason = {}        # key -> "no_reading" | "stuck" | None

water_refill_times = deque(maxlen=20)

# Component fault alerts (app notification + push), one per fault, cleared on recovery.
# water_refill is not here: dispense_water() already sends its own "Refill Failed" alert.
COMPONENT_NAMES = {
    "dht22": "Temp/Humidity Sensor (DHT22)",
    "load_cell": "Feed Scale (Load Cell)",
    "feed_bin": "Feed Container Sensor (Ultrasonic)",
}
component_fault_active = {"dht22": False, "load_cell": False, "feed_bin": False}
component_bad_since = {"dht22": None, "load_cell": None, "feed_bin": None}
component_ok_since = {"dht22": None, "load_cell": None, "feed_bin": None}

SCHEDULE_CACHE_FILE = os.path.join(BASE_DIR, "schedule_cache.json")
THRESHOLD_CACHE_FILE = os.path.join(BASE_DIR, "threshold_cache.json")
ALERT_STATE_FILE = os.path.join(BASE_DIR, "alert_state_cache.json")
DISPENSE_RATE_CACHE_FILE = os.path.join(BASE_DIR, "dispense_rate_cache.json")

THRESHOLD_REFRESH_INTERVAL = 10
threshold_refresh_loops = THRESHOLD_REFRESH_INTERVAL // config.FAST_READ_INTERVAL

SCHEDULE_REFRESH_INTERVAL = 1
schedule_refresh_loops = SCHEDULE_REFRESH_INTERVAL // config.FAST_READ_INTERVAL

cached_thresholds = None
cached_schedules = None

fired_schedules_this_minute = set()
last_checked_minute = None

# Alert active flags
feed_low_active = False
temp_high_active = False
temp_low_active = False
hum_high_active = False
container_empty_active = False

# Feed container (ultrasonic sensor on top of the container)
feed_bin_low_active = False      # True for both "low" and "empty"
feed_bin_state = "ok"            # "ok" | "low" | "empty"
feed_bin_pending = 0             # consecutive readings that disagree with the current state
latest_feed_bin_percent = None
last_bin_ok_time = None

# Automatic water refill (trough float sensor)
water_refill_cooldown_until = 0
water_lock = threading.Lock()

# Alert debounce counters (consecutive confirming / clearing readings).
# Feed advances every fast loop (real ~1/sec reading). Temp/humidity only
# advance when a genuinely new DHT22 sample was taken (see new_slow_reading
# in main()), otherwise 5 "readings" would all just be the same cached
# value repeated within one second and debounce would be meaningless.
feed_low_pending = 0
feed_clear_pending = 0
temp_low_pending = 0
temp_low_clear_pending = 0
temp_high_pending = 0
temp_high_clear_pending = 0
hum_high_pending = 0
hum_high_clear_pending = 0

exhaust_fan_last_on_time = None
shutdown_event = threading.Event()


def get_feed_capacity():
    return (cached_thresholds or {}).get("feedCapacityGrams") or config.FEED_CAPACITY_GRAMS


# Sensor health (shown on the LCD "System Check" card)
def note_reading(key, value):
    """Call with every real reading. Remembers when the value last changed."""
    if value_last.get(key) != value:
        value_last[key] = value
        value_changed_at[key] = time.time()


def get_sensor_health():
    """True = working. A part is faulty only if it gave NO value for too long,
    or kept giving the exact same value for a very long time (stuck)."""
    now = time.time()
    in_grace = (now - STARTUP_TIME) < 60          # don't flag faults right at boot
    last_ok = {"dht22": last_dht_ok_time,
               "load_cell": last_load_cell_ok_time,
               "feed_bin": last_bin_ok_time}
    health = {}
    for key, ok_time in last_ok.items():
        reason = None
        if key == "load_cell" and is_dispensing_feed:
            ok = True                                 # the dispenser owns the scale
        elif ok_time is None:
            ok = in_grace
            reason = None if ok else "no_reading"
        elif now - ok_time > NO_READING_LIMIT[key]:
            ok, reason = False, "no_reading"
        else:
            changed = value_changed_at.get(key)
            limit = STUCK_LIMIT.get(key)
            if limit and changed and now - changed > limit:
                ok, reason = False, "stuck"
            else:
                ok = True
        health[key] = ok
        fault_reason[key] = reason
    health["water_refill"] = not water_refill_failed
    return health


def check_component_health():
    """Same checks the LCD "System Check" card uses. Notifies the app once when a
    component goes bad, and once when it recovers. Returns the health dict so the
    app can show it too."""
    health = get_sensor_health()
    now = time.time()
    fault_after = getattr(config, "COMPONENT_FAULT_CONFIRM_SECONDS", 10)
    recover_after = getattr(config, "COMPONENT_RECOVER_CONFIRM_SECONDS", 30)
    for key, label in COMPONENT_NAMES.items():
        raw_ok = health.get(key, True)
        # Debounce: a fault must last `fault_after` seconds before alerting, and a
        # recovery must last `recover_after` seconds, so a flaky sensor can't spam.
        if raw_ok:
            component_bad_since[key] = None
            if component_ok_since[key] is None:
                component_ok_since[key] = now
        else:
            component_ok_since[key] = None
            if component_bad_since[key] is None:
                component_bad_since[key] = now
        bad_for = (now - component_bad_since[key]) if component_bad_since[key] else 0
        ok_for = (now - component_ok_since[key]) if component_ok_since[key] else 0

        if bad_for >= fault_after and not component_fault_active[key]:
            component_fault_active[key] = True
            stuck = fault_reason.get(key) == "stuck"
            detail = ("The reading has not changed for a long time - it may be stuck or broken."
                      if stuck else "No readings are coming in - check the cable/connection.")
            log_alert(f"{label} Problem", detail)
            try:
                push_notification({
                    "type": "componentFault",
                    "component": key,
                    "name": label,
                    "reason": "stuck" if stuck else "no_reading",
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                send_alert_push(f"{label} Problem", detail)
            except Exception as e:
                print(f"Component fault notification error: {e}")
            print(f"Component fault: {label} ({'stuck' if stuck else 'no reading'})")
        elif ok_for >= recover_after and component_fault_active[key]:
            component_fault_active[key] = False
            log_alert(f"{label} Recovered", "Readings are back to normal")
            try:
                push_notification({
                    "type": "componentRecovered",
                    "component": key,
                    "name": label,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
            except Exception as e:
                print(f"Component recovery notification error: {e}")
            print(f"Component recovered: {label}")
    # The app's alert cards use this debounced state, so they don't flicker either.
    result = dict(health)
    for key in COMPONENT_NAMES:
        result[key] = not component_fault_active[key]
    return result


#  Threshold Logic
def get_active_thresholds():
    if not is_online() and cached_thresholds:
        return cached_thresholds
    # Try Firebase (live, most up-to-date)
    try:
        thresholds = get_thresholds()
        if thresholds:
            # Normalize feedLow / feedlow key mismatch from the app
            if "feedlow" in thresholds:
                thresholds["feedLow"] = thresholds["feedlow"]
            try:
                save_json_atomic(THRESHOLD_CACHE_FILE, thresholds)
            except Exception as e:
                print(f"Threshold cache write failed: {e}")
            return thresholds
    except Exception as e:
        print(f"Threshold fetch failed, checking cache: {e}")

    # Firebase failed - try the last-known values saved from the app
    try:
        with open(THRESHOLD_CACHE_FILE, "r") as f:
            cached = json.load(f)
            print("Using cached thresholds (last known from app)")
            return cached
    except Exception:
        pass

    # No cache exists yet - hardcoded fallback
    print("No cached thresholds available, using hardcoded defaults")
    return {
        "tempMin": config.DEFAULT_TEMP_MIN,
        "tempMax": config.DEFAULT_TEMP_MAX,
        "humMin": config.DEFAULT_HUM_MIN,
        "humMax": config.DEFAULT_HUM_MAX,
        "feedLow": config.DEFAULT_FEED_LOW
    }


#  Environmental Control
def control_environment(temperature, humidity, thresholds):
    global heating_lamp_on, exhaust_fan_on, exhaust_fan_last_on_time

    if temperature is None:
        return

    temp_min = thresholds.get("tempMin", config.DEFAULT_TEMP_MIN)
    temp_max = thresholds.get("tempMax", config.DEFAULT_TEMP_MAX)
    hum_max = thresholds.get("humMax", config.DEFAULT_HUM_MAX)

    # -----------------------------
    # Heating Lamp (with hysteresis)
    # -----------------------------
    if heating_lamp_on:
        need_heat = temperature < (temp_min + config.TEMP_HYSTERESIS)
    else:
        need_heat = temperature < temp_min

    # -----------------------------
    # Exhaust Fan (with hysteresis)
    # Fully independent of the heating lamp - humidity above
    # hum_max always opens the fan, even while the lamp is on.
    # -----------------------------
    if exhaust_fan_on:
        need_exhaust = (
            temperature > (temp_max - config.TEMP_HYSTERESIS)
            or (humidity is not None and humidity > (hum_max - config.HUM_HYSTERESIS))
        )
    else:
        need_exhaust = (
            temperature > temp_max
            or (humidity is not None and humidity > hum_max)
        )

    # Don't let the fan shut off again the instant conditions dip -
    # give it a minimum run time to actually vent moisture/heat out.
    if exhaust_fan_on and not need_exhaust:
        elapsed = time.time() - (exhaust_fan_last_on_time or 0)
        if elapsed < config.EXHAUST_MIN_RUN_SECONDS:
            need_exhaust = True

    # =========================================
    # Heating Lamp
    # =========================================
    if need_heat and not heating_lamp_on:

        turn_on_heating_lamp()
        heating_lamp_on = True

        update_actuator_state("heatingLamp", True)

        log_actuator(
            "Heating Lamp",
            "ON",
            f"Temperature dropped to {temperature:.1f}°C (below {temp_min}°C)"
        )

        print(f" Heating Lamp ON ({temperature:.1f}°C)")

    elif not need_heat and heating_lamp_on:

        turn_off_heating_lamp()
        heating_lamp_on = False

        update_actuator_state("heatingLamp", False)

        log_actuator(
            "Heating Lamp",
            "OFF",
            f"Temperature recovered to {temperature:.1f}°C"
        )

        print(f" Heating Lamp OFF ({temperature:.1f}°C)")

    # =========================================
    # Exhaust Fan
    # =========================================
    if need_exhaust and not exhaust_fan_on:

        turn_on_exhaust_fan()
        exhaust_fan_on = True
        exhaust_fan_last_on_time = time.time()

        update_actuator_state("exhaustFan", True)

        reason = (
            f"Temperature reached {temperature:.1f}°C (above {temp_max}°C)"
            if temperature > temp_max
            else f"Humidity reached {humidity:.1f}% (above {hum_max}%)"
        )

        log_actuator("Exhaust Fan", "ON", reason)
        print(f"Exhaust Fan ON ({temperature:.1f}°C, {humidity}%)")

    elif not need_exhaust and exhaust_fan_on:

        turn_off_exhaust_fan()
        exhaust_fan_on = False

        update_actuator_state("exhaustFan", False)

        reason = (
            f"Temperature back to {temperature:.1f}°C (below {temp_max}°C)"
            if temperature <= temp_max and (humidity is None or humidity <= hum_max)
            else (
                f"Temperature still high but humidity dropped to {humidity:.1f}% (below {hum_max}%)"
                if temperature > temp_max
                else f"Humidity dropped to {humidity:.1f}% (below {hum_max}%)"
            )
        )

        log_actuator("Exhaust Fan", "OFF", reason)
        print(f"Exhaust Fan OFF ({temperature:.1f}°C, {humidity}%)")


#  Dispense rate memory (feeds the bulk phase estimate)
def _load_avg_dispense_rate():
    """Grams/second the feeder actually delivers, learned from past
    dispenses. Falls back to config.GRAMS_PER_SECOND until we have data."""
    try:
        with open(DISPENSE_RATE_CACHE_FILE, "r") as f:
            data = json.load(f)
        rate = data.get("avg_rate_gps")
        if rate and rate > 0:
            return rate
    except Exception:
        pass
    return getattr(config, "GRAMS_PER_SECOND", 5)


def _save_avg_dispense_rate(new_rate):
    """Blend the newly measured rate into the persisted average so one
    noisy dispense doesn't swing the bulk-phase estimate wildly, but it
    still converges toward the real rate after a handful of feedings."""
    try:
        old_rate = _load_avg_dispense_rate()
        blended = round((old_rate * 0.7) + (new_rate * 0.3), 2)
        with open(DISPENSE_RATE_CACHE_FILE, "w") as f:
            json.dump({
                "avg_rate_gps": blended,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }, f)
    except Exception as e:
        print(f"Dispense rate cache write failed: {e}")


def _read_settled_grams(capacity, samples_sets=3, delay=0.15):
    """A few spaced weight readings, median-filtered. Reduces (doesn't
    eliminate - a bird standing there the whole time will still fool it)
    the chance a single instant reading is thrown off by a chicken on
    or near the plate."""
    readings = []
    for _ in range(samples_sets):
        val = read_grams(samples=8, trust_reading=True, capacity=capacity)
        if val is not None:
            readings.append(val)
        time.sleep(delay)
    if not readings:
        return None
    return round(statistics.median(readings), 1)


# Feed Dispensing
def dispense_feed(target_grams, triggered_by="schedule", schedule_key=None,
                  add_mode=False, configured_percent=None):
    """add_mode=True: target_grams = grams to ADD (manual/LCD/schedules).
       add_mode=False: target_grams = absolute plate target.
       configured_percent: the % the user set, this is what gets logged."""
    global is_dispensing_feed, last_feeding_progress, latest_feed_weight
    global feed_low_active, feed_clear_pending
    if is_dispensing_feed:
        return

    is_dispensing_feed = True
    update_actuator_state("feedServo", True)
    try:
        capacity = get_feed_capacity()
        # Measure with a ceiling far above hopper capacity so the FULL configured
        # amount can be weighed even when the plate isn't empty.
        read_cap = getattr(config, "LOAD_CELL_MAX_GRAMS", 5000)

        start_weight = _read_settled_grams(read_cap, samples_sets=2)
        if start_weight is None:
            start_weight = latest_feed_weight if latest_feed_weight is not None else 0
        latest_feed_weight = start_weight

        if add_mode:
            add_grams = target_grams
            target_grams = start_weight + add_grams
            if configured_percent is None:
                configured_percent = (add_grams / capacity) * 100 if capacity else 0

        requested_target = target_grams
        target_grams = min(target_grams, capacity)     # never overfill the plate
        capped = target_grams < requested_target
        if capped:
            print(f"Plate can only take {target_grams - start_weight:.0f}g more "
                  f"(capacity {capacity}g) - adding less than requested")

        if configured_percent is None:
            configured_percent = (target_grams / capacity) * 100 if capacity else 0
        configured_percent = round(configured_percent)

        if start_weight >= target_grams:
            print(f"Plate already at {start_weight}g - skipping")
            log_system(f"Feed skipped: plate already full ({start_weight:.0f} g of {capacity:.0f} g)")
            return

        amount_requested = target_grams - start_weight

        min_rate = getattr(config, "DISPENSE_MIN_RATE_GPS", 0.3)
        settle_seconds = getattr(config, "DISPENSE_SETTLE_SECONDS", 0.6)
        settle_wait = max(config.SERVO_CLOSE_LATENCY_SECONDS, settle_seconds)
        min_pulse = getattr(config, "DISPENSE_MIN_PULSE_SECONDS", 0.08)
        tolerance = config.DISPENSE_OVERSHOOT_BUFFER_GRAMS
        stop_early_at = target_grams - tolerance

        avg_rate = max(min_rate, _load_avg_dispense_rate())
        est_seconds = amount_requested / avg_rate
        timeout = time.time() + max(config.FEED_DISPENSE_TIMEOUT_SECONDS, est_seconds * 3 + 15)

        print(f"Dispensing feed: {start_weight}g -> {target_grams}g "
              f"(+{amount_requested:.1f}g, configured {configured_percent}%)")

        current_weight = start_weight
        measured_rate = None
        total_open_seconds = 0.0

        # ---------- Bulk phase ----------
        bulk_fraction = getattr(config, "DISPENSE_BULK_FRACTION", 0.2)
        bulk_max_seconds = getattr(config, "DISPENSE_BULK_MAX_SECONDS", 5)
        bulk_seconds = min(bulk_max_seconds, (amount_requested * bulk_fraction) / avg_rate)

        bulk_min = getattr(config, "DISPENSE_BULK_MIN_AMOUNT_G", 60)
        if amount_requested >= bulk_min and bulk_seconds > 0.1:
            t0 = time.time()
            open_feed()
            time.sleep(bulk_seconds)
            close_feed()
            actual = time.time() - t0
            total_open_seconds += actual
            time.sleep(settle_wait)

            settled = _read_settled_grams(read_cap)
            if settled is not None:
                current_weight = settled
                latest_feed_weight = current_weight
                got = max(0.0, current_weight - start_weight)
                if actual > 0 and got > 0:
                    measured_rate = got / actual
                print(f"  Bulk: +{got:.1f}g in {actual:.2f}s")

        if measured_rate is None:
            measured_rate = avg_rate

        # ---------- Fine-tune phase ----------
        min_pulse_gain = None
        while time.time() < timeout and current_weight < stop_early_at and not shutdown_event.is_set():
            remaining = stop_early_at - current_weight
            correction = 0.15 if remaining <= 3 else (0.2 if remaining <= 10 else 0.25)
            pulse_seconds = min(0.5, max(min_pulse, (remaining / measured_rate) * correction))

            before = current_weight
            t0 = time.time()
            open_feed()
            time.sleep(pulse_seconds)
            close_feed()
            actual = time.time() - t0
            total_open_seconds += actual
            time.sleep(settle_wait)

            reading = read_grams(samples=8, trust_reading=True, capacity=read_cap)
            current_weight = reading if reading is not None else before
            latest_feed_weight = current_weight

            gained = current_weight - before
            if gained > 0:
                min_pulse_gain = gained if min_pulse_gain is None else min(min_pulse_gain, gained)
                # only trust the rate from pulses long enough for grams ~ rate x time
                if actual >= 0.3:
                    measured_rate = gained / actual
            print(f"  Pulse {pulse_seconds:.2f}s -> +{gained:.1f}g, plate {current_weight}g")

            # if even the smallest pulse we've seen would overshoot, stop here
            if min_pulse_gain is not None and (stop_early_at - current_weight) < min_pulse_gain * 0.25:
                break

        if time.time() >= timeout and current_weight < stop_early_at:
            print("Feed fail-safe: target not reached before timeout. Check hopper, servo, load cell.")

        # ---------- Final settled reading ----------
        time.sleep(settle_wait)
        final_weight = _read_settled_grams(read_cap)
        if final_weight is None:
            final_weight = current_weight
        reset_stable_grams(final_weight)
        latest_feed_weight = final_weight

        dispensed = max(0.0, round(final_weight - start_weight, 1))
        accuracy_percent = round((dispensed / amount_requested) * 100) if amount_requested else 0

        if total_open_seconds > 0 and dispensed > 0:
            _save_avg_dispense_rate(dispensed / total_open_seconds)

        log_feed(dispensed, triggered_by, percent=configured_percent,
                 note="plate full" if capped else None)

        last_feeding_progress = {
            "percent": configured_percent,
            "accuracy_percent": accuracy_percent,
            "grams": dispensed,
            "target_grams": amount_requested,
            "triggered_by": triggered_by,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        # Feed Restocked, using the settled post-dispense weight
        if feed_low_active:
            final_percent = (final_weight / capacity) * 100 if capacity else 0
            low_thr = (cached_thresholds or {}).get("feedLow", config.DEFAULT_FEED_LOW)
            hyst = getattr(config, "FEED_HYSTERESIS_PERCENT", 5)
            if final_percent >= (low_thr + hyst):
                feed_low_active = False
                feed_clear_pending = 0
                log_alert("Feed Restocked", f"{final_weight:.0f} g available ({final_percent:.0f}%)")
                update_feed_alert_state(False)
                save_alert_state()

        try:
            push_notification({
                "type": "feedDispensed",
                "amount": dispensed,
                "percent": configured_percent,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            safe_update('/sensor_data', {
                'last_feed_time': int(time.time() * 1000),
                'feeding_progress': last_feeding_progress
            })
        except Exception as e:
            print(f"Firebase write failed after dispense (feed already dispensed OK): {e}")

        print(f"Feed dispensed: {dispensed}g ({configured_percent}% configured, "
              f"{accuracy_percent}% of request), plate now {final_weight}g")

    except Exception as e:
        print(f"Feed dispense error: {e}")
        try:
            close_feed()
        except Exception as close_err:
            print(f"close_feed() also failed: {close_err}")
    finally:
        update_actuator_state("feedServo", False)
        is_dispensing_feed = False


#  Automatic Water Refill (trough float sensor)
def container_is_empty(confirm=3, gap=0.1):
    """Container float must read empty 3 times in a row (~0.2 s) so sloshing
    water can't fake it."""
    for i in range(confirm):
        if read_container_level() != "empty":
            return False
        if i < confirm - 1:
            time.sleep(gap)
    return True


def dispense_water(triggered_by="auto"):
    """Opens the water servo until the drinker float reads normal.
    Skipped when the container is empty (nothing to dispense). The valve is
    always closed in the finally block, and rest/pause intervals keep it safe."""
    global is_dispensing_water, water_refill_cooldown_until, water_refill_failed
    if not water_lock.acquire(blocking=False):
        return                      # a refill is already running
    try:
        if read_water_level() != "low" or container_is_empty():
            return

        is_dispensing_water = True
        update_actuator_state("waterServo", True)
        result = "error"
        try:
            print("Auto-refill: drinker low, opening water servo")
            open_water()
            timeout_seconds = config.WATER_HARD_CAP_SECONDS
            timeout = time.time() + timeout_seconds
            reached_normal = False
            container_ran_out = False
            normal_confirm_count = 0

            while time.time() < timeout and not shutdown_event.is_set():
                if container_is_empty():
                    container_ran_out = True
                    break
                if read_water_level() == "normal":
                    normal_confirm_count += 1
                    if normal_confirm_count >= config.WATER_NORMAL_CONFIRM_READS:
                        print("Water reached normal (confirmed). Topping up...")
                        shutdown_event.wait(config.WATER_EXTRA_FILL_TIME)
                        reached_normal = True
                        break
                else:
                    normal_confirm_count = 0
                shutdown_event.wait(0.3)

            if reached_normal:
                result = "dispensed"
            elif container_ran_out:
                result = "container_empty"
            elif shutdown_event.is_set():
                result = "interrupted"
            else:
                result = "timed_out"
                print(f"Water refill timed out after {timeout_seconds}s - check float sensor/servo")
        except Exception as e:
            print(f"Water dispense error: {e}")
        finally:
            # the valve is ALWAYS closed here, no matter how we got out
            try:
                close_water()
            except Exception as close_err:
                print(f"close_water() failed: {close_err}")
            update_actuator_state("waterServo", False)
            is_dispensing_water = False

        # Track refill health for the LCD / app
        if result == "dispensed":
            water_refill_failed = False
        elif result == "timed_out":
            water_refill_failed = True

        if result in ("timed_out", "error"):
            water_refill_cooldown_until = time.time() + getattr(
                config, "WATER_REFILL_RETRY_COOLDOWN_SECONDS", 300)

        if result == "timed_out":
            log_alert("Water Refill Failed", "Trough still low after refill - check valve/sensor")
            try:
                push_notification({
                    "type": "waterRefillFailed",
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                send_alert_push("Water Refill Failed", "Trough still low - check valve/sensor.")
            except Exception as e:
                print(f"Refill-failed notification error: {e}")

        if result == "dispensed":
            # short rest after every refill, and a long pause if it keeps repeating
            now = time.time()
            water_refill_cooldown_until = now + getattr(config, "WATER_MIN_INTERVAL_SECONDS", 20)
            water_refill_times.append(now)
            recent = [t for t in water_refill_times if now - t < 3600]
            if len(recent) >= getattr(config, "WATER_MAX_REFILLS_PER_HOUR", 6):
                water_refill_cooldown_until = now + getattr(config, "WATER_REPEAT_PAUSE_SECONDS", 1800)
                water_refill_times.clear()
                log_alert("Water Refill Repeating",
                          "Drinker keeps going low - paused refilling. Check for a leak or a stuck float.")
                try:
                    push_notification({
                        "type": "waterRefillFailed",
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                    send_alert_push("Water Refill Paused",
                                    "The drinker keeps going low. Check for a leak or a stuck float.")
                except Exception as e:
                    print(f"Refill-repeat notification error: {e}")

            log_water(triggered_by)
            try:
                push_notification({
                    "type": "waterDispensed",
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
            except Exception as e:
                print(f"Firebase write failed after refill: {e}")
            print("Water refilled")

        try:
            safe_update("/sensor_data", {"last_water_action": {
                "result": result,
                "triggered_by": triggered_by,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }})
        except Exception as e:
            print(f"Firebase write failed after water action: {e}")
    finally:
        water_lock.release()


def water_watcher():
    """Own thread: checks the drinker float ~5x per second, so a refill starts
    within a second even when the main loop is busy. Skips are logged ONCE
    with the reason, so 'why didn't it open?' is visible in the Activity Log."""
    global water_refill_failed
    last_skip = None
    while not shutdown_event.is_set():
        try:
            if read_water_level() == "normal":
                water_refill_failed = False
                last_skip = None
            else:
                skip = None
                if container_is_empty():
                    skip = "water container is empty"
                elif time.time() < water_refill_cooldown_until:
                    skip = "waiting for the safety pause to end"
                if skip is None:
                    last_skip = None
                    dispense_water("auto")          # blocks until done, this thread is its own
                elif skip != last_skip:
                    last_skip = skip
                    log_system(f"Water refill skipped: {skip}")
        except Exception as e:
            print(f"Water watcher error: {e}")
        shutdown_event.wait(getattr(config, "WATER_CHECK_INTERVAL", 0.2))


#  Feed Dispense Sequence (water is automatic now, feed only)
def run_dispense_sequence(feed_grams=None, triggered_by="schedule",
                          schedule_key=None, add_mode=False, configured_percent=None):
    global is_dispensing
    if is_dispensing:
        print("Dispense already in progress, skipping.")
        return
    is_dispensing = True
    try:
        if feed_grams:
            dispense_feed(feed_grams, triggered_by, schedule_key, add_mode, configured_percent)
    finally:
        is_dispensing = False


#  Schedule Fetching (offline cache)
def get_schedules_cached():
    if not is_online() and cached_schedules is not None:
        return cached_schedules
    # Try Firebase (live, up-to-date)
    try:
        schedules = get_schedules()
    except Exception as e:
        # Only an actual exception counts as "Firebase failed" - fall back
        # to the last-known cache.
        print(f"Schedule fetch failed, checking cache: {e}")
        try:
            with open(SCHEDULE_CACHE_FILE, "r") as f:
                cached = json.load(f)
                print("Using cached schedules (last known from app)")
                return cached
        except Exception:
            print("No cached schedules available")
            return None

    # The call succeeded - trust it completely, even if it's empty/None.
    # An empty result here means schedules were genuinely deleted/disabled,
    # NOT that the fetch failed - so we must overwrite the cache with that
    # empty state instead of silently keeping the old one, or a deleted
    # schedule keeps reappearing on the LCD forever.
    try:
        save_json_atomic(SCHEDULE_CACHE_FILE, schedules or {})
    except Exception as e:
        print(f"Schedule cache write failed: {e}")

    return schedules


# Find Next Scheduled Feed (for LCD display)
def get_next_schedule_info(schedules):
    if not schedules:
        return None

    now = datetime.now()
    day_map = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    best = None

    for key, entry in schedules.items():
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue

        sched_time = entry.get("time")
        if not sched_time:
            continue

        days_str = entry.get("days", "Mon,Tue,Wed,Thu,Fri,Sat,Sun")
        days_list = [d.strip() for d in days_str.split(",")]

        percent = entry.get("percent")
        if percent:
            amount = round(get_feed_capacity() * percent / 100)
        else:
            amount = entry.get("amount_grams")
        if not amount:
            continue

        try:
            sched_hour, sched_min = map(int, sched_time.split(":"))
        except Exception:
            continue

        for offset in range(7):
            check_day = (now.weekday() + offset) % 7
            if day_map[check_day] not in days_list:
                continue

            candidate = now.replace(hour=sched_hour, minute=sched_min, second=0, microsecond=0)
            candidate_full = candidate + timedelta(days=offset)

            if candidate_full <= now:
                continue

            minutes_away = (candidate_full - now).total_seconds() / 60
            if best is None or minutes_away < best[0]:
                best = (minutes_away, candidate_full.strftime("%I:%M %p"), amount, candidate_full.strftime("%a"))

    if best is None:
        return None

    return {
        "time": best[1],
        "day": best[3],
        "amount_grams": best[2]
    }


#  Schedule Checker
def check_schedules_with_data(schedules):
    global fired_schedules_this_minute, last_checked_minute

    try:
        if not schedules:
            return

        now_time = datetime.now().strftime("%H:%M")
        today_day = datetime.now().strftime("%a")

        if now_time != last_checked_minute:
            fired_schedules_this_minute = set()
            last_checked_minute = now_time

        for key, entry in schedules.items():

            if not isinstance(entry, dict):
                continue

            if not entry.get("enabled", True):
                continue

            sched_time = entry.get("time")

            if sched_time != now_time:
                continue

            if key in fired_schedules_this_minute:
                continue

            days_str = entry.get("days", "Mon,Tue,Wed,Thu,Fri,Sat,Sun")
            days_list = [d.strip() for d in days_str.split(",")]

            if today_day not in days_list:
                continue

            # Prefer "percent" (of the user-configured hopper capacity,
            # recomputed live) over the frozen "amount_grams" fallback that
            # older schedules (saved before percent-based storage) still use.
            percent = entry.get("percent")
            if percent:
                amount = round(get_feed_capacity() * percent / 100)
            else:
                amount = entry.get("amount_grams")

            if not amount:
                print(f"Schedule {key} has no percent or amount set - skipping")
                fired_schedules_this_minute.add(key)
                continue

            # Schedules are feed only now: water refills automatically.
            threading.Thread(
                target=run_dispense_sequence,
                args=(amount, "schedule", key, True, percent)
            ).start()

            fired_schedules_this_minute.add(key)

    except Exception as e:
        print(f"Schedule check error: {e}")


#  Manual Dispense Listener (app feed button)
def check_manual_commands():
    if not is_online():
        return
    ref = db.reference('/sensor_data')
    try:
        data = ref.get()
    except Exception as e:
        print(f"Manual command check error (read): {e}")
        return

    if not data:
        return

    # An old app version may still raise the water button flag. Water is
    # automatic now, so just clear it so it doesn't sit "pressed" forever.
    if data.get('water_dispenser') == True:
        try:
            ref.update({'water_dispenser': False})
            print("Water button ignored - water is automatic now")
        except Exception as e:
            print(f"Could not clear water_dispenser flag: {e}")

    feed_triggered = data.get('feeder_active') == True

    target = None
    manual_percent = None
    if feed_triggered:
        try:
            manual_grams = data.get('manual_dispense_grams') or 0
            manual_percent = data.get('manual_dispense_percent')
            ref.update({'feeder_active': False, 'manual_dispense_grams': 0,
                        'manual_dispense_percent': 0})
            if manual_percent and manual_percent > 0:
                target = get_feed_capacity() * manual_percent / 100   # exact, not rounded
                print(f"Manual feed: adding {manual_percent}% = {target:.1f}g")
            elif manual_grams > 0:
                manual_percent = None
                target = manual_grams
                print(f"Manual feed: adding {manual_grams}g")
            else:
                print("Manual feed pressed with no amount set - skipping")
                feed_triggered = False
        except Exception as e:
            print(f"Manual feed trigger error: {e}")
            feed_triggered = False

    if feed_triggered:
        threading.Thread(
            target=run_dispense_sequence,
            args=(target, 'manual', None, True, manual_percent)
        ).start()


#  LCD Command Listener (local file, works fully offline)
LCD_COMMANDS_FILE = "lcd_commands.json"


def check_lcd_commands():
    try:
        with open(LCD_COMMANDS_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        return  # file doesn't exist yet or is empty - nothing to do

    feed_requested = data.get("feed_dispense", {}).get("requested", False)
    water_requested = data.get("water_dispense", {}).get("requested", False)

    if not (feed_requested or water_requested):
        return

    # Clear the command file immediately so the LCD doesn't keep trying to trigger it
    try:
        save_json_atomic(LCD_COMMANDS_FILE, {
            "feed_dispense": {"requested": False, "grams": 0},
            "water_dispense": {"requested": False}
        })
    except Exception as e:
        print(f"LCD command file reset error: {e}")

    if water_requested:
        print("Water button ignored - water is automatic now")

    target = None
    if feed_requested:
        manual_grams = data.get("feed_dispense", {}).get("grams") or 0
        if manual_grams > 0:
            target = manual_grams   # grams to ADD; dispense_feed reads the start weight itself
            print(f"Manual feed: adding {manual_grams}g")
        else:
            print("LCD feed pressed with no grams set - skipping")

    if target is not None:
        threading.Thread(
            target=run_dispense_sequence,
            args=(target, "lcd", None, True)
        ).start()


#  Manual Command Watcher (Firebase-based, fast, independent of the sensor loop)
def manual_command_watcher():
    poll_interval = getattr(config, 'MANUAL_COMMAND_POLL_INTERVAL', 0.5)
    while True:
        try:
            check_manual_commands()
        except Exception as e:
            print(f"Manual command watcher error: {e}")
        time.sleep(poll_interval)


#  LCD Command Watcher (local file, independent of Firebase latency)
def lcd_command_watcher():
    poll_interval = getattr(config, 'LCD_COMMAND_POLL_INTERVAL', 0.2)
    while True:
        try:
            check_lcd_commands()
        except Exception as e:
            print(f"LCD command watcher error: {e}")
        time.sleep(poll_interval)


#  Alert State Persistence
# Restores both the active-alert flags AND the in-progress debounce
# counters, so a script restart doesn't lose partial debounce progress or
# re-fire notifications that are already active.

def load_alert_state():
    """Restore alert state from disk on startup, so a restart doesn't
    think every ongoing problem (or in-progress debounce count) is new."""
    global feed_low_active
    global temp_high_active, temp_low_active, hum_high_active
    global container_empty_active
    global feed_low_pending, feed_clear_pending
    global temp_low_pending, temp_low_clear_pending
    global temp_high_pending, temp_high_clear_pending
    global hum_high_pending, hum_high_clear_pending
    global feed_bin_low_active, feed_bin_state

    try:
        with open(ALERT_STATE_FILE, "r") as f:
            state = json.load(f)
        feed_low_active = state.get("feed_low_active", False)
        temp_high_active = state.get("temp_high_active", False)
        temp_low_active = state.get("temp_low_active", False)
        hum_high_active = state.get("hum_high_active", False)
        container_empty_active = state.get("container_empty_active", False)
        feed_bin_state = state.get("feed_bin_state",
                                   "low" if state.get("feed_bin_low_active") else "ok")
        feed_bin_low_active = feed_bin_state != "ok"
        # Remember component faults across restarts so a restart doesn't re-notify
        component_fault_active.update({
            k: bool(v) for k, v in state.get("component_fault_active", {}).items()
            if k in component_fault_active
        })
        feed_low_pending = state.get("feed_low_pending", 0)
        feed_clear_pending = state.get("feed_clear_pending", 0)
        temp_low_pending = state.get("temp_low_pending", 0)
        temp_low_clear_pending = state.get("temp_low_clear_pending", 0)
        temp_high_pending = state.get("temp_high_pending", 0)
        temp_high_clear_pending = state.get("temp_high_clear_pending", 0)
        hum_high_pending = state.get("hum_high_pending", 0)
        hum_high_clear_pending = state.get("hum_high_clear_pending", 0)
        print(f"Restored alert state from disk: {state}")
    except Exception:
        # No cache yet (first run) - flags/counters stay at their defaults,
        # which is correct: on a truly fresh start we don't know of any
        # active problem yet.
        print("No alert state cache found - starting fresh")


def save_alert_state():
    """Call this after check_alerts() runs so an unexpected restart can
    restore exactly what was active (and how far a debounce had progressed)."""
    state = {
        "feed_low_active": feed_low_active,
        "temp_high_active": temp_high_active,
        "temp_low_active": temp_low_active,
        "hum_high_active": hum_high_active,
        "container_empty_active": container_empty_active,
        "feed_bin_low_active": feed_bin_low_active,
        "feed_bin_state": feed_bin_state,
        "component_fault_active": component_fault_active,
        "feed_low_pending": feed_low_pending,
        "feed_clear_pending": feed_clear_pending,
        "temp_low_pending": temp_low_pending,
        "temp_low_clear_pending": temp_low_clear_pending,
        "temp_high_pending": temp_high_pending,
        "temp_high_clear_pending": temp_high_clear_pending,
        "hum_high_pending": hum_high_pending,
        "hum_high_clear_pending": hum_high_clear_pending,
    }
    try:
        with open(ALERT_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"Alert state cache write failed: {e}")


def check_alerts(feed_weight, water_level, temperature, humidity, thresholds,
                 new_slow_reading=False, container_level=None):
    global feed_low_active
    global temp_high_active, temp_low_active, hum_high_active
    global container_empty_active
    global feed_low_pending, feed_clear_pending
    global temp_low_pending, temp_low_clear_pending
    global temp_high_pending, temp_high_clear_pending
    global hum_high_pending, hum_high_clear_pending

    temp_max = thresholds.get("tempMax", config.DEFAULT_TEMP_MAX)
    temp_min = thresholds.get("tempMin", config.DEFAULT_TEMP_MIN)
    hum_max = thresholds.get("humMax", config.DEFAULT_HUM_MAX)

    temp_confirm = getattr(config, "TEMP_ALERT_CONFIRM_READS", 5)
    hum_confirm = getattr(config, "HUM_ALERT_CONFIRM_READS", 5)
    feed_confirm = getattr(config, "FEED_ALERT_CONFIRM_READS", 5)

    # -------------------------
    # Temperature / Humidity - counters only advance on an actual new DHT22
    # sample (it only refreshes every SLOW_READ_INTERVAL seconds), so a
    # single noisy reading can't fake 5 "consistent readings" just by being
    # re-evaluated on repeated fast-loop iterations.
    # -------------------------
    if new_slow_reading and temperature is not None:

        # High temp
        if temperature > temp_max:
            temp_high_clear_pending = 0
            if not temp_high_active:
                temp_high_pending += 1
                if temp_high_pending >= temp_confirm:
                    temp_high_active = True
                    temp_high_pending = 0
                    log_alert("Temperature High", f"{temperature:.1f}°C (above {temp_max}°C)")
                    push_notification({
                        "type": "highTemp",
                        "value": temperature,
                        "threshold": temp_max,
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                    send_alert_push("High Temperature Alert", f"Temperature reached {temperature:.1f}°C")
        else:
            temp_high_pending = 0
            if temp_high_active and temperature <= (temp_max - config.TEMP_HYSTERESIS):
                temp_high_clear_pending += 1
                if temp_high_clear_pending >= temp_confirm:
                    temp_high_active = False
                    temp_high_clear_pending = 0
            else:
                temp_high_clear_pending = 0

        # Low temp
        if temperature < temp_min:
            temp_low_clear_pending = 0
            if not temp_low_active:
                temp_low_pending += 1
                if temp_low_pending >= temp_confirm:
                    temp_low_active = True
                    temp_low_pending = 0
                    log_alert("Temperature Low", f"{temperature:.1f}°C (below {temp_min}°C)")
                    push_notification({
                        "type": "lowTemp",
                        "value": temperature,
                        "threshold": temp_min,
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                    send_alert_push("Low Temperature Alert", f"Temperature dropped to {temperature:.1f}°C")
        else:
            temp_low_pending = 0
            if temp_low_active and temperature >= (temp_min + config.TEMP_HYSTERESIS):
                temp_low_clear_pending += 1
                if temp_low_clear_pending >= temp_confirm:
                    temp_low_active = False
                    temp_low_clear_pending = 0
            else:
                temp_low_clear_pending = 0

    if new_slow_reading and humidity is not None:
        if humidity > hum_max:
            hum_high_clear_pending = 0
            if not hum_high_active:
                hum_high_pending += 1
                if hum_high_pending >= hum_confirm:
                    hum_high_active = True
                    hum_high_pending = 0
                    log_alert("Humidity High", f"{humidity:.1f}% (above {hum_max}%), exhaust fan ON")
                    push_notification({
                        "type": "humidityHigh",
                        "value": humidity,
                        "threshold": hum_max,
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                    send_alert_push("High Humidity Alert", f"Humidity reached {humidity:.1f}% — exhaust fan ON")
        else:
            hum_high_pending = 0
            if hum_high_active and humidity <= (hum_max - config.HUM_HYSTERESIS):
                hum_high_clear_pending += 1
                if hum_high_clear_pending >= hum_confirm:
                    hum_high_active = False
                    hum_high_clear_pending = 0
            else:
                hum_high_clear_pending = 0

    feed_low_threshold = thresholds.get("feedLow", 20)  # percent

    # -------------------------
    # Feed Level - feed_weight is a real per-second reading. This branch
    # only ever SETS feed_low_active True (with the existing debounce).
    # Clearing ("Feed Restocked") is handled ONLY in dispense_feed(),
    # right after a real dispense settles - never from this background
    # loop - so raw load-cell noise (a chicken standing on/near the plate)
    # can never flap the restocked notification on its own.
    # -------------------------
    if feed_weight is not None:
        feed_percent = (feed_weight / get_feed_capacity()) * 100

        if feed_percent < feed_low_threshold:
            feed_clear_pending = 0
            if not feed_low_active:
                feed_low_pending += 1
                if feed_low_pending >= feed_confirm:
                    feed_low_active = True
                    feed_low_pending = 0
                    log_alert("Low Feed", f"{feed_weight:.0f} g remaining ({feed_percent:.0f}%)")
                    push_notification({
                        "type": "lowFeed",
                        "value": feed_weight,
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                    send_alert_push(
                        "Low Feed Alert",
                        f"Feed level is low ({feed_percent:.0f}% remaining)"
                    )
                    update_feed_alert_state(True)
                    print(f"Low Feed: {feed_weight:.0f} g ({feed_percent:.0f}%)")
        else:
            feed_low_pending = 0
            # NOTE: no clearing here anymore - see dispense_feed().

    # -------------------------
    # Water container (second float sensor) - fires immediately, no delay.
    # The trough itself needs no alert: it refills automatically, and a
    # failed refill raises its own "Water Refill Failed" alert.
    # -------------------------
    if container_level == "empty":
        if not container_empty_active:
            container_empty_active = True
            log_alert("Water Container Empty", "Container needs refill")
            push_notification({
                "type": "containerEmpty",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            send_alert_push("Water Container Empty", "The water container needs refill.")
            print("Container empty")
    elif container_level == "ok":
        if container_empty_active:
            container_empty_active = False
            log_alert("Water Container Refilled", "Container has water again")
            push_notification({
                "type": "containerRefilled",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            print("Container refilled")

    # Persist active-flag + debounce-counter state so a restart doesn't
    # re-fire alerts that are already active, or lose in-progress debounce.
    save_alert_state()


#  Feed Container Alert (ultrasonic sensor)
def _bin_target(percent, current):
    low = getattr(config, "FEED_BIN_LOW_PERCENT", 20)
    empty = getattr(config, "FEED_BIN_EMPTY_PERCENT", 5)
    hyst = getattr(config, "FEED_BIN_HYSTERESIS_PERCENT", 5)
    if percent < empty:
        return "empty"
    if current == "empty" and percent < empty + hyst:
        return "empty"
    if percent < low:
        return "low"
    if current in ("low", "empty") and percent < low + hyst:
        return "low"
    return "ok"


def check_feed_bin_alert(percent, new_reading):
    """ok / low / empty with debounce. One notification per change of state."""
    global feed_bin_state, feed_bin_low_active, feed_bin_pending
    if percent is None or not new_reading:
        return

    confirm = getattr(config, "FEED_BIN_CONFIRM_READS", 3)
    target = _bin_target(percent, feed_bin_state)
    if target == feed_bin_state:
        feed_bin_pending = 0
        return
    feed_bin_pending += 1
    if feed_bin_pending < confirm:
        return

    previous = feed_bin_state
    feed_bin_state = target
    feed_bin_low_active = target != "ok"
    feed_bin_pending = 0
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        if target == "empty":
            log_alert("Feed Container Empty", f"{percent:.0f}% left - refill now")
            push_notification({"type": "feedBinEmpty", "value": percent, "timestamp": stamp})
            send_alert_push("Feed Container Empty",
                            "The feed container is empty. Please refill it now.")
        elif target == "low" and previous == "ok":
            log_alert("Feed Container Low", f"{percent:.0f}% left in the container")
            push_notification({"type": "feedBinLow", "value": percent, "timestamp": stamp})
            send_alert_push("Feed Container Low",
                            f"Only {percent:.0f}% feed left in the container. Please refill.")
        elif target == "ok":
            log_alert("Feed Container Refilled", f"{percent:.0f}% in the container")
            push_notification({"type": "feedBinRefilled", "value": percent, "timestamp": stamp})
    except Exception as e:
        print(f"Feed container notification error: {e}")
    print(f"Feed container: {previous} -> {target} ({percent:.0f}%)")


# Shared State Writer (for LCD dashboard)
SHARED_STATE_FILE = "shared_state.json"


def write_shared_state(temperature, humidity, feed_weight, water_level, thresholds,
                       next_schedule=None, system_online=True, container_level=None,
                       sensor_health=None):
    feed_percent = (feed_weight / get_feed_capacity() * 100) if feed_weight is not None else None
    try:
        internet_online = bool(is_online())
    except Exception:
        internet_online = False

    state = {
        "temperature": temperature,
        "humidity": humidity,
        "feed_weight": feed_weight,
        "feed_low_active": feed_low_active,
        "feed_percent": feed_percent,
        "feed_capacity_grams": get_feed_capacity(),
        "feeding_progress": last_feeding_progress,
        "water_level": water_level,
        "container_level": container_level,
        "container_empty_active": container_empty_active,
        "water_refill_failed": water_refill_failed,
        "sensor_health": sensor_health if sensor_health is not None else get_sensor_health(),
        "sensor_fault_reason": dict(fault_reason),
        "feed_bin_percent": latest_feed_bin_percent,
        "feed_bin_low_active": feed_bin_low_active,
        "feed_bin_state": feed_bin_state,
        "feed_low_threshold": thresholds.get("feedLow", 20),
        "temp_min_threshold": thresholds.get("tempMin", config.DEFAULT_TEMP_MIN),
        "temp_max_threshold": thresholds.get("tempMax", config.DEFAULT_TEMP_MAX),
        "hum_min_threshold": thresholds.get("humMin", config.DEFAULT_HUM_MIN),
        "hum_max_threshold": thresholds.get("humMax", config.DEFAULT_HUM_MAX),
        "is_dispensing_feed": is_dispensing_feed,
        "is_dispensing_water": is_dispensing_water,
        "heating_lamp_on": heating_lamp_on,
        "exhaust_fan_on": exhaust_fan_on,
        "system_online": system_online,
        "internet_online": internet_online,
        "next_schedule": next_schedule,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        # write to a temp file then swap it in, so the LCD (reading every
        # 50ms) can never catch a half-written file
        tmp = SHARED_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, SHARED_STATE_FILE)
    except Exception as e:
        print(f"Shared state write failed: {e}")


# Background Sync (thresholds + schedules, off the main loop)
def sync_worker():
    """Refreshes cached thresholds/schedules off the main thread.
    Right after the internet comes back it refreshes every second for a
    few seconds, so settings changed in the app while the Pi was offline
    reach the LCD quickly (the first Firebase call after a reconnect can fail)."""
    global cached_thresholds, cached_schedules
    last_thr = 0
    last_epoch = get_online_epoch()
    fast_sync_until = 0
    while not shutdown_event.is_set():
        try:
            epoch = get_online_epoch()
            if epoch != last_epoch:
                last_epoch = epoch
                fast_sync_until = time.time() + 10

            interval = 1 if time.time() < fast_sync_until else THRESHOLD_REFRESH_INTERVAL
            if cached_thresholds is None or time.time() - last_thr >= interval:
                cached_thresholds = get_active_thresholds()
                last_thr = time.time()
            cached_schedules = get_schedules_cached()
        except Exception as e:
            print(f"Sync worker error: {e}")
        shutdown_event.wait(0.5)


# Main Loop
def main():
    global cached_thresholds, cached_schedules, latest_feed_weight, latest_water_level
    global last_dht_ok_time, last_load_cell_ok_time
    global last_bin_ok_time, latest_feed_bin_percent

    print("Initializing PoultryCare system...")
    init_firebase()
    load_alert_state()
    update_feed_alert_state(feed_low_active)
    setup_relay()
    setup_hx711()
    setup_float_sensor()
    setup_ultrasonic()
    setup_servos()

    # Power may have been cut mid-dispense: force a known-safe state
    try:
        close_feed()
        close_water()
    except Exception as e:
        print(f"Startup servo reset failed: {e}")
    for name in ("feedServo", "waterServo", "heatingLamp", "exhaustFan"):
        update_actuator_state(name, False)

    print("All systems initialized. Starting main loop...")

    threading.Thread(target=manual_command_watcher, daemon=True).start()
    threading.Thread(target=lcd_command_watcher, daemon=True).start()

    loop_count = 0
    LOG_INTERVAL = 3600    # seconds
    SCHEDULE_INTERVAL = 60 # seconds

    log_interval_loops = LOG_INTERVAL // config.FAST_READ_INTERVAL
    schedule_interval_loops = SCHEDULE_INTERVAL // config.FAST_READ_INTERVAL
    slow_read_loops = config.SLOW_READ_INTERVAL // config.FAST_READ_INTERVAL

    # Cache the last known temp/humidity so every loop iteration still
    # has a value to work with between DHT22 samples
    last_temperature = None
    last_humidity = None

    # Load both caches once, then let the background thread keep them fresh
    cached_thresholds = get_active_thresholds()
    cached_schedules = get_schedules_cached()
    threading.Thread(target=sync_worker, daemon=True).start()
    threading.Thread(target=water_watcher, daemon=True).start()

    def cleanup_actuators():
        print("\nShutting down PoultryCare...")
        shutdown_event.set()

        deadline = time.time() + 5
        while (is_dispensing_feed or is_dispensing_water) and time.time() < deadline:
            time.sleep(0.1)

        try:
            close_feed()
            close_water()
        except Exception as e:
            print(f"Could not close servos on shutdown: {e}")

        try:
            turn_off_heating_lamp()
            turn_off_exhaust_fan()
        except Exception as e:
            print(f"Could not turn off relays on shutdown: {e}")

        try:
            cleanup()
            cleanup_servos()
        except Exception as e:
            print(f"Could not clean up GPIO: {e}")

        for name in ("feedServo", "waterServo", "heatingLamp", "exhaustFan"):
            update_actuator_state(name, False)

        try:
            db.reference('/sensor_data/system_online').set(False)
        except Exception as e:
            print(f"Could not set system_online False (offline?): {e}")

        print("Shutdown complete")

    def _on_sigterm(signum, frame):
        cleanup_actuators()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)

    try:
        while True:

            # Fast sensors: read every loop
            if is_dispensing_feed:
                instant_grams = latest_feed_weight   # the dispenser owns the load cell right now
            else:
                instant_grams = read_grams(capacity=get_feed_capacity())
            feed_weight = instant_grams
            water_level = read_water_level()
            container_level = read_container_level()

            if instant_grams is not None:
                latest_feed_weight = instant_grams
            # Health uses the REAL HX711 read (read_grams() returns the last good
            # value when the sensor gives nothing, which would hide a dead scale)
            raw_time, raw_value = get_last_raw()
            if raw_time is not None:
                last_load_cell_ok_time = raw_time
                note_reading("load_cell", raw_value)
            latest_water_level = water_level

            # Slow sensor - only True this iteration if a fresh DHT22 sample
            # was actually taken (used to gate the temp/humidity alert debounce).
            new_slow_reading = False
            if loop_count % slow_read_loops == 0:
                temperature, humidity = read_dht22()
                if temperature is not None:
                    last_temperature = temperature
                    last_humidity = humidity
                    new_slow_reading = True
                    last_dht_ok_time = time.time()
                    note_reading("dht22", (temperature, humidity))

            # Feed container (ultrasonic) - read on the same slow schedule as the DHT22
            new_bin_reading = False
            if loop_count % slow_read_loops == 0:
                bin_pct = read_feed_bin_percent()
                if bin_pct is not None:
                    latest_feed_bin_percent = bin_pct
                    last_bin_ok_time = time.time()
                    new_bin_reading = True
                    note_reading("feed_bin", get_last_distance())

            temperature = last_temperature
            humidity = last_humidity

            feed_display = f"{feed_weight:.1f}g" if feed_weight is not None else "N/A"
            print(f"Temp: {temperature}C | Humidity: {humidity}% | "
                  f"Feed: {feed_display} | Water: {water_level} | Container: {container_level}")

            # Calculate once here, then push in the SAME call as the rest of
            # the sensor data - one write, one round trip, instead of two.
            feed_percent_for_app = (
                round(min(100, (feed_weight / get_feed_capacity()) * 100), 1)
                if feed_weight is not None else None
            )

            health = check_component_health()

            push_sensor_data_async(
                temperature, humidity, feed_weight or 0, water_level,
                feed_percent=feed_percent_for_app,
                extra_updates={
                    "actuators/exhaustFan": exhaust_fan_on,
                    "actuators/heatingLamp": heating_lamp_on,
                    "actuators/feedServo": is_dispensing_feed,
                    "actuators/waterServo": is_dispensing_water,
                    "sensor_data/feed_low_active": feed_low_active,
                    "sensor_data/container_level": container_level,
                    "sensor_data/container_empty_active": container_empty_active,
                    "sensor_data/water_refill_failed": water_refill_failed,
                    "sensor_data/sensor_health": health,
                    "sensor_data/sensor_fault_reason": dict(fault_reason),
                    "sensor_data/feed_bin_percent": latest_feed_bin_percent,
                    "sensor_data/feed_bin_low_active": feed_bin_low_active,
                    "sensor_data/feed_bin_state": feed_bin_state,
                }
            )

            # Thresholds and schedules are refreshed by sync_worker in the
            # background - the main loop only reads the cached copies.
            thresholds = cached_thresholds
            schedules = cached_schedules

            # Environmental control runs every fast loop using the latest
            # known reading - hysteresis timing and threshold changes
            # apply promptly, even though the DHT22 itself only refreshes
            # on the slow interval.
            if temperature is not None:
                control_environment(temperature, humidity, thresholds)

            # Check alerts every fast loop (debounce logic inside gates
            # temp/humidity on new_slow_reading; feed debounces every loop)
            check_alerts(None if is_dispensing_feed else feed_weight, water_level,
                         temperature, humidity, thresholds,
                         new_slow_reading=new_slow_reading, container_level=container_level)

            # Feed container alert (ultrasonic): low / empty
            check_feed_bin_alert(latest_feed_bin_percent, new_bin_reading)

            # (Water refills run in water_watcher(), its own fast thread)

            # Check feeding schedules every fast loop
            check_schedules_with_data(schedules)

            # Update shared state file for the LCD dashboard.
            # sensor_health=health is the DEBOUNCED health, same as the app uses.
            next_sched = get_next_schedule_info(schedules)
            write_shared_state(temperature, humidity, feed_weight, water_level, thresholds,
                               next_schedule=next_sched, container_level=container_level,
                               sensor_health=health)

            loop_count += 1
            time.sleep(config.FAST_READ_INTERVAL)

    except KeyboardInterrupt:
        print("\nShutting down PoultryCare...")
        shutdown_event.set()
        deadline = time.time() + 5
        while (is_dispensing_feed or is_dispensing_water) and time.time() < deadline:
            time.sleep(0.1)
        try:
            close_feed()
            close_water()
        except Exception as e:
            print(f"Could not close servos on shutdown: {e}")
        turn_off_heating_lamp()
        turn_off_exhaust_fan()
        cleanup()
        cleanup_servos()
        for name in ("feedServo", "waterServo", "heatingLamp", "exhaustFan"):
            update_actuator_state(name, False)
        try:
            db.reference('/sensor_data/system_online').set(False)
        except Exception as e:
            print(f"Could not set system_online False (offline?): {e}")
        print("Shutdown complete")


if __name__ == "__main__":
    main()