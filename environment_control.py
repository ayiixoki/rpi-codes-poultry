import time
from sensors.dht22 import read_dht22
from actuators.relay_control import (
    setup_relay, turn_on_heating_lamp, turn_off_heating_lamp,
    turn_on_exhaust_fan, turn_off_exhaust_fan, cleanup
)
from firebase_service import init_firebase, push_sensor_data, get_thresholds, update_actuator_state, write_log
import config

def get_active_thresholds():
    """Fetch thresholds from Firebase, fallback to config defaults."""
    thresholds = get_thresholds()
    if thresholds:
        return thresholds
    return {
        "tempMin": config.DEFAULT_TEMP_MIN,
        "tempMax": config.DEFAULT_TEMP_MAX,
        "humMin": config.DEFAULT_HUM_MIN,
        "humMax": config.DEFAULT_HUM_MAX
    }

def control_environment(temperature, humidity, thresholds):
    if temperature is None:
        return
    temp_min = thresholds.get("tempMin", config.DEFAULT_TEMP_MIN)
    temp_max = thresholds.get("tempMax", config.DEFAULT_TEMP_MAX)
    hum_max  = thresholds.get("humMax", config.DEFAULT_HUM_MAX)

    need_heat    = temperature < temp_min
    need_exhaust = temperature > temp_max or (humidity is not None and humidity > hum_max)

    if need_heat:
        turn_on_heating_lamp()
        update_actuator_state("heatingLamp", True)
        log_actuator("heatingLamp", "ON")
    else:
        turn_off_heating_lamp()
        update_actuator_state("heatingLamp", False)

    if need_exhaust:
        turn_on_exhaust_fan()
        update_actuator_state("exhaustFan", True)
        log_actuator("exhaustFan", "ON")
    else:
        turn_off_exhaust_fan()
        update_actuator_state("exhaustFan", False)

def main():
    print("Initializing Firebase...")
    init_firebase()
    print("Setting up relay...")
    setup_relay()

    print("Starting adaptive environmental control loop...")
    try:
        while True:
            temperature, humidity = read_dht22()

            if temperature is not None and humidity is not None:
                print(f"Temp: {temperature:.1f}C  Humidity: {humidity:.1f}%")
                push_sensor_data(temperature, humidity, 0, "normal")

                thresholds = get_active_thresholds()
                control_environment(temperature, humidity, thresholds)
            else:
                print("Sensor read failed, retrying...")

            time.sleep(config.SENSOR_READ_INTERVAL)

    except KeyboardInterrupt:
        print("\nStopping...")
        turn_off_heating_lamp()
        turn_off_exhaust_fan()
        cleanup()


if __name__ == "__main__":
    main()
