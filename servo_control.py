"""
Standalone Servo Test Script
-----------------------------
Purpose: Isolate whether a servo issue is caused by power delivery,
mechanical binding, or wiring/code — completely separate from the
PoultryCare main.py logic.

Usage:
  1. Disconnect the servo horn from any valve/mechanism (test unloaded first).
  2. Wire the servo:
       - Signal wire -> GPIO pin (default: GPIO17, physical pin 11)
       - Power wire  -> a SEPARATE 5V supply if possible (not the Pi's 5V rail)
       - Ground wire -> MUST be common with the Pi's ground
  3. Run: python3 servo_test.py
  4. Watch the servo. It should sweep smoothly from 0 -> 90 -> 180 -> 90 -> 0.

Interpreting results:
  - Smooth movement            -> servo + wiring + code are fine.
                                   Your original issue was likely power
                                   sharing/current draw in the full circuit,
                                   or something jammed in the valve mechanism.
  - Buzzing/vibrating, no move -> if this happens even unloaded and with a
                                   solid separate power source, the servo
                                   itself is very likely defective (stripped
                                   gears or dead motor). Return/replace it.
  - No response at all         -> check wiring (signal pin, common ground)
                                   before assuming the servo is bad.

Requires: RPi.GPIO (usually preinstalled on Raspberry Pi OS)
  If missing: pip install RPi.GPIO --break-system-packages
"""

import RPi.GPIO as GPIO
import time

# ---- CONFIG: change this if your servo signal wire is on a different pin ----
SERVO_PIN = 22  # BCM numbering (physical pin 11)

# Standard hobby servo PWM frequency
PWM_FREQUENCY_HZ = 50


def angle_to_duty_cycle(angle):
    """Convert an angle (0-180) to a duty cycle percentage for a standard SG90-type servo."""
    # SG90 typical range: 2.5% (0 deg) to 12.5% (180 deg)
    return 2.5 + (angle / 180.0) * 10.0


def main():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(SERVO_PIN, GPIO.OUT)

    pwm = GPIO.PWM(SERVO_PIN, PWM_FREQUENCY_HZ)
    pwm.start(0)

    try:
        print(f"Testing servo on GPIO{SERVO_PIN}. Press Ctrl+C to stop.")
        print("Watch closely: does it move smoothly, buzz in place, or do nothing?\n")

        angles_to_test = [0, 90, 180, 90, 0]

        for angle in angles_to_test:
            duty = angle_to_duty_cycle(angle)
            print(f"Moving to {angle} degrees (duty cycle: {duty:.1f}%)")
            pwm.ChangeDutyCycle(duty)
            time.sleep(1.5)  # give it time to actually move and settle

        print("\nTest complete. Did the horn visibly rotate through the positions above?")

    except KeyboardInterrupt:
        print("\nStopped by user.")

    finally:
        pwm.stop()
        GPIO.cleanup()


if __name__ == "__main__":
    main()