import time
from actuators import servo_control as sc

try:
    sc.setup_servos()
    print("Testing Water Servo (MG90S)...")

    sc.open_water()    # 90 degrees
    time.sleep(3)
    sc.close_water()   # 10 degrees
    time.sleep(1)

    print("Water servo test done")

except KeyboardInterrupt:
    print("\nStopped by user")

finally:
    sc.cleanup_servos()