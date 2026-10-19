import time
import RPi.GPIO as GPIO
from actuators import servo_control as s

s.setup_servos()
print("Feed servo test. Commands: o = open, c = close, <number> = go to angle, q = quit")

try:
    while True:
        cmd = input("> ").strip().lower()
        if cmd == "o":
            s.open_feed()
        elif cmd == "c":
            s.close_feed()
        elif cmd == "q":
            break
        else:
            try:
                angle = float(cmd)
                print(f"Moving to {angle} degrees")
                s.set_angle(s.feed_pwm, angle)
            except ValueError:
                print("Unknown command")
finally:
    s.cleanup_servos()
    print("Done")