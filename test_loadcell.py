from sensors.load_cell import setup_hx711, read_grams
import time

setup_hx711()
print("Reading weight in grams (place objects on load cell)...")
try:
    while True:
        weight = read_grams()
        if weight is not None:
            print(f"Weight: {weight}g")
        time.sleep(1)
except KeyboardInterrupt:
    import RPi.GPIO as GPIO
    GPIO.cleanup()
    print("\nStopped")
