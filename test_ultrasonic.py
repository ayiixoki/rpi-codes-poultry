import time, statistics
import RPi.GPIO as GPIO
from sensors import ultrasonic as u    # your file's name

GPIO.setmode(GPIO.BCM)
u.setup_ultrasonic()
try:
    while True:
        r = [u.read_distance_cm() for _ in range(15)]
        good = [x for x in r if x is not None]
        if good:
            print(f"median {statistics.median(good):.1f} cm | min {min(good):.1f} max {max(good):.1f} | valid {len(good)}/15")
        else:
            print("no echo")
        time.sleep(0.5)
finally:
    GPIO.cleanup()  