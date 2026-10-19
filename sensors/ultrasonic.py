import time
import statistics
import RPi.GPIO as GPIO
import config

# HC-SR04 on top of the feed container, facing down at the feed.
# Pins are BCM (GPIO) numbers. Set them in config.py (defaults below).
TRIG_PIN = getattr(config, "ULTRASONIC_TRIG_PIN", 23)
ECHO_PIN = getattr(config, "ULTRASONIC_ECHO_PIN", 12)

# Distance from the sensor to the feed surface:
FULL_CM = getattr(config, "ULTRASONIC_FULL_CM", 6.8)      # container FULL
EMPTY_CM = getattr(config, "ULTRASONIC_EMPTY_CM", 16.8)   # container EMPTY (sensor to bottom)

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


SAMPLES = 9
MAX_ATTEMPTS = 3                # extra batches of pings if too few echoes came back
MIN_VALID = 3
SMOOTH_ALPHA = 0.3              # lower = steadier, slower to react
SNAP_CM = 1.5                   # a jump bigger than this (feed added/removed) skips smoothing
NO_ECHO_EMPTY_BELOW = 10        # % - only if it was ALREADY nearly empty...
NO_ECHO_CONFIRM = 6             # ...and ~1 min of reads (6 x 10 s) all had no echo

_smoothed_distance = None
_last_percent = None
_no_echo_count = 0


def read_feed_bin_percent(samples=SAMPLES):
    global last_distance_cm, _smoothed_distance, _last_percent, _no_echo_count
    if EMPTY_CM <= FULL_CM:
        print("Ultrasonic config error: EMPTY_CM must be bigger than FULL_CM")
        return None

    readings = []
    for _ in range(MAX_ATTEMPTS):
        for _ in range(samples):
            d = read_distance_cm()
            if d is not None:
                readings.append(d)
            time.sleep(0.06)
        if len(readings) >= MIN_VALID:
            break

    if len(readings) < MIN_VALID:
        # No usable echo. Only call it "empty" if it was already almost empty
        # AND this keeps happening for a while. Otherwise keep the old value.
        _no_echo_count += 1
        if (_last_percent is not None and _last_percent <= NO_ECHO_EMPTY_BELOW
                and _no_echo_count >= NO_ECHO_CONFIRM):
            _last_percent = 0.0
            return 0.0
        return None

    _no_echo_count = 0
    readings.sort()
    trimmed = readings[1:-1] if len(readings) > 4 else readings   # drop min/max
    distance = statistics.median(trimmed)
    last_distance_cm = round(distance, 3)

    # Smooth across calls, but react right away to a big change (feed added)
    if _smoothed_distance is None or abs(distance - _smoothed_distance) > SNAP_CM:
        _smoothed_distance = distance
    else:
        _smoothed_distance += SMOOTH_ALPHA * (distance - _smoothed_distance)

    percent = (EMPTY_CM - _smoothed_distance) / (EMPTY_CM - FULL_CM) * 100
    _last_percent = round(max(0.0, min(100.0, percent)), 1)
    return _last_percent