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
from sensors.float_sensor import (
    setup_float_sensor, read_water_level, read_container_level,
    read_water_level_settled, read_trough_raw, TROUGH_NORMAL_STATE,
)
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

import firebase_admin
from firebase_admin import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def cfg(name, default):
    """Read a setting from config.py, falling back to a default if it is missing."""
    return getattr(config, name, default)


def save_json_atomic(path, data):
    # write to a temp file first, so a power cut can't leave a half-written file
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# =====================================================================
# Global state
# =====================================================================
shutdown_event = threading.Event()

is_dispensing_feed = False     # True from the moment a feeding owns the scale/servo lock
feed_servo_active = False      # True only once the feed servo has REALLY been commanded open
is_dispensing_water = False    # True only while the water servo is commanded open
heating_lamp_on = False
exhaust_fan_on = False

last_feeding_progress = {
    "percent": None,
    "grams": None,
    "target_grams": None,
    "triggered_by": None,
    "timestamp": None
}

latest_feed_weight = None
latest_water_level = None

STARTUP_TIME = time.time()
last_dht_ok_time = None
last_load_cell_ok_time = None
last_bin_ok_time = None
latest_feed_bin_percent = None

NO_READING_LIMIT = cfg("SENSOR_NO_READING_SECONDS", {"dht22": 120, "load_cell": 60, "feed_bin": 120})
STUCK_LIMIT = cfg("SENSOR_STUCK_SECONDS", {"dht22": 2 * 3600, "load_cell": 600, "feed_bin": 6 * 3600})
value_last = {}          # key -> last value seen
value_changed_at = {}    # key -> when the value last changed
fault_reason = {}        # key -> reason string or None (live, not debounced)

SCHEDULE_CACHE_FILE = os.path.join(BASE_DIR, "schedule_cache.json")
THRESHOLD_CACHE_FILE = os.path.join(BASE_DIR, "threshold_cache.json")
ALERT_STATE_FILE = os.path.join(BASE_DIR, "alert_state_cache.json")
DISPENSE_RATE_CACHE_FILE = os.path.join(BASE_DIR, "dispense_rate_cache.json")

THRESHOLD_REFRESH_INTERVAL = 10
cached_thresholds = None
cached_schedules = None

fired_schedules_this_minute = set()
last_checked_minute = None

# ---------------- Alert flags ----------------
feed_low_active = False          # plate (load cell) is low
feed_low_since = None            # when the plate first read below the low threshold (not saved)
temp_high_active = False
temp_low_active = False
hum_high_active = False

# Water container (second float). Alert + "Low" status only after reading low for a long time.
container_empty_active = False   # True = confirmed LOW (kept under this name for the app)
water_bin_candidate_since = None

# Feed container (ultrasonic): "ok" | "low" | "empty", confirmed over time
feed_bin_low_active = False      # True for both "low" and "empty"
feed_bin_state = "ok"
feed_bin_bad_since = None
feed_bin_bad_floor = None
feed_bin_good_since = None
feed_bin_good_ceiling = None

# Temp / humidity debounce counters (advance only on a genuinely new DHT22 sample)
temp_low_pending = 0
temp_low_clear_pending = 0
temp_high_pending = 0
temp_high_clear_pending = 0
hum_high_pending = 0
hum_high_clear_pending = 0

exhaust_fan_last_on_time = None

# ---------------- Automatic water refill ----------------
water_refill_cooldown_until = 0
water_refill_times = deque(maxlen=20)
last_refill_at = None
water_fail_streak = 0
water_lock = threading.Lock()

# ---------------- Feed servo result tracking ----------------
feed_fail_streak = 0

# ---------------- Servo interlock ----------------
# servo_lock: only ONE servo (feed or water) may be open at a time.
# feed_pending: set while a feeding is waiting for / using the servo lock.
# dispense_gate: only ONE feeding sequence may run at a time (atomic, unlike a plain flag).
servo_lock = threading.Lock()
feed_pending = threading.Event()
dispense_gate = threading.Lock()

# Second line of defence: the real "who is open right now" record. _servo_open() refuses
# to open a servo if the other one is open, even if a bug ever got past servo_lock.
servo_state_lock = threading.Lock()
servo_open = {"feed": False, "water": False}
servo_opened_at = {"feed": None, "water": None}
last_servo_close_at = 0.0
_SERVO_FUNCS = {"feed": (open_feed, close_feed), "water": (open_water, close_water)}


def _servo_open(which):
    other = "water" if which == "feed" else "feed"
    with servo_state_lock:
        if servo_open[other]:
            raise RuntimeError(f"Interlock: {other} servo is open, refusing to open {which} servo")
        servo_open[which] = True
        servo_opened_at[which] = time.time()
    try:
        _SERVO_FUNCS[which][0]()
    except Exception:
        with servo_state_lock:
            servo_open[which] = False
            servo_opened_at[which] = None
        raise


def _servo_close(which):
    global last_servo_close_at
    _SERVO_FUNCS[which][1]()
    with servo_state_lock:
        servo_open[which] = False
        servo_opened_at[which] = None
        last_servo_close_at = time.time()


def _wait_servo_handoff():
    """Give the servo that just closed a moment to physically finish before the next opens."""
    gap = cfg("SERVO_HANDOFF_SECONDS", 0.5) - (time.time() - last_servo_close_at)
    if gap > 0:
        time.sleep(gap)


def _acquire_servo():
    """Feed side: wait for the other servo to finish. Returns False only if we're shutting down."""
    while not shutdown_event.is_set():
        if servo_lock.acquire(timeout=1.0):
            return True
    return False


def _acquire_servo_for_water():
    """Water side: returns True holding servo_lock AND with no feeding waiting.
    If a feeding is waiting, the lock is handed back and we wait until that feeding is done."""
    while not shutdown_event.is_set():
        if not servo_lock.acquire(timeout=1.0):
            continue
        if feed_pending.is_set():
            servo_lock.release()
            shutdown_event.wait(0.2)
            continue
        return True
    return False


def check_servo_watchdog():
    """A servo that has been commanded open far longer than any normal run is forced
    closed and reported once as 'stuck open'."""
    now = time.time()
    limits = {"feed": cfg("FEED_SERVO_MAX_OPEN_SECONDS", 15),
              "water": cfg("WATER_SERVO_MAX_OPEN_SECONDS", 300)}
    for which, limit in limits.items():
        opened = servo_opened_at[which]
        if servo_open[which] and opened is not None and now - opened > limit:
            print(f"WATCHDOG: {which} servo open for more than {limit}s - forcing it closed")
            try:
                _servo_close(which)
            except Exception as e:
                print(f"Watchdog could not close {which} servo: {e}")
            set_outcome_fault("feed_servo" if which == "feed" else "water_refill", "stuck_open")


def get_feed_capacity():
    return (cached_thresholds or {}).get("feedCapacityGrams") or config.FEED_CAPACITY_GRAMS


# =====================================================================
# Alert repeat protection
# =====================================================================
_last_alert_at = {}


def alert_allowed(key, cooldown=None):
    """True at most once per cooldown for the same kind of alert, so a value hovering
    around a threshold can't spam Firebase, the app or the phone."""
    if cooldown is None:
        cooldown = cfg("ALERT_REPEAT_COOLDOWN_SECONDS", 1800)
    now = time.time()
    last = _last_alert_at.get(key)
    if last is not None and now - last < cooldown:
        return False
    _last_alert_at[key] = now
    return True


# =====================================================================
# Component health (ALL parts)
# =====================================================================
# Sensors are judged by their readings: nothing for too long, or the exact same
# value for too long. Everything else is judged by what really happened:
#   feed_servo    -> the dispenses delivered (almost) nothing, or it stayed open too long
#   water_refill  -> valve + float + supply: trough still low after a full refill, or stuck open
#   heating_lamp  -> was ON for a long time but the coop did not warm up
#   exhaust_fan   -> was ON for a long time but temperature/humidity did not improve
SENSOR_KEYS = ("dht22", "load_cell", "feed_bin")
OUTCOME_KEYS = ("feed_servo", "water_refill", "heating_lamp", "exhaust_fan")
COMPONENT_NAMES = {
    "dht22": "Temp/Humidity Sensor (DHT22)",
    "load_cell": "Feed Scale (Load Cell)",
    "feed_bin": "Feed Container Sensor (Ultrasonic)",
    "feed_servo": "Feed Dispenser (Servo)",
    "water_refill": "Drinker Refill (Valve/Float/Supply)",
    "heating_lamp": "Heating Lamp",
    "exhaust_fan": "Exhaust Fan",
}
component_fault_active = {k: False for k in COMPONENT_NAMES}
component_bad_since = {k: None for k in COMPONENT_NAMES}
component_ok_since = {k: None for k in COMPONENT_NAMES}
component_alert_sent = {k: False for k in COMPONENT_NAMES}
component_last_alert_at = {k: 0.0 for k in COMPONENT_NAMES}
reported_reason = {k: None for k in COMPONENT_NAMES}
outcome_fault = {}   # key -> reason, set/cleared by events (saved to disk so a restart keeps it)

FAULT_DETAIL = {
    "stuck": "The reading has not changed for a long time - it may be stuck or broken.",
    "no_reading": "No readings are coming in - check the cable/connection.",
    "no_output": "The feed servo opened but almost no feed came out - check for a jam or an empty hopper.",
    "error": "The part reported an error while running - check the servo and its wiring.",
    "stuck_open": "The servo stayed open much longer than normal - it may be stuck. Check it now.",
    "no_fill": "The drinker is still low after refilling - check the water valve, supply and float.",
    "repeating": "The drinker keeps going low - check for a leak or a stuck float.",
    "no_heat": "The lamp has been ON for a long time but the coop is not warming up - check the bulb and wiring.",
    "no_airflow": "The fan has been ON for a long time but temperature/humidity are not improving - check the fan.",
}


def set_outcome_fault(key, reason):
    if outcome_fault.get(key) != reason:
        outcome_fault[key] = reason
        save_alert_state()


def clear_outcome_fault(key, only_reasons=None):
    if key in outcome_fault and (only_reasons is None or outcome_fault[key] in only_reasons):
        del outcome_fault[key]
        save_alert_state()


def note_reading(key, value):
    """Call with every real reading. Remembers when the value last changed."""
    if value_last.get(key) != value:
        value_last[key] = value
        value_changed_at[key] = time.time()


def get_sensor_health():
    """True = working. Sensors: faulty only if NO value for too long, or the exact same
    value for a very long time. Other parts: faulty only if an outcome fault is set."""
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
    for key in OUTCOME_KEYS:
        reason = outcome_fault.get(key)
        health[key] = reason is None
        fault_reason[key] = reason
    return health


def check_component_health():
    """Notifies the app ONCE when a part goes bad and ONCE when it recovers.
    - sensors: a fault must last a few seconds / recovery a couple of minutes (debounce)
    - outcome faults are already confirmed when they are set, so they alert right away
    - a part that faults again within COMPONENT_REALERT_COOLDOWN_SECONDS is NOT re-notified
      (the LCD and app status still show it), so a flaky part can't flood Firebase/phone.
    Returns (health, reasons): the debounced state the LCD and the app both use."""
    health = get_sensor_health()
    now = time.time()
    fault_after = cfg("COMPONENT_FAULT_CONFIRM_SECONDS", 10)
    recover_after = cfg("COMPONENT_RECOVER_CONFIRM_SECONDS", 120)
    cooldown = cfg("COMPONENT_REALERT_COOLDOWN_SECONDS", 3600)

    for key, label in COMPONENT_NAMES.items():
        if key in OUTCOME_KEYS:
            f_after, r_after = 0, 0
        else:
            f_after, r_after = fault_after, recover_after

        raw_ok = health.get(key, True)
        if raw_ok:
            component_bad_since[key] = None
            if component_ok_since[key] is None:
                component_ok_since[key] = now
        else:
            component_ok_since[key] = None
            if component_bad_since[key] is None:
                component_bad_since[key] = now

        bad_for = (now - component_bad_since[key]) if component_bad_since[key] is not None else 0
        ok_for = (now - component_ok_since[key]) if component_ok_since[key] is not None else 0

        if not raw_ok:
            # keep the reason current while the fault is active
            reported_reason[key] = fault_reason.get(key) or reported_reason.get(key)

        if (not raw_ok) and bad_for >= f_after and not component_fault_active[key]:
            component_fault_active[key] = True
            reason = fault_reason.get(key)
            reported_reason[key] = reason
            detail = FAULT_DETAIL.get(reason, "Not working properly - please check it.")
            if now - component_last_alert_at[key] < cooldown:
                component_alert_sent[key] = False
                print(f"Component fault (already notified recently, staying quiet): {label} ({reason})")
            else:
                component_last_alert_at[key] = now
                component_alert_sent[key] = True
                log_alert(f"{label} Problem", detail)
                try:
                    push_notification({
                        "type": "componentFault",
                        "component": key,
                        "name": label,
                        "reason": reason,
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                    send_alert_push(f"{label} Problem", detail)
                except Exception as e:
                    print(f"Component fault notification error: {e}")
                print(f"Component fault: {label} ({reason})")
            save_alert_state()

        elif raw_ok and ok_for >= r_after and component_fault_active[key]:
            component_fault_active[key] = False
            reported_reason[key] = None
            if component_alert_sent[key]:
                log_alert(f"{label} Recovered", "Working normally again")
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
            component_alert_sent[key] = False
            save_alert_state()

    health_out = {k: (not component_fault_active[k]) for k in COMPONENT_NAMES}
    reasons_out = {k: reported_reason.get(k) for k in COMPONENT_NAMES if component_fault_active[k]}
    return health_out, reasons_out


# ---------------- Lamp / fan "really doing its job" checks ----------------
lamp_track = {"since": None, "base": None, "verified": False}
fan_track = {"since": None, "base_t": None, "base_h": None, "verified": False}


def _reset_effect_trackers():
    lamp_track.update(since=None, base=None, verified=False)
    fan_track.update(since=None, base_t=None, base_h=None, verified=False)


def check_actuator_effects(temperature, humidity, thresholds):
    """Lamp ON for a long time but temperature not rising = lamp/relay problem.
    Fan ON for a long time but temperature AND humidity not improving = fan/relay problem.
    Only judged while the DHT22 itself is healthy (no evidence otherwise)."""
    if not cfg("ACTUATOR_EFFECT_CHECK_ENABLED", True):
        return
    if temperature is None or component_fault_active.get("dht22"):
        _reset_effect_trackers()
        return

    now = time.time()
    temp_min = thresholds.get("tempMin", config.DEFAULT_TEMP_MIN)
    temp_max = thresholds.get("tempMax", config.DEFAULT_TEMP_MAX)
    hum_max = thresholds.get("humMax", config.DEFAULT_HUM_MAX)

    # ----- heating lamp -----
    if heating_lamp_on:
        if lamp_track["since"] is None:
            lamp_track.update(since=now, base=temperature, verified=False)
        elif not lamp_track["verified"]:
            rise = temperature - lamp_track["base"]
            if rise >= cfg("LAMP_MIN_RISE_C", 0.5) or temperature >= temp_min:
                lamp_track["verified"] = True
                clear_outcome_fault("heating_lamp")
            elif now - lamp_track["since"] >= cfg("LAMP_CHECK_SECONDS", 1200):
                lamp_track["verified"] = True        # judged once per ON cycle
                set_outcome_fault("heating_lamp", "no_heat")
    else:
        lamp_track.update(since=None, base=None, verified=False)

    # ----- exhaust fan -----
    if exhaust_fan_on:
        if fan_track["since"] is None:
            fan_track.update(since=now, base_t=temperature, base_h=humidity, verified=False)
        elif not fan_track["verified"]:
            t_drop = fan_track["base_t"] - temperature
            h_drop = 0
            if fan_track["base_h"] is not None and humidity is not None:
                h_drop = fan_track["base_h"] - humidity
            back_to_normal = temperature <= temp_max and (humidity is None or humidity <= hum_max)
            if (t_drop >= cfg("FAN_MIN_DROP_C", 0.3) or h_drop >= cfg("FAN_MIN_DROP_HUM", 2.0)
                    or back_to_normal):
                fan_track["verified"] = True
                clear_outcome_fault("exhaust_fan")
            elif now - fan_track["since"] >= cfg("FAN_CHECK_SECONDS", 1800):
                fan_track["verified"] = True
                set_outcome_fault("exhaust_fan", "no_airflow")
    else:
        fan_track.update(since=None, base_t=None, base_h=None, verified=False)


# =====================================================================
# Threshold logic
# =====================================================================
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


# =====================================================================
# Environmental control
# =====================================================================
def control_environment(temperature, humidity, thresholds):
    global heating_lamp_on, exhaust_fan_on, exhaust_fan_last_on_time

    if temperature is None:
        return

    temp_min = thresholds.get("tempMin", config.DEFAULT_TEMP_MIN)
    temp_max = thresholds.get("tempMax", config.DEFAULT_TEMP_MAX)
    hum_max = thresholds.get("humMax", config.DEFAULT_HUM_MAX)

    # Heating Lamp (with hysteresis)
    if heating_lamp_on:
        need_heat = temperature < (temp_min + config.TEMP_HYSTERESIS)
    else:
        need_heat = temperature < temp_min

    # Exhaust Fan (with hysteresis). Fully independent of the heating lamp:
    # humidity above hum_max always opens the fan, even while the lamp is on.
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

    # Minimum run time so the fan can actually vent moisture/heat out.
    if exhaust_fan_on and not need_exhaust:
        elapsed = time.time() - (exhaust_fan_last_on_time or 0)
        if elapsed < config.EXHAUST_MIN_RUN_SECONDS:
            need_exhaust = True

    # ---- Heating Lamp ----
    if need_heat and not heating_lamp_on:
        turn_on_heating_lamp()
        heating_lamp_on = True
        update_actuator_state("heatingLamp", True)
        log_actuator("Heating Lamp", "ON",
                     f"Temperature dropped to {temperature:.1f}°C (below {temp_min}°C)")
        print(f" Heating Lamp ON ({temperature:.1f}°C)")

    elif not need_heat and heating_lamp_on:
        turn_off_heating_lamp()
        heating_lamp_on = False
        update_actuator_state("heatingLamp", False)
        log_actuator("Heating Lamp", "OFF", f"Temperature recovered to {temperature:.1f}°C")
        print(f" Heating Lamp OFF ({temperature:.1f}°C)")

    # ---- Exhaust Fan ----
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


# =====================================================================
# Dispense rate memory (feeds the bulk phase estimate)
# =====================================================================
def _load_avg_dispense_rate():
    """Grams/second the feeder actually delivers, learned from past dispenses."""
    try:
        with open(DISPENSE_RATE_CACHE_FILE, "r") as f:
            data = json.load(f)
        rate = data.get("avg_rate_gps")
        if rate and rate > 0:
            return rate
    except Exception:
        pass
    return cfg("GRAMS_PER_SECOND", 5)


def _save_avg_dispense_rate(new_rate):
    """Blend the newly measured rate into the persisted average."""
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
    """A few spaced weight readings, median-filtered."""
    readings = []
    for _ in range(samples_sets):
        val = read_grams(samples=8, trust_reading=True, capacity=capacity)
        if val is not None:
            readings.append(val)
        time.sleep(delay)
    if not readings:
        return None
    return round(statistics.median(readings), 1)


# =====================================================================
# Feed dispensing
# =====================================================================
def _feed_open():
    """Open the feed servo. The app/LCD only hear 'feed servo ON' once it was REALLY commanded open."""
    global feed_servo_active
    _servo_open("feed")
    if not feed_servo_active:
        feed_servo_active = True
        update_actuator_state("feedServo", True)


def _note_feed_failure(reason):
    global feed_fail_streak
    feed_fail_streak += 1
    if feed_fail_streak >= cfg("FEED_FAIL_CONFIRM_DISPENSES", 2):
        set_outcome_fault("feed_servo", reason)


def _evaluate_feed_outcome(requested, dispensed):
    """Judge the feed servo by what it really delivered."""
    global feed_fail_streak
    if requested < cfg("FEED_FAIL_MIN_REQUEST_G", 10):
        return
    if dispensed >= requested * 0.5:
        feed_fail_streak = 0
        clear_outcome_fault("feed_servo")
        return
    if dispensed < max(2.0, requested * 0.1):
        hopper = latest_feed_bin_percent
        hopper_empty = hopper is not None and hopper <= cfg("FEED_BIN_EMPTY_PERCENT", 5)
        # An empty hopper or a broken scale explains "nothing came out" - don't blame the servo
        if hopper_empty or feed_bin_state == "empty" or component_fault_active.get("load_cell"):
            return
        _note_feed_failure("no_output")


def dispense_feed(target_grams, triggered_by="schedule", schedule_key=None,
                  add_mode=False, configured_percent=None):
    """add_mode=True: target_grams = grams to ADD (manual/LCD/schedules).
       add_mode=False: target_grams = absolute plate target.
       configured_percent: the % the user set, this is what gets logged."""
    global is_dispensing_feed, feed_servo_active, last_feeding_progress, latest_feed_weight
    global feed_low_active
    if is_dispensing_feed:
        return

    feed_pending.set()            # tells a running water refill to pause and hand over the lock
    if not _acquire_servo():      # waits until the water servo is fully closed and released
        feed_pending.clear()
        return
    is_dispensing_feed = True
    try:
        _wait_servo_handoff()
        capacity = get_feed_capacity()
        # Measure with a ceiling far above hopper capacity so the FULL configured
        # amount can be weighed even when the plate isn't empty.
        read_cap = cfg("LOAD_CELL_MAX_GRAMS", 5000)

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

        min_rate = cfg("DISPENSE_MIN_RATE_GPS", 0.3)
        settle_seconds = cfg("DISPENSE_SETTLE_SECONDS", 0.6)
        settle_wait = max(config.SERVO_CLOSE_LATENCY_SECONDS, settle_seconds)
        min_pulse = cfg("DISPENSE_MIN_PULSE_SECONDS", 0.08)
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
        bulk_fraction = cfg("DISPENSE_BULK_FRACTION", 0.2)
        bulk_max_seconds = cfg("DISPENSE_BULK_MAX_SECONDS", 5)
        bulk_seconds = min(bulk_max_seconds, (amount_requested * bulk_fraction) / avg_rate)

        bulk_min = cfg("DISPENSE_BULK_MIN_AMOUNT_G", 60)
        if amount_requested >= bulk_min and bulk_seconds > 0.1:
            t0 = time.time()
            _feed_open()
            time.sleep(bulk_seconds)
            _servo_close("feed")
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
            _feed_open()
            time.sleep(pulse_seconds)
            _servo_close("feed")
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

        # Did the servo really deliver feed? (raises ONE servo fault if it keeps delivering nothing)
        _evaluate_feed_outcome(amount_requested, dispensed)

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
            hyst = cfg("FEED_HYSTERESIS_PERCENT", 5)
            if final_percent >= (low_thr + hyst):
                feed_low_active = False
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
        _note_feed_failure("error")
        try:
            _servo_close("feed")
        except Exception as close_err:
            print(f"close_feed() also failed: {close_err}")
    finally:
        if feed_servo_active:
            feed_servo_active = False
            update_actuator_state("feedServo", False)
        is_dispensing_feed = False
        servo_lock.release()
        feed_pending.clear()      # water refill may resume now


# =====================================================================
# Automatic water refill (trough float sensor)
# =====================================================================
def container_is_empty(confirm=3, gap=0.1):
    """Container float must read empty 3 times in a row (~0.2 s) so sloshing
    water can't fake it. (This is the instant pump-protection check; the ALERT
    for a low container waits 10 minutes, see check_water_container.)"""
    for i in range(confirm):
        if read_container_level() != "empty":
            return False
        if i < confirm - 1:
            time.sleep(gap)
    return True


def dispense_water(triggered_by="auto"):
    """Opens the water servo and keeps it open until the trough float reads normal
    (confirmed), then tops up and closes. A feeding PAUSES it: the valve closes, the
    feed runs to the end, then the refill reopens and continues.

    Servo exclusion: the water servo can only open while holding servo_lock, never
    while a feeding is running OR waiting, and _servo_open() refuses as a last resort."""
    global is_dispensing_water, water_refill_cooldown_until
    global water_fail_streak, last_refill_at
    if not water_lock.acquire(blocking=False):
        return                      # a refill is already running
    try:
        if read_water_level() != "low" or container_is_empty():
            return

        # Wait for any feeding (running or queued) to finish completely.
        if not _acquire_servo_for_water():
            return
        holding = True              # True while this thread owns servo_lock

        result = "error"
        raw_start = read_trough_raw()
        try:
            _wait_servo_handoff()
            # the wait may have taken a while: re-check before opening
            if read_water_level() != "low" or container_is_empty():
                return

            print("Auto-refill: trough low long enough, water servo open until normal")
            _servo_open("water")
            is_dispensing_water = True
            update_actuator_state("waterServo", True)
            opened_at = time.time()

            max_open = config.WATER_HARD_CAP_SECONDS   # total valve-OPEN time, across pauses
            open_used = 0.0
            reached_normal = False
            container_ran_out = False
            normal_confirm_count = 0

            while not shutdown_event.is_set():
                if open_used + (time.time() - opened_at) >= max_open:
                    break                                   # timed out

                # ---- a feeding wants the servo: pause the refill ----
                if feed_pending.is_set():
                    _servo_close("water")
                    open_used += time.time() - opened_at
                    is_dispensing_water = False
                    update_actuator_state("waterServo", False)
                    servo_lock.release()
                    holding = False
                    print("Water refill paused - feeding in progress")

                    # waits until the feeding is completely finished, then takes the lock back
                    if not _acquire_servo_for_water():
                        break                               # shutting down
                    holding = True
                    _wait_servo_handoff()

                    if container_is_empty():
                        container_ran_out = True
                        break
                    print("Feeding done - resuming water refill")
                    _servo_open("water")
                    is_dispensing_water = True
                    update_actuator_state("waterServo", True)
                    opened_at = time.time()
                    normal_confirm_count = 0
                    continue

                if container_is_empty():
                    container_ran_out = True
                    break

                if read_water_level() == "normal":
                    normal_confirm_count += 1
                    if normal_confirm_count >= config.WATER_NORMAL_CONFIRM_READS:
                        print("Water reached normal (confirmed). Topping up...")
                        # top-up ends early if a feeding is waiting
                        topup_end = time.time() + config.WATER_EXTRA_FILL_TIME
                        while (time.time() < topup_end and not shutdown_event.is_set()
                               and not feed_pending.is_set()):
                            shutdown_event.wait(0.2)
                        reached_normal = True
                        break
                else:
                    normal_confirm_count = 0
                feed_pending.wait(0.3)      # wakes immediately if a feeding asks for the servo

            if reached_normal:
                result = "dispensed"
            elif container_ran_out:
                result = "container_empty"
            elif shutdown_event.is_set():
                result = "interrupted"
            else:
                result = "timed_out"
                print(f"Water refill timed out after {max_open}s of valve-open time - check valve/supply")
        except Exception as e:
            print(f"Water dispense error: {e}")
        finally:
            # the valve is ALWAYS closed; the lock is released only if we still hold it
            try:
                _servo_close("water")
            except Exception as close_err:
                print(f"close_water() failed: {close_err}")
            update_actuator_state("waterServo", False)
            is_dispensing_water = False
            if holding:
                servo_lock.release()

        now = time.time()

        if result == "dispensed":
            water_fail_streak = 0
            clear_outcome_fault("water_refill", only_reasons=("no_fill", "stuck_open"))
        elif result in ("timed_out", "error"):
            water_fail_streak += 1
            water_refill_cooldown_until = now + cfg("WATER_REFILL_RETRY_COOLDOWN_SECONDS", 300)
            if result == "timed_out":
                if "water_refill" not in outcome_fault:
                    log_system(f"Refill diag: float pin {raw_start}->{read_trough_raw()} "
                               f"(pin value meaning 'normal' = {TROUGH_NORMAL_STATE})")
                if outcome_fault.get("water_refill") != "stuck_open":
                    set_outcome_fault("water_refill", "no_fill")    # ONE alert, sent by check_component_health
            if water_fail_streak >= cfg("WATER_MAX_FAILED_TRIES", 3):
                water_refill_cooldown_until = now + cfg("WATER_FAIL_LOCKOUT_SECONDS", 1800)
                water_fail_streak = 0

        if result == "dispensed":
            last_refill_at = now
            water_refill_cooldown_until = now + cfg("WATER_MIN_INTERVAL_SECONDS", 20)
            water_refill_times.append(now)
            recent = [t for t in water_refill_times if now - t < 3600]
            if len(recent) >= cfg("WATER_MAX_REFILLS_PER_HOUR", 6):
                water_refill_cooldown_until = now + cfg("WATER_REPEAT_PAUSE_SECONDS", 1800)
                water_refill_times.clear()
                set_outcome_fault("water_refill", "repeating")      # ONE alert via check_component_health

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
    """Own thread. A refill happens only after the trough has read LOW continuously
    for WATER_LOW_HOLD_SECONDS. Short dips are ignored."""
    global water_fail_streak
    hold = cfg("WATER_LOW_HOLD_SECONDS", 900)
    normal_reset = cfg("WATER_NORMAL_RESET_SECONDS", 10)
    fault_clear = cfg("WATER_FAULT_CLEAR_SECONDS", 120)
    low_since = None        # when the trough first read low (None = not low)
    normal_since = None     # when it started reading normal again
    last_skip = None

    while not shutdown_event.is_set():
        try:
            now = time.time()
            level = read_water_level_settled(samples=5, gap=0.05)

            if level == "normal":
                if normal_since is None:
                    normal_since = now
                # only cancel the low timer if it STAYS normal for a few seconds,
                # so a float bobbing at the edge can't reset it forever
                if now - normal_since >= normal_reset:
                    if low_since is not None:
                        print(f"Trough back to normal - {hold // 60} min low timer cancelled")
                    low_since = None
                    water_fail_streak = 0
                    last_skip = None
                if now - normal_since >= fault_clear:
                    clear_outcome_fault("water_refill", only_reasons=("no_fill",))
                if (outcome_fault.get("water_refill") == "repeating" and last_refill_at is not None
                        and now - last_refill_at > cfg("WATER_REPEAT_CLEAR_SECONDS", 3600)):
                    clear_outcome_fault("water_refill", only_reasons=("repeating",))
            else:
                normal_since = None
                if low_since is None:
                    low_since = now
                    log_system(f"Drinker reads low - will refill if still low in {hold // 60} min")

                low_for = now - low_since
                if low_for >= hold:
                    skip = None
                    if container_is_empty():
                        skip = "water container is empty"
                    elif now < water_refill_cooldown_until:
                        skip = "waiting for the safety pause to end"
                    if skip is None:
                        last_skip = None
                        dispense_water("auto")      # waits for the feed servo, blocks until done
                    elif skip != last_skip:
                        last_skip = skip
                        log_system(f"Water refill skipped: {skip}")
        except Exception as e:
            print(f"Water watcher error: {e}")
        shutdown_event.wait(cfg("WATER_CHECK_INTERVAL", 1.0))


# =====================================================================
# Feed dispense sequence (water is automatic now, feed only)
# =====================================================================
def run_dispense_sequence(feed_grams=None, triggered_by="schedule",
                          schedule_key=None, add_mode=False, configured_percent=None):
    # Atomic: only one feeding sequence can ever pass this gate at a time.
    if not dispense_gate.acquire(blocking=False):
        print("Dispense already in progress, skipping.")
        return
    try:
        if feed_grams:
            dispense_feed(feed_grams, triggered_by, schedule_key, add_mode, configured_percent)
    finally:
        dispense_gate.release()


# =====================================================================
# Schedule fetching (offline cache)
# =====================================================================
def get_schedules_cached():
    if not is_online() and cached_schedules is not None:
        return cached_schedules
    # Try Firebase (live, up-to-date)
    try:
        schedules = get_schedules()
    except Exception as e:
        # Only an actual exception counts as "Firebase failed" - fall back to the last-known cache.
        print(f"Schedule fetch failed, checking cache: {e}")
        try:
            with open(SCHEDULE_CACHE_FILE, "r") as f:
                cached = json.load(f)
                print("Using cached schedules (last known from app)")
                return cached
        except Exception:
            print("No cached schedules available")
            return None

    # The call succeeded - trust it completely, even if it's empty/None. An empty result
    # means schedules were genuinely deleted/disabled, so overwrite the cache with that.
    try:
        save_json_atomic(SCHEDULE_CACHE_FILE, schedules or {})
    except Exception as e:
        print(f"Schedule cache write failed: {e}")

    return schedules


# Find next scheduled feed (for LCD display)
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


# Schedule checker
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

            # Prefer "percent" (of the live hopper capacity) over the frozen
            # "amount_grams" fallback that older schedules still use.
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


# =====================================================================
# Manual dispense listener (app feed button)
# =====================================================================
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


# LCD command listener (local file, works fully offline)
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


def manual_command_watcher():
    poll_interval = cfg('MANUAL_COMMAND_POLL_INTERVAL', 0.5)
    while True:
        try:
            check_manual_commands()
        except Exception as e:
            print(f"Manual command watcher error: {e}")
        time.sleep(poll_interval)


def lcd_command_watcher():
    poll_interval = cfg('LCD_COMMAND_POLL_INTERVAL', 0.2)
    while True:
        try:
            check_lcd_commands()
        except Exception as e:
            print(f"LCD command watcher error: {e}")
        time.sleep(poll_interval)


# =====================================================================
# Alert state persistence
# =====================================================================
_last_saved_state = None
_state_save_lock = threading.Lock()


def load_alert_state():
    """Restore alert state from disk on startup, so a restart doesn't
    think every ongoing problem is new (and re-notify)."""
    global feed_low_active
    global temp_high_active, temp_low_active, hum_high_active
    global container_empty_active
    global temp_low_pending, temp_low_clear_pending
    global temp_high_pending, temp_high_clear_pending
    global hum_high_pending, hum_high_clear_pending
    global feed_bin_low_active, feed_bin_state

    try:
        with open(ALERT_STATE_FILE, "r") as f:
            state = json.load(f)
    except Exception:
        print("No alert state cache found - starting fresh")
        return

    try:
        feed_low_active = state.get("feed_low_active", False)
        temp_high_active = state.get("temp_high_active", False)
        temp_low_active = state.get("temp_low_active", False)
        hum_high_active = state.get("hum_high_active", False)
        container_empty_active = state.get("container_empty_active", False)
        feed_bin_state = state.get("feed_bin_state",
                                   "low" if state.get("feed_bin_low_active") else "ok")
        feed_bin_low_active = feed_bin_state != "ok"

        for k, v in state.get("component_fault_active", {}).items():
            if k in component_fault_active:
                component_fault_active[k] = bool(v)
                if v:
                    component_alert_sent[k] = True      # it was already announced before the restart
        for k, v in state.get("outcome_fault", {}).items():
            if k in OUTCOME_KEYS and v:
                outcome_fault[k] = v
                reported_reason[k] = v

        temp_low_pending = state.get("temp_low_pending", 0)
        temp_low_clear_pending = state.get("temp_low_clear_pending", 0)
        temp_high_pending = state.get("temp_high_pending", 0)
        temp_high_clear_pending = state.get("temp_high_clear_pending", 0)
        hum_high_pending = state.get("hum_high_pending", 0)
        hum_high_clear_pending = state.get("hum_high_clear_pending", 0)
        print(f"Restored alert state from disk: {state}")
    except Exception as e:
        print(f"Alert state restore error (starting fresh): {e}")


def save_alert_state():
    """Writes the file only when something actually changed (saves the SD card
    and avoids a disk write every second)."""
    global _last_saved_state
    state = {
        "feed_low_active": feed_low_active,
        "temp_high_active": temp_high_active,
        "temp_low_active": temp_low_active,
        "hum_high_active": hum_high_active,
        "container_empty_active": container_empty_active,
        "feed_bin_low_active": feed_bin_low_active,
        "feed_bin_state": feed_bin_state,
        "component_fault_active": dict(component_fault_active),
        "outcome_fault": dict(outcome_fault),
        "temp_low_pending": temp_low_pending,
        "temp_low_clear_pending": temp_low_clear_pending,
        "temp_high_pending": temp_high_pending,
        "temp_high_clear_pending": temp_high_clear_pending,
        "hum_high_pending": hum_high_pending,
        "hum_high_clear_pending": hum_high_clear_pending,
    }
    with _state_save_lock:
        if state == _last_saved_state:
            return
        try:
            save_json_atomic(ALERT_STATE_FILE, state)
            _last_saved_state = state
        except Exception as e:
            print(f"Alert state cache write failed: {e}")


# =====================================================================
# Alerts: temperature, humidity, plate feed level
# =====================================================================
def check_alerts(feed_weight, water_level, temperature, humidity, thresholds,
                 new_slow_reading=False):
    global feed_low_active, feed_low_since
    global temp_high_active, temp_low_active, hum_high_active
    global temp_low_pending, temp_low_clear_pending
    global temp_high_pending, temp_high_clear_pending
    global hum_high_pending, hum_high_clear_pending

    temp_max = thresholds.get("tempMax", config.DEFAULT_TEMP_MAX)
    temp_min = thresholds.get("tempMin", config.DEFAULT_TEMP_MIN)
    hum_max = thresholds.get("humMax", config.DEFAULT_HUM_MAX)

    temp_confirm = cfg("TEMP_ALERT_CONFIRM_READS", 5)
    hum_confirm = cfg("HUM_ALERT_CONFIRM_READS", 5)

    # Temperature / humidity - counters only advance on an actual new DHT22 sample.
    # The "active" flags make it one alert per episode; alert_allowed() additionally stops
    # a value hovering around the limit from re-alerting within the cooldown.
    if new_slow_reading and temperature is not None:

        # High temp
        if temperature > temp_max:
            temp_high_clear_pending = 0
            if not temp_high_active:
                temp_high_pending += 1
                if temp_high_pending >= temp_confirm:
                    temp_high_active = True
                    temp_high_pending = 0
                    if alert_allowed("highTemp"):
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
                    if alert_allowed("lowTemp"):
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
                    if alert_allowed("humHigh"):
                        log_alert("Humidity High", f"{humidity:.1f}% (above {hum_max}%), exhaust fan ON")
                        push_notification({
                            "type": "humidityHigh",
                            "value": humidity,
                            "threshold": hum_max,
                            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        })
                        send_alert_push("High Humidity Alert", f"Humidity reached {humidity:.1f}% - exhaust fan ON")
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

    # Plate feed level - the plate must read below the threshold for FEED_LOW_CONFIRM_SECONDS
    # (10 min) straight. Any reading back above the threshold restarts the timer.
    # Clearing ("Feed Restocked") is handled ONLY in dispense_feed(), right after a real
    # dispense settles, so raw load-cell noise can never flap the restocked notification.
    if feed_weight is not None:
        feed_percent = (feed_weight / get_feed_capacity()) * 100
        now = time.time()

        if feed_percent < feed_low_threshold:
            if not feed_low_active:
                if feed_low_since is None:
                    feed_low_since = now
                elif now - feed_low_since >= cfg("FEED_LOW_CONFIRM_SECONDS", 600):
                    feed_low_active = True
                    feed_low_since = None
                    update_feed_alert_state(True)
                    if alert_allowed("lowFeed"):
                        log_alert("Low Feed", f"{feed_weight:.0f} g remaining ({feed_percent:.0f}%)")
                        push_notification({
                            "type": "lowFeed",
                            "value": feed_weight,
                            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        })
                        send_alert_push("Low Feed Alert",
                                        f"Feed level is low ({feed_percent:.0f}% remaining)")
                    print(f"Low Feed: {feed_weight:.0f} g ({feed_percent:.0f}%)")
        else:
            feed_low_since = None

    save_alert_state()


# =====================================================================
# Water container (second float): confirmed low only after reading low for a long time
# =====================================================================
def check_water_container(container_level):
    global container_empty_active, water_bin_candidate_since
    if container_level not in ("empty", "ok"):
        return
    low_now = container_level == "empty"
    now = time.time()

    if low_now == container_empty_active:
        water_bin_candidate_since = None     # reading agrees with the confirmed state: nothing pending
        return
    if water_bin_candidate_since is None:
        water_bin_candidate_since = now      # reading disagrees: start timing it
        return

    need = (cfg("WATER_BIN_LOW_CONFIRM_SECONDS", 600) if low_now
            else cfg("WATER_BIN_REFILL_CONFIRM_SECONDS", 60))
    if now - water_bin_candidate_since < need:
        return

    container_empty_active = low_now
    water_bin_candidate_since = None
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        if low_now:
            log_alert("Water Container Low", "Water container stayed low - please refill it")
            push_notification({"type": "containerEmpty", "timestamp": stamp})
            send_alert_push("Water Container Low", "The water container needs refill.")
            print("Water container: LOW (confirmed)")
        else:
            log_alert("Water Container Refilled", "Container has water again")
            push_notification({"type": "containerRefilled", "timestamp": stamp})
            print("Water container: refilled")
    except Exception as e:
        print(f"Water container notification error: {e}")
    save_alert_state()


# =====================================================================
# Feed container (ultrasonic): ok / low / empty, confirmed over time
# =====================================================================
_BIN_RANK = {"ok": 0, "low": 1, "empty": 2}


def _bin_target(percent, current):
    low = cfg("FEED_BIN_LOW_PERCENT", 20)
    empty = cfg("FEED_BIN_EMPTY_PERCENT", 5)
    hyst = cfg("FEED_BIN_HYSTERESIS_PERCENT", 5)
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
    """ok / low / empty. Getting WORSE must hold for FEED_BIN_LOW_CONFIRM_SECONDS (10 min)
    straight; getting better (refilled) must hold for FEED_BIN_REFILL_CONFIRM_SECONDS.
    One notification per confirmed change of state."""
    global feed_bin_state, feed_bin_low_active
    global feed_bin_bad_since, feed_bin_bad_floor, feed_bin_good_since, feed_bin_good_ceiling
    if percent is None or not new_reading:
        return

    now = time.time()
    target = _bin_target(percent, feed_bin_state)
    new_state = None

    if target == feed_bin_state:
        feed_bin_bad_since = None
        feed_bin_good_since = None
        return

    if _BIN_RANK[target] > _BIN_RANK[feed_bin_state]:
        # getting worse: commit to the LEAST bad level seen during the whole wait
        feed_bin_good_since = None
        if feed_bin_bad_since is None:
            feed_bin_bad_since = now
            feed_bin_bad_floor = target
        elif _BIN_RANK[target] < _BIN_RANK[feed_bin_bad_floor]:
            feed_bin_bad_floor = target
        if now - feed_bin_bad_since >= cfg("FEED_BIN_LOW_CONFIRM_SECONDS", 600):
            new_state = feed_bin_bad_floor
    else:
        # getting better: commit to the WORST of the better levels seen
        feed_bin_bad_since = None
        if feed_bin_good_since is None:
            feed_bin_good_since = now
            feed_bin_good_ceiling = target
        elif _BIN_RANK[target] > _BIN_RANK[feed_bin_good_ceiling]:
            feed_bin_good_ceiling = target
        if now - feed_bin_good_since >= cfg("FEED_BIN_REFILL_CONFIRM_SECONDS", 60):
            new_state = feed_bin_good_ceiling

    if new_state is None or new_state == feed_bin_state:
        return

    previous = feed_bin_state
    feed_bin_state = new_state
    feed_bin_low_active = new_state != "ok"
    feed_bin_bad_since = feed_bin_good_since = None
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        if new_state == "empty":
            log_alert("Feed Container Empty", f"{percent:.0f}% left - refill now")
            push_notification({"type": "feedBinEmpty", "value": percent, "timestamp": stamp})
            send_alert_push("Feed Container Empty",
                            "The feed container is empty. Please refill it now.")
        elif new_state == "low" and previous == "ok":
            log_alert("Feed Container Low", f"{percent:.0f}% left in the container")
            push_notification({"type": "feedBinLow", "value": percent, "timestamp": stamp})
            send_alert_push("Feed Container Low",
                            f"Only {percent:.0f}% feed left in the container. Please refill.")
        elif new_state == "ok":
            log_alert("Feed Container Refilled", f"{percent:.0f}% in the container")
            push_notification({"type": "feedBinRefilled", "value": percent, "timestamp": stamp})
    except Exception as e:
        print(f"Feed container notification error: {e}")
    print(f"Feed container: {previous} -> {new_state} ({percent:.0f}%)")
    save_alert_state()


def feed_bin_label():
    """Plain words for the LCD / app: Full, Normal, Low, Empty."""
    if feed_bin_state == "empty":
        return "Empty"
    if feed_bin_state == "low":
        return "Low"
    if latest_feed_bin_percent is None:
        return "--"
    return "Full" if latest_feed_bin_percent >= cfg("FEED_BIN_FULL_PERCENT", 80) else "Normal"


def water_bin_label():
    return "Low" if container_empty_active else "Normal"


# =====================================================================
# Shared state writer (for LCD dashboard)
# =====================================================================
SHARED_STATE_FILE = "shared_state.json"


def write_shared_state(temperature, humidity, feed_weight, water_level, thresholds,
                       next_schedule=None, system_online=True, container_level=None,
                       sensor_health=None, sensor_reasons=None):
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
        "water_bin_state": "low" if container_empty_active else "ok",
        "water_bin_label": water_bin_label(),
        "water_refill_failed": "water_refill" in outcome_fault,
        "sensor_health": sensor_health if sensor_health is not None else {},
        "sensor_fault_reason": sensor_reasons if sensor_reasons is not None else {},
        "feed_bin_percent": latest_feed_bin_percent,
        "feed_bin_low_active": feed_bin_low_active,
        "feed_bin_state": feed_bin_state,
        "feed_bin_label": feed_bin_label(),
        "feed_low_threshold": thresholds.get("feedLow", 20),
        "temp_min_threshold": thresholds.get("tempMin", config.DEFAULT_TEMP_MIN),
        "temp_max_threshold": thresholds.get("tempMax", config.DEFAULT_TEMP_MAX),
        "hum_min_threshold": thresholds.get("humMin", config.DEFAULT_HUM_MIN),
        "hum_max_threshold": thresholds.get("humMax", config.DEFAULT_HUM_MAX),
        "is_dispensing_feed": feed_servo_active,
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


# =====================================================================
# Background sync (thresholds + schedules, off the main loop)
# =====================================================================
def sync_worker():
    """Refreshes cached thresholds/schedules off the main thread. Right after the
    internet comes back it refreshes every second for a few seconds."""
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


# =====================================================================
# Main loop
# =====================================================================
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
    slow_read_loops = max(1, int(config.SLOW_READ_INTERVAL // config.FAST_READ_INTERVAL))

    # Cache the last known temp/humidity so every loop iteration still
    # has a value to work with between DHT22 samples
    last_temperature = None
    last_humidity = None

    # Load both caches once, then let the background thread keep them fresh
    cached_thresholds = get_active_thresholds()
    cached_schedules = get_schedules_cached()
    threading.Thread(target=sync_worker, daemon=True).start()
    threading.Thread(target=water_watcher, daemon=True).start()

    cleanup_done = {"value": False}

    def cleanup_actuators():
        if cleanup_done["value"]:
            return
        cleanup_done["value"] = True
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

            # Slow sensors - only True this iteration if a fresh sample was taken
            new_slow_reading = False
            if loop_count % slow_read_loops == 0:
                temperature, humidity = read_dht22()
                if temperature is not None:
                    last_temperature = temperature
                    last_humidity = humidity
                    new_slow_reading = True
                    last_dht_ok_time = time.time()
                    note_reading("dht22", (temperature, humidity))

            # Feed container (ultrasonic) - same slow schedule as the DHT22
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

            thresholds = cached_thresholds or {}
            schedules = cached_schedules

            feed_percent_for_app = (
                round(min(100, (feed_weight / get_feed_capacity()) * 100), 1)
                if feed_weight is not None else None
            )

            # Health of ALL parts (one alert per fault, see check_component_health)
            check_actuator_effects(temperature, humidity, thresholds)
            check_servo_watchdog()
            check_water_container(container_level)
            health, health_reasons = check_component_health()

            push_sensor_data_async(
                temperature, humidity, feed_weight or 0, water_level,
                feed_percent=feed_percent_for_app,
                extra_updates={
                    "actuators/exhaustFan": exhaust_fan_on,
                    "actuators/heatingLamp": heating_lamp_on,
                    "actuators/feedServo": feed_servo_active,
                    "actuators/waterServo": is_dispensing_water,
                    "sensor_data/feed_low_active": feed_low_active,
                    "sensor_data/container_level": container_level,
                    "sensor_data/container_empty_active": container_empty_active,
                    "sensor_data/water_bin_state": "low" if container_empty_active else "ok",
                    "sensor_data/water_bin_label": water_bin_label(),
                    "sensor_data/water_refill_failed": "water_refill" in outcome_fault,
                    "sensor_data/sensor_health": health,
                    "sensor_data/sensor_fault_reason": health_reasons,
                    "sensor_data/feed_bin_percent": latest_feed_bin_percent,
                    "sensor_data/feed_bin_low_active": feed_bin_low_active,
                    "sensor_data/feed_bin_state": feed_bin_state,
                    "sensor_data/feed_bin_label": feed_bin_label(),
                }
            )

            # Environmental control runs every fast loop using the latest known reading
            if temperature is not None:
                control_environment(temperature, humidity, thresholds)

            # Alerts (temp/humidity debounce on real new samples; plate feed needs a long low period)
            check_alerts(None if is_dispensing_feed else feed_weight, water_level,
                         temperature, humidity, thresholds,
                         new_slow_reading=new_slow_reading)

            # Feed container alert (ultrasonic): low / empty, confirmed over time
            check_feed_bin_alert(latest_feed_bin_percent, new_bin_reading)

            # (Water refills run in water_watcher(), its own thread)

            check_schedules_with_data(schedules)

            # Shared state file for the LCD. sensor_health is the DEBOUNCED health, same as the app.
            next_sched = get_next_schedule_info(schedules)
            write_shared_state(temperature, humidity, feed_weight, water_level, thresholds,
                               next_schedule=next_sched, container_level=container_level,
                               sensor_health=health, sensor_reasons=health_reasons)

            loop_count += 1
            time.sleep(config.FAST_READ_INTERVAL)

    except KeyboardInterrupt:
        cleanup_actuators()


if __name__ == "__main__":
    main()