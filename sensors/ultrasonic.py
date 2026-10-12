import time
import statistics
import RPi.GPIO as GPIO
import config

# HC-SR04 on top of the feed container, facing down at the feed.
# Pins are BCM (GPIO) numbers. Set them in config.py (defaults below).
TRIG_PIN = getattr(config, "ULTRASONIC_TRIG_PIN", 23)
ECHO_PIN = getattr(config, "ULTRASONIC_ECHO_PIN", 12)

# Distance from the sensor to the feed surface:
FULL_CM = getattr(config, "ULTRASONIC_FULL_CM", 5)      # container FULL
EMPTY_CM = getattr(config, "ULTRASONIC_EMPTY_CM", 20)   # container EMPTY (sensor to bottom)

SPEED_OF_SOUND_CM_S = 34300

last_distance_cm = None


def get_last_distance():
    """Raw median distance of the last good reading (not clamped, not rounded)."""
    return last_distance_cm


def setup_ultrasonic():
    if GPIO.getmode() is None:
        GPIO.setmode(GPIO.BCM)
    GPIO.setup(TRIG_PIN, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(ECHO_PIN, GPIO.IN)
    time.sleep(0.05)


def read_distance_cm():
    """One distance reading in cm, or None if no echo / out of range."""
    try:
        GPIO.output(TRIG_PIN, True)
        time.sleep(0.00001)          # 10 microsecond trigger pulse
        GPIO.output(TRIG_PIN, False)

        deadline = time.perf_counter() + 0.04
        while GPIO.input(ECHO_PIN) == 0:
            if time.perf_counter() > deadline:
                return None
        pulse_start = time.perf_counter()

        deadline = pulse_start + 0.04
        while GPIO.input(ECHO_PIN) == 1:
            if time.perf_counter() > deadline:
                return None
        pulse_end = time.perf_counter()

        distance = (pulse_end - pulse_start) * SPEED_OF_SOUND_CM_S / 2
        if distance < 2 or distance > 400:       # HC-SR04 range
            return None
        return distance
    except Exception as e:
        print(f"Ultrasonic read error: {e}")
        return None


def read_feed_bin_percent(samples=5):
    """Feed left in the container, 0-100 %. None if the sensor gives no
    valid readings (wiring problem / sensor damaged)."""
    global last_distance_cm
    if EMPTY_CM <= FULL_CM:
        print("Ultrasonic config error: ULTRASONIC_EMPTY_CM must be bigger than ULTRASONIC_FULL_CM")
        return None

    readings = []
    for _ in range(samples):
        d = read_distance_cm()
        if d is not None:
            readings.append(d)
        time.sleep(0.06)

    if len(readings) < 3:
        return None

    distance = statistics.median(readings)       # median ignores stray bad readings
    last_distance_cm = round(distance, 3)
    percent = (EMPTY_CM - distance) / (EMPTY_CM - FULL_CM) * 100
    return round(max(0.0, min(100.0, percent)), 1)