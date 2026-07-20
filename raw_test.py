from hx711 import HX711
import time

hx = HX711(dout_pin=5, pd_sck_pin=6)

hx.reset()

while True:
    value = hx.get_raw_data_mean(10)
    print(value)
    time.sleep(1)
    