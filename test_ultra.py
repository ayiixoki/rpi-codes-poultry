import time
import RPi.GPIO as GPIO
from sensors import ultrasonic as u

GPIO.setmode(GPIO.BCM)
u.setup_ultrasonic()
try:
    while True:
        pct = u.read_feed_bin_percent()
        print(f"distance {u.get_last_distance()} cm -> {pct}%")
        time.sleep(1)
finally:
    GPIO.cleanup()