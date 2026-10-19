import time
import statistics
import RPi.GPIO as GPIO
from sensors import float_sensor as f

f.setup_float_sensor()
PIN = f.CONTAINER_SENSOR_PIN


def sample(seconds=3):
    """Read the pin for a few seconds and return the most common value."""
    vals = []
    end = time.time() + seconds
    while time.time() < end:
        vals.append(GPIO.input(PIN))
        time.sleep(0.05)
    return statistics.mode(vals), vals.count(0), vals.count(1)


try:
    print(f"Container float test (BCM {PIN})\n")

    input("Step 1: Put the float DOWN (like the container is EMPTY). Press Enter... ")
    down, z, o = sample()
    print(f"  Float DOWN -> pin reads {down}  (0s: {z}, 1s: {o})\n")

    input("Step 2: Put the float UP (like the container has WATER). Press Enter... ")
    up, z, o = sample()
    print(f"  Float UP   -> pin reads {up}  (0s: {z}, 1s: {o})\n")

    print("---------- RESULT ----------")
    if down == up:
        print("The pin did NOT change. Check the wiring on GPIO 18 and the ground wire.")
    else:
        print(f"Pin value when EMPTY should be: {down}")
        print(f"CONTAINER_EMPTY_STATE is currently: {f.CONTAINER_EMPTY_STATE}")
        if down == f.CONTAINER_EMPTY_STATE:
            print("CORRECT. The setting matches. No change needed.")
        else:
            flipped = "GPIO.HIGH" if down == 1 else "GPIO.LOW"
            print(f"REVERSED. In sensors/float_sensor.py set CONTAINER_EMPTY_STATE = {flipped}")

    print("\nLive mode: move the float now. Ctrl+C to stop.\n")
    while True:
        print(f"container: {f.read_container_level()}  (raw {GPIO.input(PIN)})")
        time.sleep(0.5)
except KeyboardInterrupt:
    pass
finally:
    GPIO.cleanup()
    print("\nStopped")