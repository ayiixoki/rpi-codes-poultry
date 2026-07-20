import RPi.GPIO as GPIO
import time
from hx711 import HX711
import config

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

def read_grams(offset=None, scale=None, samples=10):
    """Read weight in grams using calibration values from config.py."""
    if offset is None:
        offset = config.HX711_OFFSET
    if scale is None:
        scale = config.HX711_SCALE

    raw = read_raw(samples=samples)
    if raw is None:
        return None

    weight = (raw - offset) / scale
    weight = max(0.0, round(weight, 1))  # Never return negative
    return weight

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