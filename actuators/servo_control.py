import RPi.GPIO as GPIO
import time

FEED_SERVO_PIN = 24
WATER_SERVO_PIN = 25

def setup_servos():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(FEED_SERVO_PIN, GPIO.OUT)
    GPIO.setup(WATER_SERVO_PIN, GPIO.OUT)

    global feed_pwm, water_pwm
    feed_pwm = GPIO.PWM(FEED_SERVO_PIN, 50)   # 50Hz PWM
    water_pwm = GPIO.PWM(WATER_SERVO_PIN, 50)
    feed_pwm.start(0)
    water_pwm.start(0)

def set_angle(pwm, angle):
    """Move servo to specified angle (0-180)."""
    duty = 2 + (angle / 18)
    pwm.ChangeDutyCycle(duty)
    time.sleep(0.5)
    pwm.ChangeDutyCycle(0)  # Stop signal to prevent jitter

def close_feed():
    print("Feed servo CLOSING...")
    set_angle(feed_pwm, 5)

def open_feed():
    print("Feed servo OPENING...")
    set_angle(feed_pwm, 20)

def close_water():
    print("Water servo CLOSING...")
    set_angle(water_pwm, 10)
    
def open_water():
    print("Water servo OPENING...")
    set_angle(water_pwm, 90)

def cleanup_servos():
    feed_pwm.stop()
    water_pwm.stop()
    GPIO.cleanup()

if __name__ == "__main__":
    setup_servos()
    print("Testing Feed Servo (MG996R)...")
    open_feed()
    time.sleep(4)
    close_feed()
    time.sleep(1)
    print("Testing Water Servo (MG90S)...")
    open_water()
    time.sleep(4)
    close_water()
    cleanup_servos()
    print("Servo test done")
