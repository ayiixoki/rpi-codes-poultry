# GPIO Pin Assignments
DHT22_PIN = 4
HEATING_LAMP_PIN = 17
EXHAUST_FAN_PIN = 27
FLOAT_SENSOR_PIN = 26
HX711_DT_PIN = 5
HX711_SCK_PIN = 6
FEED_SERVO_PIN = 24
WATER_SERVO_PIN = 25

ULTRASONIC_ECHO_PIN = 12
ULTRASONIC_TRIG_PIN = 23
ULTRASONIC_FULL_CM = 5       # sensor to feed surface when container is FULL
ULTRASONIC_EMPTY_CM = 20     # sensor to container bottom when EMPTY
FEED_BIN_LOW_PERCENT = 20

# Default thresholds (fallback if Firebase unreachable)
DEFAULT_TEMP_MIN = 32
DEFAULT_TEMP_MAX = 34
DEFAULT_HUM_MIN = 50
DEFAULT_HUM_MAX = 70

# HX711 Calibration
HX711_OFFSET = 61716
HX711_SCALE = 412.65
GRAMS_PER_SECOND = 15   # fallback dispense-rate estimate until dispense_rate_cache.json has real data

SERVO_CLOSE_LATENCY_SECONDS = 0.25  # placeholder — measure this for real, see below
SERVO_CLOSE_LATENCY = 0.20   # seconds — how long servo takes to fully stop feed after command sent
TARGET_WEIGHT = 200           # grams — your dispense target

DEFAULT_MANUAL_DISPENSE_GRAMS = 200
# Feed hopper capacity
FEED_CAPACITY_GRAMS = 420
MANUAL_COMMAND_POLL_INTERVAL = 0.5

SCHEDULE_FEED_GRAMS = 200          # default per-schedule dispense amount (2x/day = 200g/day)
DISPENSE_STAGE_INTERVAL_SECONDS = 3  # pause between feed servo closing and water servo opening

WATER_NORMAL_CONFIRM_READS = 2      # consecutive "normal" float reads required before closing water valve
WATER_DISPENSE_TIMEOUT_SECONDS = 300  # safety cutoff if float sensor never reports "normal"
WATER_DISPENSE_TIMEOUT_MANUAL_SECONDS = 300  # manual dispense safety cutoff
WATER_EXTRA_FILL_TIME = 20 #seconds to keep the water pump running after the water level sensor reads "normal" to ensure the water is topped up
WATER_HARD_CAP_SECONDS = 270  # stop and assume "normal" after this long, sensor is too slow to trust beyond it
WATER_LOW_ALERT_DELAY_SECONDS = 1800  # 30 minutes — surface-level container dips low briefly/normally
WATER_REFILL_RETRY_COOLDOWN_SECONDS = 300

FEED_HYSTERESIS_PERCENT = 5
FEED_ALERT_CONFIRM_READS = 10
LOAD_CELL_MAX_GRAMS = 5000              # your 5 kg cell
ALLOW_EXCEED_CAPACITY = True

# Sensor read interval (seconds)
FAST_READ_INTERVAL = 1    # seconds - feed weight + water level
SLOW_READ_INTERVAL = 10     # seconds - temperature + humidity (DHT22)

# Feed low threshold default
DEFAULT_FEED_LOW = 20

# Hysteresis (prevents relay chattering)
TEMP_HYSTERESIS = 0.1     # degrees
HUM_HYSTERESIS = 1.0       # %

# Alert debounce — consecutive confirming/clearing readings required before
# an alert fires or clears. Feed is checked every fast loop (~1/sec); temp
# and humidity only advance this count on an actual new DHT22 sample
# (every SLOW_READ_INTERVAL seconds), so this is ~5 real samples, not just
# 5 loop iterations of the same cached value.
TEMP_ALERT_CONFIRM_READS = 5
HUM_ALERT_CONFIRM_READS = 5

DISPENSE_OVERSHOOT_BUFFER_GRAMS = 1  # stop this many grams early to account for feed still falling after servo closes
DISPENSE_BULK_MIN_AMOUNT_G = 60 
DISPENSE_MIN_PULSE_SECONDS = 0.08

# Bulk-phase feed dispensing (replaces the old fixed 0.08s probe pulse)
DISPENSE_BULK_FRACTION = 0.2      # portion of the requested amount delivered in one continuous bulk pulse
DISPENSE_BULK_MAX_SECONDS = 5    # safety cap on the bulk pulse duration, in case the rate estimate is way off
DISPENSE_MIN_RATE_GPS = 0.3       # floor for the assumed dispense rate, avoids an overlong bulk pulse if the estimate is bad
DISPENSE_SETTLE_SECONDS = 0.6  # how long feed keeps trickling after the servo fully closes — tune by observation

EXHAUST_MIN_RUN_SECONDS = 180      # fan must run at least this long once it starts

FEED_DISPENSE_TIMEOUT_SECONDS = 30  # fail-safe only, the servo normally closes when the sensor reaches the target

# ---- Component health (false-alarm fix) ----
COMPONENT_FAULT_CONFIRM_SECONDS = 10     # the "no reading" limits below already wait long enough
COMPONENT_RECOVER_CONFIRM_SECONDS = 30
SENSOR_NO_READING_SECONDS = {"dht22": 120, "load_cell": 60, "feed_bin": 120}
# Same value for this long = stuck. Load cell is not checked (its stable-hold filter keeps values flat on purpose).
# If read_feed_bin_percent() returns a rounded whole number, set feed_bin to None or keep it long.
SENSOR_STUCK_SECONDS = {"dht22": 2 * 3600, "load_cell": 600, "feed_bin": 6 * 3600}

# ---- Water refill ----
WATER_CHECK_INTERVAL = 0.2               # drinker float is checked ~5x per second
WATER_MIN_INTERVAL_SECONDS = 20          # rest after every successful refill
WATER_MAX_REFILLS_PER_HOUR = 6           # more than this = leak/stuck float, pause and alert
WATER_REPEAT_PAUSE_SECONDS = 1800

# ---- Feed container ----
FEED_BIN_EMPTY_PERCENT = 5               # below this = EMPTY (LOW is FEED_BIN_LOW_PERCENT = 20)