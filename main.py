import time
import json
import statistics
import threading
from datetime import datetime, timedelta
import RPi.GPIO as GPIO

from firebase_service import (
    init_firebase, push_sensor_data, get_thresholds,
    get_schedules, update_actuator_state, write_log, send_alert_push,
    update_feed_alert_state
)
from sensors.dht22 import read_dht22
from sensors.load_cell import setup_hx711, read_grams, update_stable_grams, reset_stable_grams
from sensors.float_sensor import setup_float_sensor, read_water_level
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

SCHEDULE_CACHE_FILE = "schedule_cache.json"
THRESHOLD_CACHE_FILE = "threshold_cache.json"
ALERT_STATE_FILE = "alert_state_cache.json"
DISPENSE_RATE_CACHE_FILE = "dispense_rate_cache.json"

THRESHOLD_REFRESH_INTERVAL = 30  
threshold_refresh_loops = THRESHOLD_REFRESH_INTERVAL // config.FAST_READ_INTERVAL

SCHEDULE_REFRESH_INTERVAL = 1
schedule_refresh_loops = SCHEDULE_REFRESH_INTERVAL // config.FAST_READ_INTERVAL

cached_thresholds = None
cached_schedules = None

fired_schedules_this_minute = set()
last_checked_minute = None

# Alert active flags
feed_low_active = False
water_low_active = False     
temp_high_active = False
temp_low_active = False
hum_high_active = False

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

def get_feed_capacity():
    return (cached_thresholds or {}).get("feedCapacityGrams") or config.FEED_CAPACITY_GRAMS

#  Threshold Logic 
def get_active_thresholds():
    # Try Firebase (live, most up-to-date)
    try:
        thresholds = get_thresholds()
        if thresholds:
            # Normalize feedLow / feedlow key mismatch from the app -
            if "feedlow" in thresholds:
                thresholds["feedLow"] = thresholds["feedlow"]

            try:
                with open(THRESHOLD_CACHE_FILE, "w") as f:
                    json.dump(thresholds, f)
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
    # Fully independent of the heating lamp — humidity above
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

    # Don't let the fan shut off again the instant conditions dip —
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
    eliminate — a bird standing there the whole time will still fool it)
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


def dispense_feed(target_grams, triggered_by="schedule", schedule_key=None):
    global is_dispensing_feed, last_feeding_progress
    if is_dispensing_feed:
        return

    capacity = get_feed_capacity()
    current_weight = latest_feed_weight if latest_feed_weight is not None else read_grams(capacity=capacity)
    start_weight = current_weight if current_weight is not None else 0

    # target_grams is always the ABSOLUTE amount that should be on the plate
    # when dispensing finishes — independent of what's already there.
    target_grams = min(target_grams, capacity)
    
    if start_weight >= target_grams:
        print(f"Plate already at {start_weight}g (capacity {capacity}g) - skipping, no dispense needed")
        return

    amount_requested = target_grams - start_weight

    min_rate = getattr(config, "DISPENSE_MIN_RATE_GPS", 0.3)
    avg_rate_for_buffer = max(min_rate, _load_avg_dispense_rate())
    settle_seconds = getattr(config, "DISPENSE_SETTLE_SECONDS", 0.6)  # time feed keeps falling after close, in seconds — measure this once by eye
    dynamic_buffer = max(config.DISPENSE_OVERSHOOT_BUFFER_GRAMS, avg_rate_for_buffer * settle_seconds)
    stop_early_at = target_grams - dynamic_buffer

    is_dispensing_feed = True
    dispense_start = time.time()
    try:
        print(f"Dispensing feed: {start_weight}g -> target {target_grams}g (adding {amount_requested}g, closing early at {stop_early_at}g)")
        update_actuator_state("feedServo", True)

        timeout = time.time() + config.FEED_DISPENSE_TIMEOUT_SECONDS
        current_weight = start_weight
        measured_rate = None
        total_open_seconds = 0.0

        # ---------- Bulk phase ----------
        bulk_fraction = getattr(config, "DISPENSE_BULK_FRACTION", 0.2)
        bulk_max_seconds = getattr(config, "DISPENSE_BULK_MAX_SECONDS", 5)

        avg_rate = max(min_rate, _load_avg_dispense_rate())
        bulk_grams_target = amount_requested * bulk_fraction
        bulk_seconds = min(bulk_max_seconds, bulk_grams_target / avg_rate)

        if bulk_seconds > 0.1:
            print(f"  Bulk phase: opening {bulk_seconds:.2f}s (~{bulk_grams_target:.0f}g @ {avg_rate:.1f}g/s est.)")
            bulk_pulse_start = time.time()
            open_feed()
            time.sleep(bulk_seconds)
            close_feed()
            actual_bulk_time = time.time() - bulk_pulse_start
            total_open_seconds += actual_bulk_time
            time.sleep(config.SERVO_CLOSE_LATENCY_SECONDS)

            settled = _read_settled_grams(capacity)
            if settled is not None:
                current_weight = settled
                bulk_dispensed = max(0.0, current_weight - start_weight)
                if actual_bulk_time > 0 and bulk_dispensed > 0:
                    measured_rate = bulk_dispensed / actual_bulk_time
                print(f"  Bulk result: +{bulk_dispensed:.1f}g in {actual_bulk_time:.2f}s"
                      + (f" (~{measured_rate:.1f}g/s)" if measured_rate else " (rate unclear, keeping estimate)"))

        # ---------- Adaptive fine-tune phase ----------
        # Small self-correcting pulses to land on the exact gram target,
        # seeded with the rate just measured from the bulk phase (a real,
        # multi-second measurement) instead of a noisy 0.08s guess.
        if measured_rate is None:
            measured_rate = avg_rate

        while time.time() < timeout and current_weight < stop_early_at:
            remaining = stop_early_at - current_weight

            if remaining <= 3:
                correction = 0.3   # final fine-tuning pulses: tiny bites
            elif remaining <= 10:
                correction = 0.5
            else:
                # Higher than the old 0.08 default: measured_rate now comes
                # from a multi-second bulk measurement, not a 0.08s probe,
                # so it's trustworthy enough to take bigger corrective bites.
                correction = 0.4

            pulse_seconds = min(1.0, max(0.03, (remaining / measured_rate) * correction))

            before = current_weight
            pulse_start = time.time()

            open_feed()
            time.sleep(pulse_seconds)
            close_feed()

            actual_pulse_time = time.time() - pulse_start
            total_open_seconds += actual_pulse_time
            time.sleep(config.SERVO_CLOSE_LATENCY_SECONDS)  # let settling/momentum finish before measuring

            current_weight = read_grams(samples=8, trust_reading=True, capacity=capacity)
            if current_weight is None:
                current_weight = before

            dispensed_this_pulse = current_weight - before
            if actual_pulse_time > 0 and dispensed_this_pulse > 0:
                # Update the measured rate every pulse, so it keeps adapting
                # as the hopper empties or feed settles differently.
                measured_rate = dispensed_this_pulse / actual_pulse_time

            print(f"  Pulse {pulse_seconds:.2f}s -> +{dispensed_this_pulse:.1f}g, plate now {current_weight}g "
                  f"(rate ~{measured_rate:.1f}g/s)")

            if current_weight >= stop_early_at:
                break

        update_actuator_state("feedServo", False)
        reached_target = current_weight >= stop_early_at
        if not reached_target and time.time() >= timeout:
            print(f"Feed fail-safe: {target_grams}g not reached after {config.FEED_DISPENSE_TIMEOUT_SECONDS}s. Check hopper, servo and load cell.")

        final_weight = _read_settled_grams(capacity)
        if final_weight is None:
            final_weight = current_weight
        reset_stable_grams(final_weight)

        dispensed = max(0.0, round(final_weight - start_weight, 1))
        log_feed(dispensed, triggered_by)

        # Learn from the whole dispense (bulk + fine-tune combined) so the
        # bulk-phase estimate keeps improving over successive feedings.
        if total_open_seconds > 0 and dispensed > 0:
            _save_avg_dispense_rate(dispensed / total_open_seconds)

        feeding_percent = round(min(100, (dispensed / amount_requested) * 100))
        last_feeding_progress = {
            "percent": feeding_percent,
            "grams": dispensed,
            "target_grams": amount_requested,
            "triggered_by": triggered_by,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        try:
            db.reference("/notifications").push({
                "type": "feedDispensed",
                "amount": dispensed,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            db.reference('/sensor_data').update({'last_feed_time': int(time.time() * 1000)})
            db.reference('/sensor_data').update({'feeding_progress': last_feeding_progress})
        except Exception as e:
            print(f"Firebase write failed after dispense (feed already dispensed OK): {e}")

        print(f"Feed dispensed: {dispensed}g added ({feeding_percent}% of {amount_requested}g), plate now {final_weight}g")

    finally:
        is_dispensing_feed = False
        
#  Water Dispensing 
def dispense_water(triggered_by="schedule"):
    global is_dispensing_water
    if is_dispensing_water:
        return

    current_level = latest_water_level if latest_water_level is not None else read_water_level()
    if current_level in ("normal", "full"):
        print(f"Water already '{current_level}' - skipping, no dispense needed")
        return

    is_dispensing_water = True
    try:
        print(f"Dispensing water: current level '{current_level}'")
        update_actuator_state("waterServo", True)
        open_water()

        # Manual and LCD dispenses use a shorter safety cutoff than scheduled ones
        timeout_seconds = (
            config.WATER_DISPENSE_TIMEOUT_MANUAL_SECONDS
            if triggered_by in ("manual", "lcd")
            else config.WATER_DISPENSE_TIMEOUT_SECONDS
        )
        timeout = time.time() + timeout_seconds

        reached_normal = False
        normal_confirm_count = 0

        while time.time() < timeout:
            level = read_water_level()
            if level == "normal":
                normal_confirm_count += 1
                if normal_confirm_count >= config.WATER_NORMAL_CONFIRM_READS:
                    print("Water reached normal (confirmed). Topping up...")
                    time.sleep(config.WATER_EXTRA_FILL_TIME)
                    reached_normal = True
                    break
            else:
                normal_confirm_count = 0
            time.sleep(0.3)

        close_water()
        update_actuator_state("waterServo", False)

        if not reached_normal:
            print(f"Water dispense timed out after {timeout_seconds}s without reaching 'normal' - check float sensor/servo")

        log_water(triggered_by) 

        try:
            db.reference("/notifications").push({
                "type": "waterDispensed",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
        except Exception as e:
            print(f"Firebase write failed after dispense (water already dispensed OK): {e}")

        print("Water dispensed")
    finally:
        is_dispensing_water = False
    
#  Combined Sequential Dispense 
def run_dispense_sequence(feed_grams=None, do_water=False, triggered_by="schedule", schedule_key=None):
    global is_dispensing
    if is_dispensing:
        print("Dispense already in progress, skipping.")
        return
    is_dispensing = True
    try:
        if feed_grams:
            dispense_feed(feed_grams, triggered_by, schedule_key)   # blocks until feed finishes
            if do_water:
                print(f"Feed done - waiting {config.DISPENSE_STAGE_INTERVAL_SECONDS}s before water...")
                time.sleep(config.DISPENSE_STAGE_INTERVAL_SECONDS)
        if do_water:
            dispense_water(triggered_by)              # only starts after feed (+ pause) is done
    finally:
        is_dispensing = False

#  Schedule Fetching (offline cache) 
def get_schedules_cached():
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
        with open(SCHEDULE_CACHE_FILE, "w") as f:
            json.dump(schedules or {}, f)
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

            threading.Thread(
                target=run_dispense_sequence,
                args=(amount, True, "schedule", key)
            ).start()

            fired_schedules_this_minute.add(key)

    except Exception as e:
        print(f"Schedule check error: {e}")

#  Manual Dispense Listener
def check_manual_commands():
    ref = db.reference('/sensor_data')
    try:
        data = ref.get()
    except Exception as e:
        print(f"Manual command check error (read): {e}")
        return

    if not data:
        return

    feed_triggered = data.get('feeder_active') == True
    water_triggered = data.get('water_dispenser') == True

    target = None
    if feed_triggered:
        try:
            manual_grams = data.get('manual_dispense_grams') or 0
            ref.update({'feeder_active': False, 'manual_dispense_grams': 0})
            if manual_grams <= 0:
                print("Manual feed pressed with no grams set in the app - skipping")
                feed_triggered = False
            else:
                current = latest_feed_weight
                if current is None:
                    current = read_grams(samples=3, trust_reading=True, capacity=get_feed_capacity()) or 0
                target = current + manual_grams
                print(f"Manual feed: current {current}g + {manual_grams}g -> target {target}g")
        except Exception as e:
            print(f"Manual feed trigger error: {e}")
            feed_triggered = False

    if water_triggered:
        try:
            ref.update({'water_dispenser': False})
        except Exception as e:
            print(f"Manual water trigger error: {e}")
            water_triggered = False

    if feed_triggered or water_triggered:
        threading.Thread(
            target=run_dispense_sequence,
            args=(target, water_triggered, 'manual')
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
    water_triggered = data.get("water_dispense", {}).get("requested", False)

    if not (feed_requested or water_triggered):
        return

    # Clear the command file immediately so the LCD doesn't keep trying to trigger it
    try:
        with open(LCD_COMMANDS_FILE, "w") as f:
            json.dump({
                "feed_dispense": {"requested": False, "grams": 0},
                "water_dispense": {"requested": False}
            }, f)
    except Exception as e:
        print(f"LCD command file reset error: {e}")

    target = None
    if feed_requested:
        manual_grams = data.get("feed_dispense", {}).get("grams") or 0
        if manual_grams > 0:
            current = latest_feed_weight
            if current is None:
                current = read_grams(samples=3, trust_reading=True, capacity=get_feed_capacity()) or 0
            target = current + manual_grams
            print(f"LCD manual feed: current {current}g + {manual_grams}g -> target {target}g")
        else:
            print("LCD feed pressed with no grams set - skipping")

    if target is not None or water_triggered:
        threading.Thread(
            target=run_dispense_sequence,
            args=(target, water_triggered, "lcd")
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
    global feed_low_active, water_low_active
    global temp_high_active, temp_low_active, hum_high_active
    global feed_low_pending, feed_clear_pending
    global temp_low_pending, temp_low_clear_pending
    global temp_high_pending, temp_high_clear_pending
    global hum_high_pending, hum_high_clear_pending

    try:
        with open(ALERT_STATE_FILE, "r") as f:
            state = json.load(f)
        feed_low_active = state.get("feed_low_active", False)
        water_low_active = state.get("water_low_active", False)
        temp_high_active = state.get("temp_high_active", False)
        temp_low_active = state.get("temp_low_active", False)
        hum_high_active = state.get("hum_high_active", False)
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
        "water_low_active": water_low_active,
        "temp_high_active": temp_high_active,
        "temp_low_active": temp_low_active,
        "hum_high_active": hum_high_active,
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


def check_alerts(feed_weight, water_level, temperature, humidity, thresholds, new_slow_reading=False):
    global feed_low_active, water_low_active
    global temp_high_active, temp_low_active, hum_high_active
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
    # Temperature / Humidity — counters only advance on an actual new DHT22
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
                    db.reference("/notifications").push({
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
                    db.reference("/notifications").push({
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
                    db.reference("/notifications").push({
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
    # Feed Level — feed_weight is a real per-second reading, so this
    # debounces directly against load-cell noise.
    # -------------------------
    if feed_weight is not None:
        feed_percent = (feed_weight / get_feed_capacity()) * 100
        feed_hysteresis = getattr(config, "FEED_HYSTERESIS_PERCENT", 5)

        # Feed became LOW — requires feed_confirm consecutive low readings
        # before firing, so a single noisy dip (or a chicken briefly
        # standing on the load cell) can't trigger a false alert. Once
        # active, dropping further (18%, 10%, etc.) does NOT fire again.
        if feed_percent < feed_low_threshold:
            feed_clear_pending = 0
            if not feed_low_active:
                feed_low_pending += 1
                if feed_low_pending >= feed_confirm:
                    feed_low_active = True
                    feed_low_pending = 0
                    log_alert("Low Feed", f"{feed_weight:.0f} g remaining ({feed_percent:.0f}%)")
                    db.reference("/notifications").push({
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

        # Feed restored — must climb feed_hysteresis% above the threshold
        # AND hold it for feed_confirm consecutive readings to clear, not
        # just barely tick over it for one instant.
        else:
            feed_low_pending = 0
            if feed_low_active and feed_percent >= (feed_low_threshold + feed_hysteresis):
                feed_clear_pending += 1
                if feed_clear_pending >= feed_confirm:
                    feed_low_active = False
                    feed_clear_pending = 0
                    log_alert("Feed Restocked", f"{feed_weight:.0f} g available ({feed_percent:.0f}%)")
                    update_feed_alert_state(False)
                    print(f"Feed Restocked: {feed_weight:.0f} g ({feed_percent:.0f}%)")
            else:
                feed_clear_pending = 0

    # -------------------------
    # Water Level
    # -------------------------
    # Left as a direct low/normal check (no debounce) - the float
    # sensor is discrete/categorical rather than a noisy continuous
    # reading, so there's no threshold edge for it to flap around.
    current_low = str(water_level).lower() == "low"


    if current_low and not water_low_active:
        
        water_low_active = True
        log_alert("Low Water", "Refill needed")

        db.reference("/notifications").push({
            "type": "lowWater",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        send_alert_push("Low Water Alert", "Water level is low.")
        print("Low Water")


    elif not current_low and water_low_active:

        water_low_active = False

        log_alert("Water Restored", "Water level normal")

        print(" Water Restored")

    # Persist active-flag + debounce-counter state so a restart doesn't
    # re-fire alerts that are already active, or lose in-progress debounce.
    save_alert_state()

# Shared State Writer (for LCD dashboard) 
SHARED_STATE_FILE = "shared_state.json"        

def write_shared_state(temperature, humidity, feed_weight, water_level, thresholds, next_schedule=None, system_online=True):
    feed_percent = (feed_weight / get_feed_capacity() * 100) if feed_weight is not None else None
    
    state = {
        "temperature": temperature,
        "humidity": humidity,
        "feed_weight": feed_weight,
        "feed_low_active": feed_low_active,
        "feed_percent": feed_percent,          # hopper-capacity % (low-feed alert basis, unchanged)
        "feed_capacity_grams": get_feed_capacity(), 
        "feeding_progress": last_feeding_progress,  # per-feeding target % (e.g. 100g = 100%)
        "water_level": water_level,
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
        "next_schedule": next_schedule,
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    try:
        with open(SHARED_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"Shared state write failed: {e}")

# Main Loop 
def main():
    print("Initializing PoultryCare system...")
    global cached_thresholds, cached_schedules
    init_firebase()
    load_alert_state()
    update_feed_alert_state(feed_low_active)
    setup_relay()
    setup_hx711()
    setup_float_sensor()
    setup_servos()
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
    last_temperature = None
    last_humidity = None

    try:
        while True:

            # Fast sensors: read every loop 
            instant_grams = read_grams(capacity=get_feed_capacity())
            feed_weight = instant_grams
            water_level = read_water_level()

            global latest_feed_weight, latest_water_level   # if not already global in this scope
            if instant_grams is not None:
                latest_feed_weight = instant_grams
            latest_water_level = water_level

            # Slow sensor — only True this iteration if a fresh DHT22 sample
            # was actually taken (used to gate the temp/humidity alert debounce).
            new_slow_reading = False
            if loop_count % slow_read_loops == 0:
                temperature, humidity = read_dht22()
                if temperature is not None:
                    last_temperature = temperature
                    last_humidity = humidity
                    new_slow_reading = True

            temperature = last_temperature
            humidity = last_humidity

            feed_display = f"{feed_weight:.1f}g" if feed_weight is not None else "N/A"
            print(f"Temp: {temperature}C | Humidity: {humidity}% | "
                f"Feed: {feed_display} | Water: {water_level}")

            # Calculate once here, then push in the SAME call as the rest of
            # the sensor data - one write, one round trip, instead of two.
            feed_percent_for_app = (
                round(min(100, (feed_weight / get_feed_capacity()) * 100), 1)
                if feed_weight is not None else None
            )
            
            push_sensor_data(
                temperature, humidity, feed_weight or 0, water_level,
                feed_percent=feed_percent_for_app,
                extra_updates={"actuators/exhaustFan": exhaust_fan_on}  # only when it changed this tick
            )
           
            # Only hit Firebase for thresholds/schedules every ~30s, not every loop
            if loop_count % threshold_refresh_loops == 0 or cached_thresholds is None:
                cached_thresholds = get_active_thresholds()

            if loop_count % schedule_refresh_loops == 0 or cached_schedules is None:
                cached_schedules = get_schedules_cached()

            thresholds = cached_thresholds
            schedules = cached_schedules

            # Environmental control runs every fast loop using the latest
            # known reading — hysteresis timing and threshold changes
            # apply promptly, even though the DHT22 itself only refreshes
            # on the slow interval.
            if temperature is not None:
                control_environment(temperature, humidity, thresholds)

            # Check alerts every fast loop (debounce logic inside gates
            # temp/humidity on new_slow_reading; feed debounces every loop)
            check_alerts(feed_weight, water_level, temperature, humidity, thresholds, new_slow_reading=new_slow_reading)

            # Publish the debounced flag itself, so the app and LCD show
            # the SAME "low" state as this function just computed, instead of
            # each recomputing feed_percent < threshold on their own with no
            # debounce memory.
            try:
                db.reference('/sensor_data').update({'feed_low_active': feed_low_active})
            except Exception as e:
                print(f"Feed alert state Firebase write failed: {e}")

            # Check feeding schedules every fast loop 
            check_schedules_with_data(schedules)

            # Update shared state file for the LCD dashboard
            next_sched = get_next_schedule_info(schedules)
            write_shared_state(temperature, humidity, feed_weight, water_level, thresholds, next_schedule=next_sched)

            loop_count += 1
            time.sleep(config.FAST_READ_INTERVAL)

    except KeyboardInterrupt:
        print("\nShutting down PoultryCare...")
        db.reference('/sensor_data/system_online').set(False)
        turn_off_heating_lamp()
        turn_off_exhaust_fan()
        cleanup()
        cleanup_servos()
        print("Shutdown complete")

if __name__ == "__main__":
    main()