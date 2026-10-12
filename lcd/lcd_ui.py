import tkinter as tk
from tkinter import font as tkfont
from PIL import Image, ImageTk
import json
import os
import time
from datetime import datetime

SHARED_STATE_FILE = "shared_state.json"
LCD_COMMANDS_FILE = "lcd_commands.json"
HEARTBEAT_FILE = "lcd_heartbeat.json"   # written every few seconds so other tools can tell the LCD is alive
POLL_MS = 50             # how often the dashboard refreshes from shared_state.json
STALE_SECONDS = 15       # if shared_state.json is older than this, main.py is assumed down
HEARTBEAT_EVERY = 5      # seconds between heartbeat file writes

# Fallback only, used until the first shared_state.json read arrives with
# the live feed_capacity_grams value (set by the user in the app's
# Settings screen). Not the source of truth.
FEED_CAPACITY_GRAMS = 200

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
LOGO_PATH = os.path.join(ASSETS_DIR, "poultrycare_logo.png")

# Colors (simple, high-contrast for a small touchscreen)
BG_DARK = "#1e2328"
BG_CARD = "#2a3038"
BG_CARD_ALT = "#323a44"
BG_ALERT = "#4a2a29"
ACCENT_GREEN = "#4caf7d"
ACCENT_RED = "#e5605a"
ACCENT_BLUE = "#4a90d9"
ACCENT_AMBER = "#e0a63a"
TEXT_MAIN = "#f2f2f2"
TEXT_DIM = "#9aa4ae"

BADGE_LOW = ("#4a4326", "#e0c568")
BADGE_NORMAL = ("#25402f", "#7fd9a8")
BADGE_HIGH = ("#4a2a29", "#e5807c")
BADGE_INFO = ("#1f3a52", "#7fb8ef")

ACTUATOR_ON_BG = "#3a3320"
ACTUATOR_OFF_BG = BG_CARD_ALT

START_BG = "#f4f3ef"
START_TEXT = "#1e2328"


class PoultryDashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PoultryCare Dashboard")
        self.geometry("800x480")
        self.attributes("-fullscreen", True)
        self.configure(bg=BG_DARK)
        self.bind("<Escape>", lambda e: self.attributes("-fullscreen", False))

        # Live hopper capacity, updated from shared_state.json every poll.
        # Must be set BEFORE _poll_state() runs.
        self.feed_capacity_grams = FEED_CAPACITY_GRAMS

        # Fail-safe bookkeeping
        self.last_good_read = time.time()   # last time shared_state.json was read OK
        self.controller_ok = False          # False until fresh data arrives; blocks commands
        self.poll_errors = 0
        self.transient_status = None        # (text, color, expire_time)
        self.heartbeat_on = False
        self.last_heartbeat_write = 0

        self.font_big = tkfont.Font(family="DejaVu Sans", size=30, weight="bold")
        self.font_med = tkfont.Font(family="DejaVu Sans", size=18, weight="bold")
        self.font_small = tkfont.Font(family="DejaVu Sans", size=13, weight="bold")
        self.font_badge = tkfont.Font(family="DejaVu Sans", size=13, weight="bold")
        self.font_button = tkfont.Font(family="DejaVu Sans", size=16, weight="bold")
        self.font_tiny = tkfont.Font(family="DejaVu Sans", size=12, weight="normal")
        self.font_title = tkfont.Font(family="DejaVu Sans", size=32, weight="bold")

        # Container holds both screens stacked on top of each other.
        self.container = tk.Frame(self, bg=BG_DARK)
        self.container.pack(fill="both", expand=True)

        self.start_screen = tk.Frame(self.container, bg=START_BG)
        self.dashboard_screen = tk.Frame(self.container, bg=BG_DARK)

        for screen in (self.start_screen, self.dashboard_screen):
            screen.place(x=0, y=0, relwidth=1, relheight=1)

        self._build_start_screen()
        self._build_layout()

        self.start_screen.tkraise()

        self._tick_clock()
        self._poll_state()

    # -- Start / splash screen -------------------------------------------
    def _build_start_screen(self):
        self.start_screen.grid_rowconfigure(0, weight=1)
        self.start_screen.grid_rowconfigure(1, weight=0)
        self.start_screen.grid_rowconfigure(2, weight=0)
        self.start_screen.grid_rowconfigure(3, weight=0)
        self.start_screen.grid_rowconfigure(4, weight=1)
        self.start_screen.grid_columnconfigure(0, weight=1)

        try:
            img = Image.open(LOGO_PATH).convert("RGBA")
            img = img.resize((200, 209), Image.LANCZOS)
            self.logo_image = ImageTk.PhotoImage(img)
            logo_label = tk.Label(self.start_screen, image=self.logo_image, bg=START_BG)
        except Exception:
            # Fallback if the logo file isn't found next to the script
            logo_label = tk.Label(
                self.start_screen, text="🐔", font=("DejaVu Sans", 80), bg=START_BG
            )

        logo_label.grid(row=1, column=0, pady=(0, 8))

        title_label = tk.Label(
            self.start_screen, text="PoultryCare", font=self.font_title,
            bg=START_BG, fg=START_TEXT
        )
        title_label.grid(row=2, column=0, pady=(0, 30))

        enter_btn = tk.Button(
            self.start_screen, text="Get Started", font=self.font_button,
            bg=ACCENT_GREEN, fg="white", activebackground="#3d8a63",
            relief="flat", width=18, height=2, bd=0, cursor="hand2",
            command=self._show_dashboard
        )
        enter_btn.grid(row=3, column=0)

    def _show_dashboard(self):
        self.dashboard_screen.tkraise()

    # -- Main dashboard ----------------------------------------------------
    def _build_layout(self):
        self.status_bar = tk.Frame(self.dashboard_screen, bg=BG_CARD, height=40)
        self.status_bar.pack(fill="x", side="top")
        self.status_bar.pack_propagate(False)

        # Exit button - packed first on the right so it sits in the
        # very top-right corner of the screen.
        self.exit_btn = tk.Button(
            self.status_bar, text="✕", font=self.font_small,
            bg=BG_CARD, fg=TEXT_DIM, activebackground=ACCENT_RED,
            activeforeground="white", relief="flat", bd=0, width=3,
            cursor="hand2", command=self._exit_app
        )
        self.exit_btn.pack(side="right", padx=(0, 10))

        self.time_label = tk.Label(
            self.status_bar, text="", font=self.font_small, bg=BG_CARD, fg=TEXT_DIM
        )
        self.time_label.pack(side="right", padx=(15, 4))

        # Heartbeat dot: blinks every second. If it stops blinking, the
        # dashboard itself has frozen.
        self.heartbeat_label = tk.Label(
            self.status_bar, text="●", font=self.font_small, bg=BG_CARD, fg=TEXT_DIM
        )
        self.heartbeat_label.pack(side="right", padx=(0, 4))

        self.online_label = tk.Label(
            self.status_bar, text="● System OFFLINE", font=self.font_small,
            bg=BG_CARD, fg=ACCENT_RED
        )
        self.online_label.pack(side="left", padx=15)

        title = tk.Label(
            self.status_bar, text="PoultryCare", font=self.font_med,
            bg=BG_CARD, fg=TEXT_MAIN
        )
        title.pack(side="left", padx=5)

        content = tk.Frame(self.dashboard_screen, bg=BG_DARK)
        content.pack(fill="both", expand=True, padx=10, pady=3)

        left = tk.Frame(content, bg=BG_DARK, width=380)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        left.pack_propagate(False)

        right = tk.Frame(content, bg=BG_DARK, width=380)
        right.pack(side="right", fill="both", expand=True, padx=(8, 0))
        right.pack_propagate(False)

        # ---- Left column: sensors, actuators, next schedule ----
        sensor_grid = tk.Frame(left, bg=BG_DARK, height=258)
        sensor_grid.pack(fill="x", pady=(0, 3))
        sensor_grid.pack_propagate(False)
        sensor_grid.grid_columnconfigure(0, weight=1)
        sensor_grid.grid_columnconfigure(1, weight=1)
        sensor_grid.grid_rowconfigure(0, weight=1)
        sensor_grid.grid_rowconfigure(1, weight=1)

        self.temp_card, self.temp_value, self.temp_badge = self._make_reading_card(sensor_grid, "Temperature", "-- °C")
        self.temp_card.grid(row=0, column=0, sticky="nsew", padx=(0, 4), pady=(0, 3))

        self.hum_card, self.hum_value, self.hum_badge = self._make_reading_card(sensor_grid, "Humidity", "-- %")
        self.hum_card.grid(row=0, column=1, sticky="nsew", padx=(4, 0), pady=(0, 3))

        self.feed_card, self.feed_value, self.feed_badge = self._make_reading_card(sensor_grid, "Feed Level", "-- %")
        self.feed_card.grid(row=1, column=0, sticky="nsew", padx=(0, 4), pady=(3, 0))

        # "Drinker" = the trough the chickens drink from (float sensor 1).
        # It refills automatically, so there is no water button anymore.
        self.water_card, self.water_value, self.water_badge = self._make_reading_card(sensor_grid, "Drinker", "--")
        self.water_card.grid(row=1, column=1, sticky="nsew", padx=(4, 0), pady=(3, 0))

        actuator_section = tk.Frame(left, bg=BG_DARK, height=108)
        actuator_section.pack(fill="x", pady=(0, 3))
        actuator_section.pack_propagate(False)

        actuator_title = tk.Label(
            actuator_section, text="Environmental Control Status", font=self.font_small,
            bg=BG_DARK, fg=TEXT_DIM, anchor="w"
        )
        actuator_title.pack(fill="x", pady=(0, 2))

        actuator_row = tk.Frame(actuator_section, bg=BG_DARK)
        actuator_row.pack(fill="both", expand=True)
        actuator_row.grid_columnconfigure(0, weight=1)
        actuator_row.grid_columnconfigure(1, weight=1)
        actuator_row.grid_rowconfigure(0, weight=1)

        self.fan_card, self.fan_state_label = self._make_actuator_card(actuator_row, "Exhaust Fan")
        self.fan_card.grid(row=0, column=0, sticky="nsew", padx=(0, 4))

        self.lamp_card, self.lamp_state_label = self._make_actuator_card(actuator_row, "Heating Lamp")
        self.lamp_card.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        self.schedule_card = tk.Frame(left, bg=BG_CARD_ALT, height=72)
        self.schedule_card.pack(fill="x")
        self.schedule_card.pack_propagate(False)

        schedule_label = tk.Label(
            self.schedule_card, text="Next Scheduled Feed", font=self.font_small,
            bg=BG_CARD_ALT, fg=TEXT_MAIN, anchor="w"
        )
        schedule_label.pack(fill="x", padx=12, pady=(4, 0))

        self.schedule_value = tk.Label(
            self.schedule_card, text="No schedule set", font=self.font_med,
            bg=BG_CARD_ALT, fg=TEXT_MAIN, anchor="w"
        )
        self.schedule_value.pack(fill="x", padx=12, pady=(0, 2))

        # ---- Right column: manual feed, feed container, system check ----
        # Status line is packed to the bottom FIRST so it can never be
        # pushed off-screen by the cards above it.
        self.status_label = tk.Label(
            right, text="", font=self.font_small, bg=BG_DARK, fg=ACCENT_AMBER,
            anchor="w", justify="left", wraplength=370
        )
        self.status_label.pack(side="bottom", fill="x", pady=(2, 0))

        controls_label = tk.Label(
            right, text="Manual Dispense", font=self.font_med,
            bg=BG_DARK, fg=TEXT_MAIN, anchor="w"
        )
        controls_label.pack(fill="x", pady=(0, 4))

        feed_frame = tk.Frame(right, bg=BG_DARK)
        feed_frame.pack(fill="x", pady=(0, 6))

        feed_title = tk.Label(
            feed_frame, text="Feed", font=self.font_med, bg=BG_DARK, fg=TEXT_MAIN, anchor="w"
        )
        feed_title.pack(fill="x")

        # Manual dispense steps in 10% increments of the LIVE hopper
        # capacity (self.feed_capacity_grams, kept in sync from shared_state.json).
        self.custom_percent = tk.IntVar(value=10)
        self.custom_percent_display = tk.StringVar(value="10 %")

        custom_frame = tk.Frame(feed_frame, bg=BG_DARK)
        custom_frame.pack(fill="x", pady=(6, 0))

        custom_title = tk.Label(
            custom_frame, text="Feed Amount (% of hopper capacity):", font=self.font_small,
            bg=BG_DARK, fg=TEXT_MAIN, anchor="w"
        )
        custom_title.pack(fill="x")

        stepper_row = tk.Frame(custom_frame, bg=BG_DARK)
        stepper_row.pack(fill="x", pady=(4, 0))

        minus_btn = tk.Button(
            stepper_row, text="−", font=self.font_button,
            bg=BG_CARD_ALT, fg="white", activebackground="#3f4854",
            relief="flat", width=3, height=1,
            command=lambda: self._adjust_custom_percent(-10)
        )
        minus_btn.pack(side="left", fill="y", padx=(0, 6))

        self.custom_percent_label = tk.Label(
            stepper_row, textvariable=self.custom_percent_display, font=self.font_med,
            bg=BG_CARD_ALT, fg=TEXT_MAIN, width=8
        )
        self.custom_percent_label.pack(side="left", fill="both", expand=True, padx=6)

        plus_btn = tk.Button(
            stepper_row, text="+", font=self.font_button,
            bg=BG_CARD_ALT, fg="white", activebackground="#3f4854",
            relief="flat", width=3, height=1,
            command=lambda: self._adjust_custom_percent(10)
        )
        plus_btn.pack(side="left", fill="y", padx=(6, 0))

        self.custom_dispense_btn = tk.Button(
            custom_frame, text="Dispense Feeds", font=self.font_button,
            bg=ACCENT_GREEN, fg="white", activebackground="#3d8a63",
            relief="flat", height=2,
            command=lambda: self._dispense_feed(self.custom_percent.get())
        )
        self.custom_dispense_btn.pack(fill="x", pady=(4, 0))

        manual_note = tk.Label(
            right,
            text="Water refills automatically. Use the button above to dispense feed manually.",
            font=self.font_tiny, bg=BG_DARK, fg=TEXT_DIM, anchor="w",
            justify="left", wraplength=360
        )
        manual_note.pack(fill="x", pady=(0, 2))

        # Feed container card (ultrasonic sensor on top of the feed container)
        self.bin_card = tk.Frame(right, bg=BG_CARD)
        self.bin_card.pack(fill="x", pady=(4, 3))
        self.bin_label = tk.Label(
            self.bin_card, text="Feed Container", font=self.font_small,
            bg=BG_CARD, fg=TEXT_DIM, anchor="w"
        )
        self.bin_label.pack(side="left", padx=12, pady=8)
        self.bin_value = tk.Label(
            self.bin_card, text="--", font=self.font_med,
            bg=BG_CARD, fg=TEXT_MAIN, anchor="e"
        )
        self.bin_value.pack(side="right", padx=12, pady=6)

        # System check card: plain-language problems for non-technical users.
        self.health_card = tk.Frame(right, bg=BG_CARD)
        self.health_card.pack(fill="x", pady=(3, 0))
        self.health_title = tk.Label(
            self.health_card, text="System Check: --", font=self.font_small,
            bg=BG_CARD, fg=TEXT_DIM, anchor="w"
        )
        self.health_title.pack(fill="x", padx=12, pady=(5, 2))

        self.health_msg = tk.Label(
            self.health_card, text="", font=self.font_tiny, bg=BG_CARD, fg=TEXT_DIM,
            anchor="w", justify="left", wraplength=350
        )
        self.health_msg.pack(fill="x", padx=12, pady=(0, 6))

    def _make_reading_card(self, parent, label_text, initial_value):
        card = tk.Frame(parent, bg=BG_CARD)
        label = tk.Label(card, text=label_text, font=self.font_small, bg=BG_CARD, fg=TEXT_DIM, anchor="w")
        label.pack(fill="x", padx=12, pady=(4, 0))
        value = tk.Label(card, text=initial_value, font=self.font_big, bg=BG_CARD, fg=TEXT_MAIN, anchor="w")
        value.pack(fill="x", padx=12, pady=(0, 2))
        badge = tk.Label(card, text="--", font=self.font_badge, bg=BADGE_NORMAL[0], fg=BADGE_NORMAL[1], anchor="w", padx=7, pady=0)
        badge.pack(fill="none", anchor="w", padx=12, pady=(0, 4))
        return card, value, badge

    def _make_actuator_card(self, parent, label_text):
        card = tk.Frame(parent, bg=ACTUATOR_OFF_BG)
        label = tk.Label(card, text=label_text, font=self.font_small, bg=ACTUATOR_OFF_BG, fg=TEXT_DIM, anchor="w")
        label.pack(fill="x", padx=12, pady=(5, 0))
        state_label = tk.Label(card, text="OFF", font=self.font_med, bg=ACTUATOR_OFF_BG, fg=TEXT_DIM, anchor="w")
        state_label.pack(fill="x", padx=12, pady=(0, 4))
        card._name_label = label
        return card, state_label

    def _set_badge(self, badge_label, text, level):
        colors = {
            "low": BADGE_LOW, "normal": BADGE_NORMAL,
            "high": BADGE_HIGH, "info": BADGE_INFO,
        }.get(level, BADGE_NORMAL)
        badge_label.config(text=text, bg=colors[0], fg=colors[1])

    def _set_actuator_card(self, card, state_label, is_on):
        bg = ACTUATOR_ON_BG if is_on else ACTUATOR_OFF_BG
        fg = ACCENT_AMBER if is_on else TEXT_DIM
        card.config(bg=bg)
        card._name_label.config(bg=bg)
        state_label.config(text="ON" if is_on else "OFF", bg=bg, fg=fg)

    def _set_bin(self, percent, state, sensor_ok, dim=False):
        """Feed container card. state: ok / low / empty."""
        if not sensor_ok:
            bg, text, fg = BG_ALERT, "⚠ Sensor problem", ACCENT_RED
        elif percent is None:
            bg, text, fg = BG_CARD, "--", TEXT_DIM
        elif state == "empty":
            bg, text, fg = BG_ALERT, "⚠ EMPTY", ACCENT_RED
        elif state == "low":
            bg, text, fg = BG_ALERT, f"⚠ LOW {percent:.0f} %", ACCENT_RED
        else:
            bg, text, fg = BG_CARD, f"Has feed {percent:.0f} %", (TEXT_DIM if dim else ACCENT_GREEN)
        self.bin_card.config(bg=bg)
        self.bin_label.config(bg=bg)
        self.bin_value.config(text=text, bg=bg, fg=fg)

    # -- Fail-safe: component health (plain language) ---------------------
    PART_NAMES = {
        "dht22": "Temperature & humidity sensor",
        "load_cell": "Feed scale",
        "feed_bin": "Feed container sensor",
    }

    def _problem_text(self, key, reason):
        if key == "water_refill":
            return "Drinker is not filling up. Check the water valve and float."
        name = self.PART_NAMES[key]
        if reason == "stuck":
            return f"{name}: the reading has not changed for a long time. It may be stuck."
        return f"{name}: not working. Please check its cable."

    def _update_health(self, state, controller_ok):
        if not controller_ok:
            self.health_title.config(text="System Check: ⚠ Controller not responding", fg=ACCENT_RED)
            self.health_msg.config(text="The main controller has stopped. Please restart the device.",
                                   fg=ACCENT_RED)
            self._set_health_bg(BG_ALERT)
            return

        health = state.get("sensor_health")
        health = health if isinstance(health, dict) else {}
        reasons = state.get("sensor_fault_reason") or {}
        problems = [self._problem_text(k, reasons.get(k))
                    for k in ("dht22", "load_cell", "feed_bin", "water_refill")
                    if health.get(k, True) is False]

        if problems:
            n = len(problems)
            shown = problems[:2]
            if n > 2:
                shown.append(f"+ {n - 2} more problem{'s' if n - 2 > 1 else ''}")
            self.health_title.config(
                text=f"System Check: ⚠ {n} problem{'s' if n > 1 else ''}", fg=ACCENT_RED)
            self.health_msg.config(text="\n".join(shown), fg=TEXT_MAIN)
            self._set_health_bg(BG_ALERT)
        else:
            self.health_title.config(text="System Check: ✓ All OK", fg=ACCENT_GREEN)
            self.health_msg.config(text="All parts are working normally.", fg=TEXT_DIM)
            self._set_health_bg(BG_CARD)

    def _set_health_bg(self, bg):
        self.health_card.config(bg=bg)
        self.health_title.config(bg=bg)
        self.health_msg.config(bg=bg)

    # -- Controls ----------------------------------------------------------
    def _adjust_custom_percent(self, delta):
        # Steps 10 -> 20 -> ... -> 100%. 100% caps at the full live
        # hopper capacity (self.feed_capacity_grams).
        MIN_PERCENT = 10
        MAX_PERCENT = 100
        new_value = self.custom_percent.get() + delta
        new_value = max(MIN_PERCENT, min(MAX_PERCENT, new_value))
        self.custom_percent.set(new_value)
        self.custom_percent_display.set(f"{new_value} %")

    def _exit_app(self):
        self.destroy()

    def _flash_status(self, text, color, seconds=3):
        self.transient_status = (text, color, time.time() + seconds)
        self.status_label.config(text=text, fg=color)

    def _dispense_feed(self, percent):
        # Fail-safe: don't queue a command if the controller isn't running,
        # otherwise it would fire unexpectedly when it comes back.
        if not self.controller_ok:
            self._flash_status("Controller offline - feed NOT sent", ACCENT_RED, seconds=4)
            return

        grams = round((percent / 100) * self.feed_capacity_grams)

        self._flash_button(self.custom_dispense_btn, f"Sending {percent}%...")
        if self._write_command(feed_requested=True, feed_grams=grams):
            self._flash_status(f"Feed command sent: {percent}% ({grams}g)", ACCENT_AMBER)

    def _flash_button(self, button, temp_text, restore_ms=600):
        original_text = button.cget("text")
        button.config(text=temp_text, state="disabled")

        def restore():
            button.config(text=original_text, state="normal")

        self.after(restore_ms, restore)

    def _write_command(self, feed_requested=False, feed_grams=None):
        # water_dispense stays in the file (always False) so the Pi's
        # command reader keeps working unchanged.
        command = {
            "feed_dispense": {"requested": feed_requested, "grams": feed_grams},
            "water_dispense": {"requested": False}
        }
        try:
            tmp_path = LCD_COMMANDS_FILE + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(command, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, LCD_COMMANDS_FILE)
            return True
        except Exception as e:
            self._flash_status(f"Error sending command: {e}", ACCENT_RED, seconds=5)
            return False

    # -- Clock + heartbeat --------------------------------------------------
    def _tick_clock(self):
        try:
            self.time_label.config(text=datetime.now().strftime("%a %d %b  %I:%M:%S %p"))
            self.heartbeat_on = not self.heartbeat_on
            self.heartbeat_label.config(fg=ACCENT_GREEN if self.heartbeat_on else TEXT_DIM)
            self._write_heartbeat()
        except Exception as e:
            print(f"Clock tick error: {e}")
        finally:
            self.after(1000, self._tick_clock)

    def _write_heartbeat(self):
        now = time.time()
        if now - self.last_heartbeat_write < HEARTBEAT_EVERY:
            return
        self.last_heartbeat_write = now
        try:
            tmp = HEARTBEAT_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"alive": now, "pid": os.getpid()}, f)
            os.replace(tmp, HEARTBEAT_FILE)
        except Exception as e:
            print(f"Heartbeat write failed: {e}")

    # -- State polling -----------------------------------------------------
    def _poll_state(self):
        # try/finally guarantees the next poll is ALWAYS scheduled, even if
        # drawing the screen throws, so the display can never silently freeze.
        try:
            state = self._read_shared_state()
            if state:
                self.last_good_read = time.time()
                self._update_display(state)
                self.poll_errors = 0
            elif time.time() - self.last_good_read > STALE_SECONDS:
                self._show_no_data()
        except Exception as e:
            self.poll_errors += 1
            self._show_display_error(e)
        finally:
            self.after(POLL_MS, self._poll_state)

    def _read_shared_state(self):
        if not os.path.exists(SHARED_STATE_FILE):
            return None
        try:
            with open(SHARED_STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return None

    def _show_no_data(self):
        """shared_state.json missing/unreadable for a while: controller is down."""
        self.controller_ok = False
        self.online_label.config(text="● System OFFLINE", fg=ACCENT_RED)
        self._update_health({}, controller_ok=False)
        self.status_label.config(
            text="No data from controller (shared_state.json missing or unreadable)",
            fg=ACCENT_RED)

    def _show_display_error(self, error):
        print(f"Display update error: {error}")
        try:
            self.health_title.config(text="System Check: ⚠ Display error", fg=ACCENT_RED)
            self.status_label.config(text=f"Display error: {error}", fg=ACCENT_RED)
        except Exception:
            pass

    def _update_display(self, state):
        temp = state.get("temperature")
        hum = state.get("humidity")
        water = state.get("water_level")
        tank = state.get("container_level")
        online = state.get("system_online", False)
        internet_online = state.get("internet_online", True)
        last_updated = state.get("last_updated", "")
        try:
            age = (datetime.now() - datetime.strptime(last_updated, "%Y-%m-%d %H:%M:%S")).total_seconds()
            data_stale = age > STALE_SECONDS
        except Exception:
            data_stale = True

        controller_ok = bool(online) and not data_stale
        self.controller_ok = controller_ok
        val_fg = TEXT_DIM if data_stale else TEXT_MAIN   # dim old numbers so they aren't trusted

        is_feeding = state.get("is_dispensing_feed", False)
        is_watering = state.get("is_dispensing_water", False)
        temp_min = state.get("temp_min_threshold")
        temp_max = state.get("temp_max_threshold")
        hum_min = state.get("hum_min_threshold")
        hum_max = state.get("hum_max_threshold")
        heating_lamp_on = state.get("heating_lamp_on", False)
        exhaust_fan_on = state.get("exhaust_fan_on", False)
        next_schedule = state.get("next_schedule")

        # Whole numbers for temperature and humidity
        self.temp_value.config(text=f"{temp:.0f} °C" if temp is not None else "-- °C", fg=val_fg)
        if temp is not None and temp_min is not None and temp_max is not None:
            if temp < temp_min:
                self._set_badge(self.temp_badge, "Low", "low")
            elif temp > temp_max:
                self._set_badge(self.temp_badge, "High", "high")
            else:
                self._set_badge(self.temp_badge, "Normal", "normal")
        else:
            self._set_badge(self.temp_badge, "--", "normal")

        self.hum_value.config(text=f"{hum:.0f} %" if hum is not None else "-- %", fg=val_fg)
        if hum is not None and hum_min is not None and hum_max is not None:
            if hum < hum_min:
                self._set_badge(self.hum_badge, "Low", "low")
            elif hum > hum_max:
                self._set_badge(self.hum_badge, "High", "high")
            else:
                self._set_badge(self.hum_badge, "Normal", "normal")
        else:
            self._set_badge(self.hum_badge, "--", "normal")

        self.feed_capacity_grams = state.get("feed_capacity_grams", self.feed_capacity_grams)

        # Feed level: use the percentage the Pi already computed.
        feed_pct = state.get("feed_percent")
        is_low = state.get("feed_low_active", False)
        if feed_pct is not None:
            feed_pct = max(0, min(100, feed_pct))
            feed_color = ACCENT_RED if (is_low and not data_stale) else val_fg
            self.feed_value.config(text=f"{feed_pct:.0f} %", fg=feed_color)
            self._set_badge(self.feed_badge, "Low" if is_low else "Normal", "low" if is_low else "normal")
        else:
            self.feed_value.config(text="-- %", fg=val_fg)
            self._set_badge(self.feed_badge, "--", "normal")

        # Drinker card = ONE water indicator.
        # Priority: tank empty (float sensor 2) > refilling > drinker low (float sensor 1) > normal
        if tank == "empty":
            self.water_value.config(text="Empty", fg=ACCENT_RED)
            self._set_badge(self.water_badge, "Refill tank", "high")
        elif is_watering:
            self.water_value.config(text="Refilling", fg=ACCENT_BLUE)
            self._set_badge(self.water_badge, "Auto-refill", "info")
        elif water == "low":
            self.water_value.config(text="Low", fg=ACCENT_RED if not data_stale else val_fg)
            self._set_badge(self.water_badge, "Low", "low")
        elif water == "normal":
            self.water_value.config(text="Normal", fg=val_fg)
            self._set_badge(self.water_badge, "Normal", "normal")
        else:
            self.water_value.config(text="--", fg=val_fg)
            self._set_badge(self.water_badge, "--", "normal")

        # Feed container (ultrasonic)
        bin_pct = state.get("feed_bin_percent")
        bin_low = state.get("feed_bin_low_active", False)
        bin_state = state.get("feed_bin_state") or ("low" if bin_low else "ok")
        health = state.get("sensor_health")
        bin_ok = bool(health.get("feed_bin", True)) if isinstance(health, dict) else True
        self._set_bin(bin_pct, bin_state, bin_ok, dim=data_stale)

        self._set_actuator_card(self.fan_card, self.fan_state_label, exhaust_fan_on)
        self._set_actuator_card(self.lamp_card, self.lamp_state_label, heating_lamp_on)

        if not controller_ok:
            self.online_label.config(text="● System OFFLINE", fg=ACCENT_RED)
        elif internet_online:
            self.online_label.config(text="● Online Mode", fg=ACCENT_GREEN)
        else:
            self.online_label.config(text="● Offline Mode", fg=ACCENT_AMBER)

        self._update_health(state, controller_ok)

        # A schedule is only considered valid if it actually has a time set.
        if next_schedule and next_schedule.get("time"):
            day = next_schedule.get("day", "")
            sched_time = next_schedule.get("time", "")
            amount = next_schedule.get("amount_grams", "")
            self.schedule_value.config(text=f"{day} {sched_time} · {amount}g")
        else:
            self.schedule_value.config(text="No schedule set")

        # Status line, most important message first
        msg, color = "", ACCENT_AMBER
        transient = self.transient_status
        if not controller_ok:
            msg, color = "Controller not responding - showing last known data", ACCENT_RED
        elif transient and time.time() < transient[2]:
            msg, color = transient[0], transient[1]
        elif is_feeding:
            msg, color = "Dispensing feed...", ACCENT_AMBER
        elif is_watering:
            msg, color = "Refilling drinker...", ACCENT_BLUE
        elif state.get("water_refill_failed", False):
            msg, color = "Water refill failed - check valve / float", ACCENT_RED
        elif bin_state == "empty" and bin_ok:
            msg, color = "Feed container is EMPTY - please refill", ACCENT_RED
        elif bin_state == "low" and bin_ok:
            msg, color = "Feed container is low - please refill soon", ACCENT_RED
        self.status_label.config(text=msg, fg=color)


if __name__ == "__main__":
    app = PoultryDashboard()
    app.mainloop()