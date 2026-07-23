import time
import json
import threading
from datetime import datetime, timedelta
import RPi.GPIO as GPIO

from firebase_service import (
    init_firebase, push_sensor_data, get_thresholds,
    get_schedules, update_actuator_state, write_log, send_alert_push
)
from sensors.dht22 import read_dht22
from sensors.load_cell import setup_hx711, read_grams
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
is_dispensing = False  # NEW: master lock so feed+water never overlap

SCHEDULE_CACHE_FILE = "schedule_cache.json"
THRESHOLD_CACHE_FILE = "threshold_cache.json"

fired_schedules_this_minute = set()
last_checked_minute = None

feed_low_active = False
water_low_active = False     

exhaust_fan_last_on_time = None

#  Threshold Logic 
def get_active_thresholds():
    # Try Firebase (live, most up-to-date)
    try:
        thresholds = get_thresholds()
        if thresholds:
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
        "feedLow": 100
    }

#  Environmental Control 
def control_environment(temperature, humidity, thresholds):
    global heating_lamp_on, exhaust_fan_on

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
    # -----------------------------
    if exhaust_fan_on:
        need_exhaust = (
            temperature > (temp_max - config.TEMP_HYSTERESIS)
            or (humidity is not None and humidity > hum_max)
        )
    else:
        need_exhaust = (
            temperature > temp_max
            or (humidity is not None and humidity > hum_max)
        )

    hum_critical = hum_max + config.HUM_CRITICAL_BUFFER
    humidity_critical = humidity is not None and humidity >= hum_critical

    if need_heat and not (temperature > temp_max) and not humidity_critical:
        # Suppress exhaust for ordinary high humidity while heating —
        # but let genuinely critical humidity override, since prolonged
        # high humidity is a health risk regardless of temperature.
        need_exhaust = False

    # Don't let the fan shut off again the instant temp dips —
    # give it a minimum run time to actually vent moisture out.
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

        db.reference("/actuators").update({
            "heatingLamp": True
        })

        log_actuator(
            "Heating Lamp",
            "ON",
            f"Temperature dropped to {temperature:.1f}°C (below {temp_min}°C)"
        )

        print(f"🔥 Heating Lamp ON ({temperature:.1f}°C)")

    elif not need_heat and heating_lamp_on:

        turn_off_heating_lamp()
        heating_lamp_on = False

        update_actuator_state("heatingLamp", False)

        db.reference("/actuators").update({
            "heatingLamp": False
        })

        log_actuator(
            "Heating Lamp",
            "OFF",
            f"Temperature recovered to {temperature:.1f}°C"
        )

        print(f"🟢 Heating Lamp OFF ({temperature:.1f}°C)")

    # =========================================
    # Exhaust Fan
    # =========================================

    if need_exhaust and not exhaust_fan_on:

        turn_on_exhaust_fan()
        exhaust_fan_on = True
        exhaust_fan_last_on_time = time.time()

        update_actuator_state("exhaustFan", True)

        db.reference("/actuators").update({
            "exhaustFan": True
        })

        reason = (
            f"Temperature reached {temperature:.1f}°C (above {temp_max}°C)"
            if temperature > temp_max
            else f"Humidity reached {humidity:.1f}% (above {hum_max}%)"
        )

        log_actuator(
            "Cooling Fan",
            "ON",
            reason
        )

        print(f"❄ Cooling Fan ON ({temperature:.1f}°C)")

    elif not need_exhaust and exhaust_fan_on:

        turn_off_exhaust_fan()
        exhaust_fan_on = False

        update_actuator_state("exhaustFan", False)

        db.reference("/actuators").update({
            "exhaustFan": False
        })

        log_actuator(
            "Cooling Fan",
            "OFF",
            f"Environment returned to normal ({temperature:.1f}°C)"
        )

        print(f"🟢 Cooling Fan OFF ({temperature:.1f}°C)")

#  Feed Dispensing 
def dispense_feed(target_grams, triggered_by="schedule"):
    global is_dispensing_feed
    if is_dispensing_feed:
        return

    current_weight = read_grams()
    if current_weight is not None and current_weight >= target_grams:
        print(f"Feed already at {current_weight}g (target {target_grams}g) - skipping, no dispense needed")
        return

    is_dispensing_feed = True
    try:
        stop_early_at = target_grams - config.DISPENSE_OVERSHOOT_BUFFER_GRAMS
        print(f"Dispensing feed: {current_weight}g -> target {target_grams}g (closing early at {stop_early_at}g)")
        update_actuator_state("feedServo", True)
        open_feed()

        timeout = time.time() + 30
        while time.time() < timeout:
            weight = read_grams()
            if weight is not None and weight >= stop_early_at:
                break
            time.sleep(0.5)

        close_feed()
        update_actuator_state("feedServo", False)
        final_weight = read_grams()
        log_feed(final_weight, triggered_by)

        try:
            db.reference("/notifications").push({
                "type": "feedDispensed",
                "amount": final_weight,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            db.reference('/sensor_data').update({'last_feed_time': int(time.time() * 1000)})
        except Exception as e:
            print(f"Firebase write failed after dispense (feed already dispensed OK): {e}")

        print(f"Feed dispensed: {final_weight}g")
    finally:
        is_dispensing_feed = False
        
#  Water Dispensing 
def dispense_water(triggered_by="schedule"):
    global is_dispensing_water
    if is_dispensing_water:
        return

    current_level = read_water_level()
    if current_level in ("normal", "full"):
        print(f"Water already '{current_level}' - skipping, no dispense needed")
        return

    is_dispensing_water = True
    try:
        print(f"Dispensing water: current level '{current_level}'")
        update_actuator_state("waterServo", True)
        open_water()

        timeout = time.time() + 30
        reached_normal = False

        while time.time() < timeout:
            level = read_water_level()
            if level == "normal":
                print("Water reached normal. Topping up...")
                time.sleep(config.WATER_EXTRA_FILL_TIME)
                reached_normal = True
                break
            time.sleep(0.5)

        close_water()
        update_actuator_state("waterServo", False)

        if not reached_normal:
            print("Water dispense timed out after 30s without reaching 'normal' - check float sensor/servo")

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
def run_dispense_sequence(feed_grams=None, do_water=False, triggered_by="schedule"):
    global is_dispensing
    if is_dispensing:
        print("Dispense already in progress, skipping.")
        return
    is_dispensing = True
    try:
        if feed_grams:
            dispense_feed(feed_grams, triggered_by)   # blocks until feed finishes
        if do_water:
            dispense_water(triggered_by)              # only starts after feed is done
    finally:
        is_dispensing = False

#  Schedule Fetching (offline cache) 
def get_schedules_cached():
    # Try Firebase (live, up-to-date)
    try:
        schedules = get_schedules()
        if schedules:
            try:
                with open(SCHEDULE_CACHE_FILE, "w") as f:
                    json.dump(schedules, f)
            except Exception as e:
                print(f"Schedule cache write failed: {e}")
            return schedules
    except Exception as e:
        print(f"Schedule fetch failed, checking cache: {e}")

    #  Firebase failed - fall back to last-known synced schedule
    try:
        with open(SCHEDULE_CACHE_FILE, "r") as f:
            cached = json.load(f)
            print("Using cached schedules (last known from app)")
            return cached
    except Exception:
        print("No cached schedules available")
        return None

# Find Next Scheduled Feed (for LCD display)
def get_next_schedule_info(schedules):
    if not schedules:
        return None

    now = datetime.now()
    day_map = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    best = None  # (minutes_from_now, time_str, amount)

    for key, entry in schedules.items():
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue

        sched_time = entry.get("time")  
        if not sched_time:
            continue

        days_str = entry.get("days", "Mon,Tue,Wed,Thu,Fri,Sat,Sun")
        days_list = [d.strip() for d in days_str.split(",")]
        amount = entry.get("amount_grams", 500)

        try:
            sched_hour, sched_min = map(int, sched_time.split(":"))
        except Exception:
            continue

        # Check the next 7 days starting today to find the closest upcoming schedule
        for offset in range(7):
            check_day = (now.weekday() + offset) % 7
            if day_map[check_day] not in days_list:
                continue

            candidate = now.replace(hour=sched_hour, minute=sched_min, second=0, microsecond=0)
            candidate = candidate.replace(day=candidate.day)  
            candidate_full = candidate + timedelta(days=offset)

            if candidate_full <= now:
                continue  # today but already passed - skip to next matching day

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

            amount = entry.get("amount_grams", 500)

            threading.Thread(
                target=run_dispense_sequence,
                args=(amount, True, "schedule")
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
            manual_grams = data.get('manual_dispense_grams', config.DEFAULT_MANUAL_DISPENSE_GRAMS)
            ref.update({'feeder_active': False, 'manual_dispense_grams': 0})
            current = read_grams() or 0
            target = current + manual_grams  # ADD grams on top of current level
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

    feed_triggered = data.get("feed_dispense", {}).get("requested", False)
    water_triggered = data.get("water_dispense", {}).get("requested", False)

    target = None
    if feed_triggered:
        try:
            manual_grams = data.get("feed_dispense", {}).get("grams", config.DEFAULT_MANUAL_DISPENSE_GRAMS)
            current = read_grams() or 0
            target = current + manual_grams
            print(f"LCD manual feed: current {current}g + {manual_grams}g -> target {target}g")
        except Exception as e:
            print(f"LCD feed trigger error: {e}")
            feed_triggered = False

    if feed_triggered or water_triggered:
        # Clear the command file immediately so the LCD doesn't keep trying to trigger it
        try:
            with open(LCD_COMMANDS_FILE, "w") as f:
                json.dump({
                    "feed_dispense": {"requested": False, "grams": 0},
                    "water_dispense": {"requested": False}
                }, f)
        except Exception as e:
            print(f"LCD command file reset error: {e}")

        threading.Thread(
            target=run_dispense_sequence,
            args=(target, water_triggered, "lcd")
        ).start()
 
#  Manual Command Watcher (fast, independent of the sensor loop) 
def manual_command_watcher():
    poll_interval = getattr(config, 'MANUAL_COMMAND_POLL_INTERVAL', 0.5)
    while True:
        try:
            check_manual_commands()
        except Exception as e:
            print(f"Manual command watcher error: {e}")
        try:
            check_lcd_commands()
        except Exception as e:
            print(f"LCD command watcher error: {e}")
        time.sleep(poll_interval)

#  Alert Checker
def check_alerts(feed_weight, water_level, thresholds):
    global feed_low_active, water_low_active

    feed_low_threshold = thresholds.get("feedLow", 100)

    # -------------------------
    # Feed Level
    # -------------------------
    if feed_weight is not None:

        # Feed became LOW
        if feed_weight < feed_low_threshold and not feed_low_active:
            feed_low_active = True

            log_alert("Low Feed", f"{feed_weight:.0f} g remaining")

            db.reference("/notifications").push({
                "type": "lowFeed",
                "value": feed_weight,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })

            send_alert_push(
                "Low Feed Alert",
                f"Feed level is low ({feed_weight:.0f} g remaining)"
            )

            print(f"⚠ Low Feed: {feed_weight:.0f} g")

        # Feed restored
        elif feed_weight >= feed_low_threshold and feed_low_active:
            feed_low_active = False

            log_alert("Feed Restocked", f"{feed_weight:.0f} g available")

            print(f"✅ Feed Restocked: {feed_weight:.0f} g")

    # -------------------------
    # Water Level
    # -------------------------
    current_low = str(water_level).lower() == "low"

    # Water became LOW
    if current_low and not water_low_active:
        water_low_active = True

        log_alert("Low Water", "Refill needed")

        db.reference("/notifications").push({
            "type": "lowWater",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

        send_alert_push(
            "Low Water Alert",
            "Water level is low."
        )

        print("⚠ Low Water")

    # Water restored
    elif not current_low and water_low_active:
        water_low_active = False

        log_alert("Water Restored", "Water level normal")

        print("✅ Water Restored")

# Shared State Writer (for LCD dashboard) 
SHARED_STATE_FILE = "shared_state.json"        

def write_shared_state(temperature, humidity, feed_weight, water_level, thresholds, next_schedule=None, system_online=True):
    state = {
        "temperature": temperature,
        "humidity": humidity,
        "feed_weight": feed_weight,
        "water_level": water_level,
        "feed_low_threshold": thresholds.get("feedLow", 100),
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
    init_firebase()
    setup_relay()
    setup_hx711()
    setup_float_sensor()
    setup_servos()
    print("All systems initialized. Starting main loop...")
    
    threading.Thread(target=manual_command_watcher, daemon=True).start()

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
            feed_weight = read_grams()
            water_level = read_water_level()

            # Slow sensor
            if loop_count % slow_read_loops == 0:
                temperature, humidity = read_dht22()
                if temperature is not None:
                    last_temperature = temperature
                    last_humidity = humidity

            temperature = last_temperature
            humidity = last_humidity

            print(f"Temp: {temperature}C | Humidity: {humidity}% | "
                  f"Feed: {feed_weight}g | Water: {water_level}")

            # Push to Firebase every fast loop
            push_sensor_data(temperature, humidity, feed_weight or 0, water_level)

            # Environmental control only needs to run when temp/humidity
            if loop_count % slow_read_loops == 0 and temperature is not None:
                thresholds = get_active_thresholds()
                control_environment(temperature, humidity, thresholds)
            else:
                thresholds = get_active_thresholds()

            # Check alerts every fast loop 
            check_alerts(feed_weight, water_level, thresholds)

            # Check feeding schedules every fast loop 
            schedules = get_schedules_cached()
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
