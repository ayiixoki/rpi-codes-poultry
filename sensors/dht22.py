import adafruit_dht
import board
import time

sensor = adafruit_dht.DHT22(board.D4)

def read_dht22():
    try:
        temperature = sensor.temperature
        humidity = sensor.humidity
        return temperature, humidity
    except RuntimeError as e:
        print(f"Reading error: {e}")
        return None, None

def cleanup():
    sensor.exit()
