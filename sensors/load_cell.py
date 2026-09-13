import RPi.GPIO as GPIO
import time
from hx711 import HX711
import config
import statistics

_hx = None

def setup_hx711():
    """Initialize the HX711 object. Safe to call multiple times."""
    global _hx
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    _hx = HX711(dout_pin=config.HX711_DT_PIN, pd_sck_pin=config.HX711_SCK_PIN)
    _hx.reset()
    _hx.set_offset(0)

    return _hx


def read_raw(samples=10):
    global _hx
    if _hx is None:
        setup_hx711()

    result = _hx.get_raw_data_mean(readings=samples)
    if result is False:
        return None

    return result

def tare(samples=25):
    """Get the zero offset by averaging multiple raw readings."""
    print("Taring... remove all weight from load cell")
    time.sleep(2)
    offset = read_raw(samples=samples)
    if offset is None:
        print("Tare failed - no valid readings. Check wiring/ground connection.")
        return 0
    print(f"Tare offset: {offset:.0f}")
    return offset

def read_weight(offset, scale):
    """Read weight in grams (legacy signature, kept for compatibility)."""
    return read_grams(offset=offset, scale=scale)

_last_valid_grams = None
_pending_value = None
_pending_count = 0
MAX_PLAUSIBLE_DELTA_G = 100       # single-reading jump considered "suspicious"
CONFIRM_READS_REQUIRED = 3        # how many consistent readings needed to accept a big jump

def read_grams(offset=None, scale=None, samples=15):
    global _last_valid_grams, _pending_value, _pending_count
    if offset is None:
        offset = config.HX711_OFFSET
    if scale is None:
        scale = config.HX711_SCALE

    raw = read_raw(samples=samples)
    if raw is None:
        return _last_valid_grams

    weight = max(0.0, round((raw - offset) / scale, 1))

    # Small change, or first-ever reading — accept immediately
    if _last_valid_grams is None or abs(weight - _last_valid_grams) <= MAX_PLAUSIBLE_DELTA_G:
        _last_valid_grams = weight
        _pending_value = None
        _pending_count = 0
        return weight

    # Big jump — needs to repeat consistently before we trust it
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
    """
    Call once per main-loop iteration with whatever read_grams() just
    returned. Reports a new value only every 30s, using the median of
    everything sampled in that window - so a short noise burst (relay
    or servo EMI) can't move it, only a real, sustained weight change
    can. Use the return value for the app, LCD, and alerts.
    """
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
        _stable_value = _stable_window[0]  # don't show blank on startup

    return _stable_value

def reset_stable_grams(value):
    """Call right after a real dispense finishes, so the app/LCD update
    immediately instead of waiting up to 30s for the next window."""
    global _stable_window, _stable_window_start, _stable_value
    _stable_value = value
    _stable_window = []
    _stable_window_start = time.time()

if __name__ == "__main__":
    setup_hx711()
    print("HX711 Library-based Test (gandalf15/HX711)")
    print("Reading raw values first...")

    for i in range(5):
        raw = read_raw()
        print(f"Raw value {i+1}: {raw}")
        time.sleep(0.5)

    offset = tare()

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