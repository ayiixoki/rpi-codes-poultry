import RPi.GPIO as GPIO
import time

# GPIO pin assignments
HEATING_LAMP_PIN = 17
EXHAUST_FAN_PIN = 27

def setup_relay():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(HEATING_LAMP_PIN, GPIO.OUT)
    GPIO.setup(EXHAUST_FAN_PIN, GPIO.OUT)
    # Active LOW relay HIGH means OFF at start
    GPIO.output(HEATING_LAMP_PIN, GPIO.HIGH)
    GPIO.output(EXHAUST_FAN_PIN, GPIO.HIGH)

def turn_on_heating_lamp():
    if GPIO.input(HEATING_LAMP_PIN) != GPIO.LOW:
        GPIO.output(HEATING_LAMP_PIN, GPIO.LOW)
        print("Heating lamp ON")


def turn_off_heating_lamp():
    if GPIO.input(HEATING_LAMP_PIN) != GPIO.HIGH:
        GPIO.output(HEATING_LAMP_PIN, GPIO.HIGH)
        print("Heating lamp OFF")


def turn_on_exhaust_fan():
    if GPIO.input(EXHAUST_FAN_PIN) != GPIO.LOW:
        GPIO.output(EXHAUST_FAN_PIN, GPIO.LOW)
        print("Exhaust fan ON")


def turn_off_exhaust_fan():
    if GPIO.input(EXHAUST_FAN_PIN) != GPIO.HIGH:
        GPIO.output(EXHAUST_FAN_PIN, GPIO.HIGH)
        print("Exhaust fan OFF")

def cleanup():
    GPIO.cleanup()

if __name__ == "__main__":
    setup_relay()
    time.sleep(1)
    print("Testing relay CH1 - Heating Lamp")
    turn_on_heating_lamp()
    time.sleep(3)
    turn_off_heating_lamp()
    time.sleep(2)
    print("Testing relay CH2 - Exhaust Fan")
    turn_on_exhaust_fan()
    time.sleep(3)
    turn_off_exhaust_fan()
    cleanup()
    print("Relay test done")
