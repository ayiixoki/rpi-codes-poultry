import RPi.GPIO as GPIO
import time

FLOAT_SENSOR_PIN = 26        # trough float (auto-refill trigger)
CONTAINER_SENSOR_PIN = 18    # container float (BCM 18 = physical pin 12)

# Which pin state means the container is EMPTY. Flip to GPIO.HIGH if reversed.
CONTAINER_EMPTY_STATE = GPIO.HIGH

# Pin state that means water is present at the trough.
# With PUD_UP a normal float switch pulls the pin LOW when the float rises,
# so GPIO.LOW is the most likely correct value. Test with this file's __main__.
TROUGH_NORMAL_STATE = GPIO.HIGH


def setup_float_sensor():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(FLOAT_SENSOR_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(CONTAINER_SENSOR_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)


def read_water_level():
    """'normal' if water present at the trough, 'low' if not."""
    return "normal" if GPIO.input(FLOAT_SENSOR_PIN) == TROUGH_NORMAL_STATE else "low"


def read_trough_raw():
    """Raw pin value (0/1). Used for diagnostics so we can see what the pin does."""
    return GPIO.input(FLOAT_SENSOR_PIN)


def read_water_level_settled(samples=7, gap=0.05):
    """Majority vote of several quick reads, so ripples can't flip the result."""
    normal = 0
    for _ in range(samples):
        if read_water_level() == "normal":
            normal += 1
        time.sleep(gap)
    return "normal" if normal > samples // 2 else "low"


def read_container_level():
    """'ok' if the container has water, 'empty' if it needs refill."""
    return "empty" if GPIO.input(CONTAINER_SENSOR_PIN) == CONTAINER_EMPTY_STATE else "ok"


if __name__ == "__main__":
    setup_float_sensor()
    print("Reading float sensors... (move each float up and down)")
    try:
        while True:
            print(f"Trough: {read_water_level()} (raw {read_trough_raw()}) | "
                  f"Container: {read_container_level()}")
            time.sleep(1)
    except KeyboardInterrupt:
        GPIO.cleanup()
        print("\nStopped")