# GPIO Pin Assignments
DHT22_PIN = 4
HEATING_LAMP_PIN = 17
EXHAUST_FAN_PIN = 27
FLOAT_SENSOR_PIN = 26
HX711_DT_PIN = 5
HX711_SCK_PIN = 6
FEED_SERVO_PIN = 24
WATER_SERVO_PIN = 25

# Default thresholds (fallback if Firebase unreachable)
DEFAULT_TEMP_MIN = 32
DEFAULT_TEMP_MAX = 37
DEFAULT_HUM_MIN = 50
DEFAULT_HUM_MAX = 70

# HX711 Calibration
HX711_OFFSET = -17369
HX711_SCALE = 412.65
GRAMS_PER_SECOND = 5

DEFAULT_MANUAL_DISPENSE_GRAMS = 200
MANUAL_COMMAND_POLL_INTERVAL = 0.1

# Sensor read interval (seconds)
FAST_READ_INTERVAL = 1     # seconds - feed weight + water level
SLOW_READ_INTERVAL = 5     # seconds - temperature + humidity (DHT22)

# Feed low threshold default
DEFAULT_FEED_LOW = 100

# Hysteresis (prevents relay chattering)
TEMP_HYSTERESIS = 1.0      # degrees
HUM_HYSTERESIS = 5.0       # %

DISPENSE_OVERSHOOT_BUFFER_GRAMS = 2  # stop this many grams early to account for feed still falling after servo closes

WATER_EXTRA_FILL_TIME = 1.0 #seconds to keep the water pump running after the water level sensor reads "normal" to ensure the water is topped up
