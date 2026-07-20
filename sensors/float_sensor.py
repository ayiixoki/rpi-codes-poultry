import RPi.GPIO as GPIO
import time

FLOAT_SENSOR_PIN = 26

def setup_float_sensor():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(FLOAT_SENSOR_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

def read_water_level():
    """Returns 'normal' if water present, 'low' if water level is low."""
    state = GPIO.input(FLOAT_SENSOR_PIN)
    if state == GPIO.HIGH:
        return "normal"
    else:
        return "low"

if __name__ == "__main__":
    setup_float_sensor()
    print("Reading float sensor... (move the float up and down)")
    try:
        while True:
            level = read_water_level()
            print(f"Water level: {level}")
            time.sleep(1)
    except KeyboardInterrupt:
        GPIO.cleanup()
        print("\nStopped")
