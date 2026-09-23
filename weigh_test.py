# weigh_test.py
import time
from sensors.load_cell import setup_hx711, tare, read_raw
import config

setup_hx711()

print("Empty the plate, then press Enter to tare...")
input()
offset = tare(save=False)

print("Now place the item you want to weigh.")
try:
    while True:
        raw = read_raw(samples=15)
        if raw is not None:
            grams = round((raw - offset) / config.HX711_SCALE, 1)
            print(f"Weight: {grams} g")
        else:
            print("Invalid reading")
        time.sleep(1)
except KeyboardInterrupt:
    print("\nStopped")