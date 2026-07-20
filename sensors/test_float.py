import RPi.GPIO as GPIO
import time
import config

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
GPIO.setup(config.FLOAT_SENSOR_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

try:
    while True:
        print(f"Float switch raw reading: {GPIO.input(config.FLOAT_SENSOR_PIN)}")
        time.sleep(0.5)
except KeyboardInterrupt:
    GPIO.cleanup()
