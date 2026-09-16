import RPi.GPIO as GPIO
import time
import json
import os
import argparse
from hx711 import HX711
import config
import statistics

_hx = None

CALIBRATION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration.json")
_current_offset = None


def setup_hx711(warmup_reads=5, warmup_delay=1.0):
    """Initialize the HX711 object. Safe to call multiple times.

    Discards a handful of raw readings right after power-up/reset before
    returning - the HX711's first readings after reset are inherently
    unstable (ADC settling time), independent of anything physically on
    the plate. Skipping these avoids feeding garbage values into
    read_grams()'s jitter filter (_last_valid_grams) at startup."""
    global _hx
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    _hx = HX711(dout_pin=config.HX711_DT_PIN, pd_sck_pin=config.HX711_SCK_PIN)
    _hx.reset()
    _hx.set_offset(0)

    time.sleep(warmup_delay)
    for _ in range(warmup_reads):
        _hx.get_raw_data_mean(readings=5)  # discarded - just letting the ADC settle

    return _hx


def read_raw(samples=10):
    global _hx
    if _hx is None:
        setup_hx711()

    result = _hx.get_raw_data_mean(readings=samples)
    if result is False:
        return None

    return result


def _load_offset_from_file():
    """Read the persisted tare offset from calibration.json, if present."""
    if os.path.exists(CALIBRATION_FILE):
        try:
            with open(CALIBRATION_FILE, "r") as f:
                data = json.load(f)
            return data.get("offset")
        except (json.JSONDecodeError, OSError) as e:
            print(f"Calibration file unreadable ({e}), falling back to config.HX711_OFFSET")
    return None


def _save_offset_to_file(offset):
    try:
        with open(CALIBRATION_FILE, "w") as f:
            json.dump(
                {"offset": offset, "tared_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                f,
                indent=2,
            )
        print(f"Saved new tare offset to {CALIBRATION_FILE}")
    except OSError as e:
        print(f"Could not save calibration file: {e}")


def get_offset():
    """Offset used for weight math: calibration.json if a tare has been
    saved, otherwise config.HX711_OFFSET as a fallback."""
    global _current_offset
    if _current_offset is None:
        file_offset = _load_offset_from_file()
        _current_offset = file_offset if file_offset is not None else config.HX711_OFFSET
    return _current_offset


def tare(samples=25, save=True):
    """Get the zero offset by averaging multiple raw readings.
    IMPORTANT: leave the empty feed plate/bowl on the load cell exactly as
    it sits in normal operation - only remove the FEED, not the plate.
    Otherwise plate weight gets baked into every gram reading afterward.

    By default, saves the result to calibration.json so it survives restarts
    and future re-tares don't require editing config.py or redeploying code.
    """
    global _current_offset
    print("Taring... make sure the plate is ON, empty of feed, then wait")
    time.sleep(2)
    offset = read_raw(samples=samples)
    if offset is None:
        print("Tare failed - no valid readings. Check wiring/ground connection.")
        return 0
    print(f"Tare offset: {offset:.0f}")

    if save:
        _save_offset_to_file(offset)
    _current_offset = offset

    return offset


def read_weight(offset, scale):
    """Read weight in grams (legacy signature, kept for compatibility)."""
    return read_grams(offset=offset, scale=scale)

def read_filtered_weight(hx, samples=3):
    """Returns a noise-filtered weight reading using median-of-3."""
    readings = [hx.get_weight(1) for _ in range(samples)]
    readings.sort()
    return readings[len(readings) // 2]  # median


_last_valid_grams = None
_pending_value = None
_pending_count = 0
MAX_PLAUSIBLE_DELTA_G = 100       # single-reading jump considered "suspicious"
CONFIRM_READS_REQUIRED = 3        # how many consistent readings needed to accept a big jump

def read_grams(offset=None, scale=None, samples=15, trust_reading=False):
    global _last_valid_grams, _pending_value, _pending_count
    if offset is None:
        offset = get_offset()
    if scale is None:
        scale = config.HX711_SCALE

    raw = read_raw(samples=samples)
    if raw is None:
        return _last_valid_grams

    # Clamp to [0, FEED_CAPACITY_GRAMS] - the feeder physically cannot hold
    # more than its capacity, so any reading above that is noise, not real
    # feed. This keeps LCD/app percentage displays from exceeding 100%.
    weight = max(0.0, min(config.FEED_CAPACITY_GRAMS, round((raw - offset) / scale, 1)))

    if trust_reading:
        _last_valid_grams = weight
        _pending_value = None
        _pending_count = 0
        return weight

    if _last_valid_grams is None or abs(weight - _last_valid_grams) <= MAX_PLAUSIBLE_DELTA_G:
        _last_valid_grams = weight
        _pending_value = None
        _pending_count = 0
        return weight

    if _pending_value is not None and abs(weight - _pending_value) < 20:
        _pending_count += 1
    else:
        _pending_value = weight
        _pending_count = 1

    if _pending_count >= CONFIRM_READS_REQUIRED:
        print(f"Confirmed new feed level: {weight}g")
        _last_valid_grams = weight
        _pending_value = None
        _pending_count = 0
        return weight

    print(f" Rejected noisy feed reading: {weight}g (last valid: {_last_valid_grams}g)")
    return _last_valid_grams

# ---- Stabilized reading for display / Firebase / alerts ----
_stable_window = []
_stable_window_start = time.time()
_stable_value = None
STABLE_WINDOW_SECONDS = 30

def update_stable_grams(instant_value):
    global _stable_window, _stable_window_start, _stable_value

    if instant_value is not None:
        _stable_window.append(instant_value)

    now = time.time()
    if now - _stable_window_start >= STABLE_WINDOW_SECONDS:
        if _stable_window:
            _stable_value = round(statistics.median(_stable_window), 1)
        _stable_window = []
        _stable_window_start = now
    elif _stable_value is None and _stable_window:
        _stable_value = _stable_window[0]

    return _stable_value

def reset_stable_grams(value):
    global _stable_window, _stable_window_start, _stable_value
    _stable_value = value
    _stable_window = []
    _stable_window_start = time.time()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HX711 load cell utility")
    parser.add_argument(
        "--tare",
        action="store_true",
        help="Re-tare with the empty plate ON (no feed) and save the offset to calibration.json",
    )
    args = parser.parse_args()

    setup_hx711()

    if args.tare:
        tare()
        GPIO.cleanup()
        raise SystemExit(0)

    print("HX711 Library-based Test (gandalf15/HX711)")
    print("Reading raw values first...")

    for i in range(5):
        raw = read_raw()
        print(f"Raw value {i+1}: {raw}")
        time.sleep(0.5)

    offset = tare(save=False)  # test-mode run: doesn't overwrite the saved calibration

    print("\nPlace a known weight on the load cell...")
    print("Raw readings (subtract offset to see change):")
    try:
        while True:
            raw = read_raw()
            if raw is not None:
                diff = raw - offset
                print(f"Raw: {raw}  |  Difference from tare: {diff:.0f}")
            else:
                print("Invalid reading (check wiring)")
            time.sleep(1)
    except KeyboardInterrupt:
        GPIO.cleanup()
        print("\nStopped")