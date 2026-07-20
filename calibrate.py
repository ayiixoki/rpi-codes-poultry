import RPi.GPIO as GPIO
import time
from sensors.load_cell import setup_hx711, read_raw, tare

def calibrate(known_weight_grams):
    setup_hx711()

    # Step 1 - Tare with no weight
    print("Step 1: Make sure load cell is EMPTY")
    offset = tare(samples=20)

    # Step 2 - Place known weight
    print(f"\nStep 2: Place your {known_weight_grams}g weight on the load cell")
    print("Waiting 5 seconds...")
    time.sleep(5)

    # Step 3 - Read with known weight
    print("Reading with known weight...")
    readings = []
    for _ in range(20):
        raw = read_raw()
        if raw is not None:
            readings.append(raw)
        time.sleep(0.1)

    avg_raw = sum(readings) / len(readings)
    scale = (avg_raw - offset) / known_weight_grams

    print(f"\nCalibration Results:")
    print(f"Offset: {offset:.0f}")
    print(f"Scale factor: {scale:.2f}")
    print(f"\nSave these values in config.py:")
    print(f"HX711_OFFSET = {offset:.0f}")
    print(f"HX711_SCALE = {scale:.2f}")

    GPIO.cleanup()

if __name__ == "__main__":
    known = float(input("Enter known weight in grams: "))
    calibrate(known)
