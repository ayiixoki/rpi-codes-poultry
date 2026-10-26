import time
from actuators.relay_control import (
    setup_relay, turn_on_exhaust_fan, turn_off_exhaust_fan,
    turn_on_heating_lamp, turn_off_heating_lamp, cleanup
)

def main():
    print("Setting up relay...")
    setup_relay()

    print("Forcing exhaust fan and heating lamp ON. Press Ctrl+C to stop.")
    try:
        # turn_on_exhaust_fan()
        turn_on_heating_lamp()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        turn_off_exhaust_fan()
        turn_off_heating_lamp()
        cleanup()

if __name__ == "__main__":
    main()