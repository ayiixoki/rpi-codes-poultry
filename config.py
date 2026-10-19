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
ULTRASONIC_FULL_CM = 6.8      # sensor to feed surface when container is FULL
ULTRASONIC_EMPTY_CM = 16.8    # sensor to container bottom when EMPTY

# Default thresholds (fallback if Firebase unreachable)
DEFAULT_TEMP_MIN = 32
DEFAULT_TEMP_MAX = 34
DEFAULT_HUM_MIN = 50
DEFAULT_HUM_MAX = 70

# HX711 Calibration
HX711_OFFSET = 66206
HX711_SCALE = 412.65
GRAMS_PER_SECOND = 15   # fallback dispense-rate estimate until dispense_rate_cache.json has real data

SERVO_CLOSE_LATENCY_SECONDS = 0.25  # placeholder - measure this for real
SERVO_CLOSE_LATENCY = 0.20          # seconds - how long servo takes to fully stop feed after command sent
TARGET_WEIGHT = 200                 # grams - your dispense target

DEFAULT_MANUAL_DISPENSE_GRAMS = 200
# Feed hopper capacity
FEED_CAPACITY_GRAMS = 760
MANUAL_COMMAND_POLL_INTERVAL = 0.5

SCHEDULE_FEED_GRAMS = 200            # default per-schedule dispense amount
DISPENSE_STAGE_INTERVAL_SECONDS = 3  # (legacy, not used by the current dispense code)

# ---- Water refill (drinker / trough) ----
WATER_NORMAL_CONFIRM_READS = 2        # consecutive "normal" float reads required before closing the valve
WATER_DISPENSE_TIMEOUT_SECONDS = 300  # (legacy)
WATER_DISPENSE_TIMEOUT_MANUAL_SECONDS = 300  # (legacy)
WATER_EXTRA_FILL_TIME = 20            # seconds to keep the valve open after the float reads "normal" (top-up)
WATER_HARD_CAP_SECONDS = 300          # max total valve-OPEN time per refill (across feeding pauses)
WATER_REFILL_RETRY_COOLDOWN_SECONDS = 300
WATER_MIN_INTERVAL_SECONDS = 20       # rest after every successful refill
WATER_MAX_REFILLS_PER_HOUR = 6        # more than this = leak / stuck float: pause and alert once
WATER_REPEAT_PAUSE_SECONDS = 1800
WATER_REPEAT_CLEAR_SECONDS = 3600     # "keeps going low" fault clears after this long with no refill
WATER_MAX_FAILED_TRIES = 3
WATER_FAIL_LOCKOUT_SECONDS = 1800
WATER_CHECK_INTERVAL = 1.0            # how often the water watcher thread looks at the float
WATER_LOW_HOLD_SECONDS = 900          # trough must read LOW this long before any refill (900 = 15 min, 1800 = 30 min)
WATER_NORMAL_RESET_SECONDS = 10       # must read normal this long to cancel the low timer
WATER_FAULT_CLEAR_SECONDS = 120       # trough must stay normal this long to clear a "not filling" fault

# ---- Feed (plate / load cell) ----
FEED_HYSTERESIS_PERCENT = 5
FEED_ALERT_CONFIRM_READS = 10         # (unused now - see FEED_LOW_CONFIRM_SECONDS)
FEED_LOW_CONFIRM_SECONDS = 600        # plate must stay below the low threshold this long before "Low Feed" fires
LOAD_CELL_MAX_GRAMS = 5000            # your 5 kg cell
ALLOW_EXCEED_CAPACITY = True

# Sensor read interval (seconds)
FAST_READ_INTERVAL = 1    # feed weight + water level
SLOW_READ_INTERVAL = 10   # temperature + humidity (DHT22) + feed container (ultrasonic)

# Feed low threshold default
DEFAULT_FEED_LOW = 20

# Hysteresis (prevents relay chattering)
TEMP_HYSTERESIS = 0.1     # degrees
HUM_HYSTERESIS = 1.0      # %

# Alert debounce for temperature / humidity (real DHT22 samples, ~10 s apart)
TEMP_ALERT_CONFIRM_READS = 5
HUM_ALERT_CONFIRM_READS = 5

DISPENSE_OVERSHOOT_BUFFER_GRAMS = 1  # stop this many grams early (feed still falling after servo closes)
DISPENSE_BULK_MIN_AMOUNT_G = 60
DISPENSE_MIN_PULSE_SECONDS = 0.08

# Bulk-phase feed dispensing
DISPENSE_BULK_FRACTION = 0.2      # portion of the requested amount delivered in one continuous pulse
DISPENSE_BULK_MAX_SECONDS = 5     # safety cap on the bulk pulse duration
DISPENSE_MIN_RATE_GPS = 0.3       # floor for the assumed dispense rate
DISPENSE_SETTLE_SECONDS = 0.6     # how long feed keeps trickling after the servo fully closes

EXHAUST_MIN_RUN_SECONDS = 180     # fan must run at least this long once it starts

FEED_DISPENSE_TIMEOUT_SECONDS = 30  # fail-safe only

# ---- Feed container (ultrasonic) ----
FEED_BIN_LOW_PERCENT = 20
FEED_BIN_EMPTY_PERCENT = 5
FEED_BIN_HYSTERESIS_PERCENT = 5
FEED_BIN_FULL_PERCENT = 80        # at or above this the LCD / app says "Full", below it "Normal"
FEED_BIN_LOW_CONFIRM_SECONDS = 600     # must read low/empty this long (straight) before it counts
FEED_BIN_REFILL_CONFIRM_SECONDS = 60   # must read OK this long before "refilled" is announced

# ---- Water container (second float) ----
WATER_BIN_LOW_CONFIRM_SECONDS = 600    # must read low this long (straight) before the alert / "Low" status
WATER_BIN_REFILL_CONFIRM_SECONDS = 60  # must read OK this long before "refilled" is announced
# (auto-refill still refuses to run the instant the container float reads empty - that protects the pump)

# ---- Servo safety ----
SERVO_HANDOFF_SECONDS = 0.5            # gap after one servo closes before the other may open
FEED_SERVO_MAX_OPEN_SECONDS = 15       # watchdog: one continuous feed-servo open longer than this = stuck
WATER_SERVO_MAX_OPEN_SECONDS = 350     # watchdog: one continuous water-servo open longer than this = stuck
FEED_FAIL_MIN_REQUEST_G = 10           # only judge the feed servo on dispenses of at least this many grams
FEED_FAIL_CONFIRM_DISPENSES = 2        # this many dispenses in a row with ~no feed out = servo/hopper fault

# ---- Component health ----
COMPONENT_FAULT_CONFIRM_SECONDS = 10     # (the "no reading" limits below already wait long enough)
COMPONENT_RECOVER_CONFIRM_SECONDS = 120  # must be good this long before "recovered" (keep above the 60 s boot grace)
COMPONENT_REALERT_COOLDOWN_SECONDS = 3600  # a part that faults again within this time is NOT re-notified
ALERT_REPEAT_COOLDOWN_SECONDS = 1800       # same alert type (temp / humidity / low feed) is not re-sent within this time

SENSOR_NO_READING_SECONDS = {"dht22": 120, "load_cell": 60, "feed_bin": 120}
# Same value for this long = stuck.
SENSOR_STUCK_SECONDS = {"dht22": 2 * 3600, "load_cell": 600, "feed_bin": 6 * 3600}

# ---- Lamp / fan "is it really doing its job" checks ----
# These look at the temperature / humidity change after a part switches ON.
# There is no sensor on the part itself, so they are heuristics. Set to False to turn them off.
ACTUATOR_EFFECT_CHECK_ENABLED = True
LAMP_CHECK_SECONDS = 1200     # lamp ON this long without warming the coop = suspect
LAMP_MIN_RISE_C = 0.5
FAN_CHECK_SECONDS = 1800      # fan ON this long without improving temp/humidity = suspect
FAN_MIN_DROP_C = 0.3
FAN_MIN_DROP_HUM = 2.0

ACTUATOR_EFFECT_CHECK_ENABLED = False