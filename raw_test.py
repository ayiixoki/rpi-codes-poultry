import RPi.GPIO as GPIO
from hx711 import HX711
import time

GPIO.setmode(GPIO.BCM)

hx = HX711(dout_pin=5, pd_sck_pin=6)

hx.reset()

try:
    while True:
        value = hx.get_raw_data_mean(10)
        if value is False:
            continue
        print(value)
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    GPIO.cleanup()