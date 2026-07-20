import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from sensors.dht22 import read_dht22, cleanup
from actuators.relay_control import (
    setup_relay, turn_on_heating_lamp, turn_off_heating_lamp,
    turn_on_exhaust_fan, turn_off_exhaust_fan
)
from firebase_service import init_firebase, push_sensor_data, get_thresholds
import config

def run_adaptive_control():
    init_firebase()
    setup_relay()
    print("Adaptive control started...")
    
    fan_on = False
    lamp_on = False

    while True:
        # Read sensor
        temp, hum = read_dht22()

        if temp is None or hum is None:
            print("Sensor read failed, retrying...")
            time.sleep(3)
            continue

        print(f"Temp: {temp:.1f}C | Humidity: {hum:.1f}%")

        # Get thresholds from Firebase
        thresholds = get_thresholds()
        if thresholds:
            temp_min = thresholds.get("tempMin", config.TEMP_MIN)
            temp_max = thresholds.get("tempMax", config.TEMP_MAX)
            hum_max = thresholds.get("humMax", config.HUM_MAX)
        else:
            temp_min = config.TEMP_MIN
            temp_max = config.TEMP_MAX
            hum_max = config.HUM_MAX

        # ==========================
        # Temperature has priority
        # ==========================

        if temp >= temp_max or (fan_on and temp > temp_max - config.TEMP_HYSTERESIS):

            if not fan_on:
                turn_on_exhaust_fan()
                fan_on = True

            if lamp_on:
                turn_off_heating_lamp()
                lamp_on = False

            print("ACTION: Fan ON (High Temperature)")


        elif temp <= temp_min or (lamp_on and temp < temp_min + config.TEMP_HYSTERESIS):

            if not lamp_on:
                turn_on_heating_lamp()
                lamp_on = True

            if fan_on:
                turn_off_exhaust_fan()
                fan_on = False

            print("ACTION: Lamp ON (Low Temperature)")


        else:

            # Temperature is OK
            # Check humidity

            if hum >= hum_max:

                if not fan_on:
                    turn_on_exhaust_fan()
                    fan_on = True

                if lamp_on:
                    turn_off_heating_lamp()
                    lamp_on = False

                print("ACTION: Fan ON (High Humidity)")

            elif hum <= (hum_max - config.HUM_HYSTERESIS):

                if fan_on:
                    turn_off_exhaust_fan()
                    fan_on = False

                if lamp_on:
                    turn_off_heating_lamp()
                    lamp_on = False

        print("ACTION: Environment Normal")

        # Push to Firebase
        push_sensor_data(temp, hum, 0, "normal")

        time.sleep(5)

if __name__ == "__main__":
    try:
        run_adaptive_control()
    except KeyboardInterrupt:
        print("Stopping...")
        cleanup()
