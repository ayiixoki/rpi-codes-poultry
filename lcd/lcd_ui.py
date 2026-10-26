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
BG_WARN = "#3a3320"
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

# ---- Layout ----
DESIGN_W, DESIGN_H = 800, 480   # the 7-inch DSI touchscreen
PAD_X = 8                       # left/right margin around the two columns
COL_GAP = 10                    # gap between the left and right column
MAX_BOOST = 1.25                # how much bigger than the base font sizes the UI may grow


class PoultryDashboard(tk.Tk):
    # Base font sizes in PIXELS (negative size = pixels in tkinter).
    # _fit_to_window() picks the largest scale at which everything still
    # fits on the real window, so the layout fills the screen.
    FONT_PX = {"big": 38, "word": 28, "med": 22, "small": 15,
               "badge": 14, "button": 20, "tiny": 13}

    def __init__(self):
        super().__init__()
        self.title("PoultryCare Dashboard")
        self.geometry("800x480")
        self.attributes("-fullscreen", True)
        self.configure(bg=BG_DARK)
        self.bind("<Escape>", lambda e: self.attributes("-fullscreen", False))

        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.base_scale = max(0.5, min(sw / DESIGN_W, sh / DESIGN_H))
        self.ui_scale = self.base_scale

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

        # Dashboard fonts (resized automatically by _fit_to_window)
        self.fonts = {}
        for name, px in self.FONT_PX.items():
            self.fonts[name] = tkfont.Font(
                family="DejaVu Sans", size=-int(px * self.base_scale),
                weight="normal" if name == "tiny" else "bold")
        self.font_big = self.fonts["big"]
        self.font_word = self.fonts["word"]
        self.font_med = self.fonts["med"]
        self.font_small = self.fonts["small"]
        self.font_badge = self.fonts["badge"]
        self.font_button = self.fonts["button"]
        self.font_tiny = self.fonts["tiny"]

        # Start screen fonts (fixed size)
        self.font_title = tkfont.Font(
            family="DejaVu Sans", size=-int(38 * self.base_scale), weight="bold")
        self.font_start_btn = tkfont.Font(
            family="DejaVu Sans", size=-int(22 * self.base_scale), weight="bold")

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

        # Re-fit whenever the window size changes (also runs once at startup)
        self._fit_job = None
        self.bind("<Configure>", self._schedule_fit)
        self._schedule_fit()

        self._tick_clock()
        self._poll_state()

    # -- Auto-fit to the real window size -------------------------------------
    def _schedule_fit(self, event=None):
        if event is not None and event.widget is not self:
            return
        if self._fit_job is not None:
            self.after_cancel(self._fit_job)
        self._fit_job = self.after(250, self._fit_to_window)

    def _apply_scale(self, k):
        self.ui_scale = k
        for name, px in self.FONT_PX.items():
            self.fonts[name].configure(size=-max(8, int(round(px * k))))

    def _fit_to_window(self):
        self._fit_job = None
        self.update_idletasks()
        avail_w, avail_h = self.winfo_width(), self.winfo_height()
        if avail_w < 300 or avail_h < 200:      # window not shown yet
            avail_w, avail_h = self.winfo_screenwidth(), self.winfo_screenheight()
        avail_h -= 4

        col_w = (avail_w - 2 * PAD_X - COL_GAP) // 2
        card_w = (col_w - 5) // 2               # one sensor / container card

        self.status_label.config(wraplength=max(150, col_w - 6))
        self.health_msg.config(wraplength=max(150, col_w - 26))

        # Worst-case texts that must still fit inside their card.
        checks = [
            (self.font_big, "100 %", card_w - 28),
            (self.font_word, "Refilling", card_w - 28),
            (self.font_med, "Sensor error", card_w - 20),
            (self.font_med, "Wednesday 12:00 PM · 150g", col_w - 24),
        ]

        def fits():
            if self.dashboard_screen.winfo_reqheight() > avail_h:
                return False
            if self.dashboard_screen.winfo_reqwidth() > avail_w:
                return False
            for font, sample, room in checks:
                if font.measure(sample) > room:
                    return False
            return True

        k = self.base_scale * MAX_BOOST
        floor = self.base_scale * 0.5
        while True:
            self._apply_scale(k)
            self.update_idletasks()
            if fits() or k <= floor:
                break
            k *= 0.96

    # -- Layout helper ----------------------------------------------------------
    def _stack(self, card, widgets, padx=12):
        """Put widgets in a column, vertically centered inside the card."""
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(0, weight=1)
        for i, (w, sticky) in enumerate(widgets, start=1):
            w.grid(row=i, column=0, sticky=sticky, padx=padx)
        card.grid_rowconfigure(len(widgets) + 1, weight=1)

    # -- Start / splash screen -------------------------------------------
    def _build_start_screen(self):
        s = self.base_scale
        self.start_screen.grid_rowconfigure(0, weight=1)
        self.start_screen.grid_rowconfigure(1, weight=0)
        self.start_screen.grid_rowconfigure(2, weight=0)
        self.start_screen.grid_rowconfigure(3, weight=0)
        self.start_screen.grid_rowconfigure(4, weight=1)
        self.start_screen.grid_columnconfigure(0, weight=1)

        try:
            img = Image.open(LOGO_PATH).convert("RGBA")
            img = img.resize((int(200 * s), int(209 * s)), Image.LANCZOS)
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
        title_label.grid(row=2, column=0, pady=(0, int(24 * s)))

        enter_btn = tk.Button(
            self.start_screen, text="Get Started", font=self.font_start_btn,
            bg=ACCENT_GREEN, fg="white", activebackground="#3d8a63",
            activeforeground="white", relief="flat", width=16, pady=int(10 * s),
            bd=0, highlightthickness=0, cursor="hand2",
            command=self._show_dashboard
        )
        enter_btn.grid(row=3, column=0)

    def _show_dashboard(self):
        self.dashboard_screen.tkraise()

    # -- Main dashboard ----------------------------------------------------
    def _build_layout(self):
        # ---------------- Top status bar ----------------
        self.status_bar = tk.Frame(self.dashboard_screen, bg=BG_CARD)
        self.status_bar.pack(fill="x", side="top")

        # Exit button - packed first on the right so it sits in the
        # very top-right corner of the screen.
        self.exit_btn = tk.Button(
            self.status_bar, text="✕", font=self.font_med,
            bg=BG_CARD, fg=TEXT_DIM, activebackground=ACCENT_RED,
            activeforeground="white", relief="flat", bd=0, highlightthickness=0,
            padx=16, pady=2, cursor="hand2", command=self._exit_app
        )
        self.exit_btn.pack(side="right", padx=(0, 2))

        self.time_label = tk.Label(
            self.status_bar, text="", font=self.font_small, bg=BG_CARD, fg=TEXT_DIM
        )
        self.time_label.pack(side="right", padx=(10, 4), pady=7)

        # Heartbeat dot: blinks every second. If it stops blinking, the
        # dashboard itself has frozen.
        self.heartbeat_label = tk.Label(
            self.status_bar, text="●", font=self.font_small, bg=BG_CARD, fg=TEXT_DIM
        )
        self.heartbeat_label.pack(side="right", padx=(0, 4), pady=7)

        self.online_label = tk.Label(
            self.status_bar, text="● System OFFLINE", font=self.font_small,
            bg=BG_CARD, fg=ACCENT_RED
        )
        self.online_label.pack(side="left", padx=10, pady=7)

        title = tk.Label(
            self.status_bar, text="PoultryCare", font=self.font_med,
            bg=BG_CARD, fg=TEXT_MAIN
        )
        title.pack(side="left", padx=5, pady=4)

        # ---------------- Two columns ----------------
        content = tk.Frame(self.dashboard_screen, bg=BG_DARK)
        content.pack(fill="both", expand=True, padx=PAD_X, pady=6)
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1, uniform="cols")
        content.grid_columnconfigure(1, weight=1, uniform="cols")

        left = tk.Frame(content, bg=BG_DARK)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, COL_GAP // 2))

        right = tk.Frame(content, bg=BG_DARK)
        right.grid(row=0, column=1, sticky="nsew", padx=(COL_GAP // 2, 0))

        # ---- Left column: sensors, actuators, next schedule ----
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(0, weight=5)
        left.grid_rowconfigure(1, weight=2)
        left.grid_rowconfigure(2, weight=1)

        sensor_grid = tk.Frame(left, bg=BG_DARK)
        sensor_grid.grid(row=0, column=0, sticky="nsew", pady=(0, 5))
        for i in (0, 1):
            sensor_grid.grid_columnconfigure(i, weight=1, uniform="scol")
            sensor_grid.grid_rowconfigure(i, weight=1, uniform="srow")

        self.temp_card, self.temp_value, self.temp_badge = self._make_reading_card(sensor_grid, "Temperature", "-- °C")
        self.temp_card.grid(row=0, column=0, sticky="nsew", padx=(0, 3), pady=(0, 3))

        self.hum_card, self.hum_value, self.hum_badge = self._make_reading_card(sensor_grid, "Humidity", "-- %")
        self.hum_card.grid(row=0, column=1, sticky="nsew", padx=(3, 0), pady=(0, 3))

        self.feed_card, self.feed_value, self.feed_badge = self._make_reading_card(sensor_grid, "Feed Plate", "-- %")
        self.feed_card.grid(row=1, column=0, sticky="nsew", padx=(0, 3), pady=(3, 0))

        # "Drinker" = the trough the chickens drink from (float sensor 1).
        # It refills automatically, so there is no water button anymore.
        # Shows words (Normal / Low / Refilling) so it uses the smaller "word" font.
        self.water_card, self.water_value, self.water_badge = self._make_reading_card(sensor_grid, "Drinker", "--")
        self.water_value.config(font=self.font_word)
        self.water_card.grid(row=1, column=1, sticky="nsew", padx=(3, 0), pady=(3, 0))

        actuator_section = tk.Frame(left, bg=BG_DARK)
        actuator_section.grid(row=1, column=0, sticky="nsew", pady=(0, 5))
        actuator_section.grid_columnconfigure(0, weight=1)
        actuator_section.grid_rowconfigure(1, weight=1)

        actuator_title = tk.Label(
            actuator_section, text="Environmental Control Status", font=self.font_small,
            bg=BG_DARK, fg=TEXT_DIM, anchor="w"
        )
        actuator_title.grid(row=0, column=0, sticky="ew", pady=(0, 2))

        actuator_row = tk.Frame(actuator_section, bg=BG_DARK)
        actuator_row.grid(row=1, column=0, sticky="nsew")
        actuator_row.grid_rowconfigure(0, weight=1)
        for i in (0, 1):
            actuator_row.grid_columnconfigure(i, weight=1, uniform="act")

        self.fan_card, self.fan_state_label = self._make_actuator_card(actuator_row, "Exhaust Fan")
        self.fan_card.grid(row=0, column=0, sticky="nsew", padx=(0, 3))

        self.lamp_card, self.lamp_state_label = self._make_actuator_card(actuator_row, "Heating Lamp")
        self.lamp_card.grid(row=0, column=1, sticky="nsew", padx=(3, 0))

        self.schedule_card = tk.Frame(left, bg=BG_CARD_ALT)
        self.schedule_card.grid(row=2, column=0, sticky="nsew")

        schedule_label = tk.Label(
            self.schedule_card, text="Next Scheduled Feed", font=self.font_small,
            bg=BG_CARD_ALT, fg=TEXT_DIM, anchor="w"
        )
        self.schedule_value = tk.Label(
            self.schedule_card, text="No schedule set", font=self.font_med,
            bg=BG_CARD_ALT, fg=TEXT_MAIN, anchor="w"
        )
        self._stack(self.schedule_card,
                    [(schedule_label, "ew"), (self.schedule_value, "ew")], padx=12)

        # ---- Right column: manual feed, containers, system check ----
        # Status line is packed to the bottom FIRST so it can never be
        # pushed off-screen by the cards above it.
        self.status_label = tk.Label(
            right, text="", font=self.font_small, bg=BG_DARK, fg=ACCENT_AMBER,
            anchor="w", justify="left", wraplength=370, height=2
        )
        self.status_label.pack(side="bottom", fill="x", pady=(4, 0))

        controls_label = tk.Label(
            right, text="Manual Feed", font=self.font_med,
            bg=BG_DARK, fg=TEXT_MAIN, anchor="w"
        )
        controls_label.pack(fill="x")

        # Manual dispense steps in 10% increments of the LIVE hopper
        # capacity (self.feed_capacity_grams, kept in sync from shared_state.json).
        self.custom_percent = tk.IntVar(value=10)
        self.custom_percent_display = tk.StringVar(value="10 %")

        custom_frame = tk.Frame(right, bg=BG_DARK)
        custom_frame.pack(fill="x", pady=(0, 4))

        custom_title = tk.Label(
            custom_frame, text="Amount to add (% of the feed plate size):", font=self.font_tiny,
            bg=BG_DARK, fg=TEXT_DIM, anchor="w"
        )
        custom_title.pack(fill="x")

        stepper_row = tk.Frame(custom_frame, bg=BG_DARK)
        stepper_row.pack(fill="x", pady=(3, 0))
        stepper_row.grid_columnconfigure(1, weight=1)

        minus_btn = tk.Button(
            stepper_row, text="−", font=self.font_button,
            bg=BG_CARD_ALT, fg="white", activebackground="#3f4854",
            activeforeground="white", relief="flat", bd=0, highlightthickness=0,
            width=4, pady=8,
            command=lambda: self._adjust_custom_percent(-10)
        )
        minus_btn.grid(row=0, column=0, sticky="ns", padx=(0, 6))

        self.custom_percent_label = tk.Label(
            stepper_row, textvariable=self.custom_percent_display, font=self.font_med,
            bg=BG_CARD_ALT, fg=TEXT_MAIN
        )
        self.custom_percent_label.grid(row=0, column=1, sticky="nsew")

        plus_btn = tk.Button(
            stepper_row, text="+", font=self.font_button,
            bg=BG_CARD_ALT, fg="white", activebackground="#3f4854",
            activeforeground="white", relief="flat", bd=0, highlightthickness=0,
            width=4, pady=8,
            command=lambda: self._adjust_custom_percent(10)
        )
        plus_btn.grid(row=0, column=2, sticky="ns", padx=(6, 0))

        self.custom_dispense_btn = tk.Button(
            custom_frame, text="Dispense Feed", font=self.font_button,
            bg=ACCENT_GREEN, fg="white", activebackground="#3d8a63",
            activeforeground="white", relief="flat", bd=0, highlightthickness=0,
            pady=10,
            command=lambda: self._dispense_feed(self.custom_percent.get())
        )
        self.custom_dispense_btn.pack(fill="x", pady=(5, 0))

        manual_note = tk.Label(
            right, text="Water refills by itself.",
            font=self.font_tiny, bg=BG_DARK, fg=TEXT_DIM, anchor="w", justify="left"
        )
        manual_note.pack(fill="x", pady=(0, 2))

        # Two container cards side by side: feed container + water container.
        bins_title = tk.Label(
            right, text="Containers", font=self.font_small,
            bg=BG_DARK, fg=TEXT_DIM, anchor="w"
        )
        bins_title.pack(fill="x")

        bins_row = tk.Frame(right, bg=BG_DARK)
        bins_row.pack(fill="both", expand=True, pady=(2, 4))
        bins_row.grid_rowconfigure(0, weight=1)
        bins_row.grid_columnconfigure(0, weight=1, uniform="bins")
        bins_row.grid_columnconfigure(1, weight=1, uniform="bins")

        self.feed_bin_card = self._make_container_card(bins_row, "Feed Container")
        self.feed_bin_card["frame"].grid(row=0, column=0, sticky="nsew", padx=(0, 3))

        self.water_bin_card = self._make_container_card(bins_row, "Water Container")
        self.water_bin_card["frame"].grid(row=0, column=1, sticky="nsew", padx=(3, 0))

        # System check card: plain-language problems for non-technical users.
        self.health_card = tk.Frame(right, bg=BG_CARD)
        self.health_card.pack(fill="both", expand=True)
        self.health_title = tk.Label(
            self.health_card, text="System Check: --", font=self.font_small,
            bg=BG_CARD, fg=TEXT_DIM, anchor="w"
        )
        self.health_msg = tk.Label(
            self.health_card, text="", font=self.font_tiny, bg=BG_CARD, fg=TEXT_DIM,
            anchor="nw", justify="left", wraplength=350, height=3
        )
        self._stack(self.health_card,
                    [(self.health_title, "ew"), (self.health_msg, "ew")], padx=10)

    def _make_reading_card(self, parent, label_text, initial_value):
        card = tk.Frame(parent, bg=BG_CARD)
        label = tk.Label(card, text=label_text, font=self.font_small, bg=BG_CARD, fg=TEXT_DIM, anchor="w")
        value = tk.Label(card, text=initial_value, font=self.font_big, bg=BG_CARD, fg=TEXT_MAIN, anchor="w")
        badge = tk.Label(card, text="--", font=self.font_badge, bg=BADGE_NORMAL[0], fg=BADGE_NORMAL[1], padx=8, pady=1)
        self._stack(card, [(label, "ew"), (value, "ew"), (badge, "w")], padx=12)
        return card, value, badge

    def _make_actuator_card(self, parent, label_text):
        card = tk.Frame(parent, bg=ACTUATOR_OFF_BG)
        label = tk.Label(card, text=label_text, font=self.font_small, bg=ACTUATOR_OFF_BG, fg=TEXT_DIM, anchor="w")
        state_label = tk.Label(card, text="OFF", font=self.font_med, bg=ACTUATOR_OFF_BG, fg=TEXT_DIM, anchor="w")
        self._stack(card, [(label, "ew"), (state_label, "ew")], padx=12)
        card._name_label = label
        return card, state_label

    def _make_container_card(self, parent, title_text):
        """Small card: title, one big easy word (Full / Normal / Low / Empty) and a hint line."""
        frame = tk.Frame(parent, bg=BG_CARD)
        title = tk.Label(frame, text=title_text, font=self.font_small, bg=BG_CARD, fg=TEXT_DIM, anchor="w")
        value = tk.Label(frame, text="--", font=self.font_med, bg=BG_CARD, fg=TEXT_DIM, anchor="w")
        hint = tk.Label(frame, text=" ", font=self.font_tiny, bg=BG_CARD, fg=TEXT_DIM, anchor="w")
        self._stack(frame, [(title, "ew"), (value, "ew"), (hint, "ew")], padx=10)
        return {"frame": frame, "title": title, "value": value, "hint": hint}

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

    # Container cards show plain words, no percentages.
    #   Full / Normal -> green     Low -> amber (refill soon)     Empty -> red (refill now)
    HINTS = {
        "feed": {"Low": "Refill soon", "Empty": "Refill now"},
        "water": {"Low": "Please refill"},
    }

    def _set_container(self, card, kind, label, sensor_ok=True, dim=False):
        if not sensor_ok:
            bg, text, fg, hint = BG_ALERT, "Sensor error", ACCENT_RED, "Check its cable"
        elif label in (None, "--"):
            bg, text, fg, hint = BG_CARD, "--", TEXT_DIM, " "
        elif label == "Empty":
            bg, text, fg = BG_ALERT, "Empty", ACCENT_RED
            hint = self.HINTS.get(kind, {}).get("Empty", "Refill now")
        elif label == "Low":
            bg, text, fg = BG_WARN, "Low", ACCENT_AMBER
            hint = self.HINTS.get(kind, {}).get("Low", "Refill soon")
        else:
            bg, text, fg, hint = BG_CARD, label, (TEXT_DIM if dim else ACCENT_GREEN), " "
        card["frame"].config(bg=bg)
        card["title"].config(bg=bg)
        card["value"].config(text=text, bg=bg, fg=fg)
        card["hint"].config(text=hint, bg=bg, fg=fg if hint.strip() else TEXT_DIM)

    # -- Fail-safe: component health (plain language) ---------------------
    PART_NAMES = {
        "dht22": "Temp & humidity sensor",
        "load_cell": "Feed scale",
        "feed_bin": "Feed container sensor",
        "feed_servo": "Feed dispenser",
        "water_refill": "Drinker refill",
        "heating_lamp": "Heating lamp",
        "exhaust_fan": "Exhaust fan",
    }
    SENSOR_PARTS = ("dht22", "load_cell", "feed_bin")
    PART_ORDER = ("dht22", "load_cell", "feed_bin", "feed_servo",
                  "water_refill", "heating_lamp", "exhaust_fan")
    REASON_TEXT = {
        "no_output": "no feed came out. Check for a jam.",
        "error": "had an error. Check the servo.",
        "stuck_open": "may be stuck open. Check it now.",
        "no_fill": "not filling up. Check valve and float.",
        "repeating": "keeps going low. Check for a leak.",
        "no_heat": "is ON but not warming up. Check the bulb.",
        "no_airflow": "is ON but air is not improving. Check it.",
    }

    def _problem_text(self, key, reason):
        name = self.PART_NAMES.get(key, key)
        if key in self.SENSOR_PARTS:
            if reason == "stuck":
                return f"{name}: reading is stuck. Please check it."
            return f"{name}: not working. Check its cable."
        return f"{name} {self.REASON_TEXT.get(reason, 'is not working. Please check it.')}"

    def _update_health(self, state, controller_ok):
        if not controller_ok:
            self.health_title.config(text="System Check: ⚠ No controller", fg=ACCENT_RED)
            self.health_msg.config(text="The main controller has stopped. Please restart the device.",
                                   fg=ACCENT_RED)
            self._set_health_bg(BG_ALERT)
            return

        health = state.get("sensor_health")
        health = health if isinstance(health, dict) else {}
        reasons = state.get("sensor_fault_reason") or {}
        problems = [self._problem_text(k, reasons.get(k))
                    for k in self.PART_ORDER
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

        # Feed level (plate): use the percentage the Pi already computed.
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

        # Drinker card (float sensor 1): refilling / low / normal.
        # The water CONTAINER has its own card on the right.
        if is_watering:
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

        # Containers: Full / Normal / Low / Empty (no percentages)
        health = state.get("sensor_health")
        health = health if isinstance(health, dict) else {}
        bin_ok = bool(health.get("feed_bin", True))

        bin_state = state.get("feed_bin_state") or ("low" if state.get("feed_bin_low_active") else "ok")
        feed_label = state.get("feed_bin_label")
        if not feed_label:
            pct = state.get("feed_bin_percent")
            if bin_state == "empty":
                feed_label = "Empty"
            elif bin_state == "low":
                feed_label = "Low"
            elif pct is None:
                feed_label = "--"
            else:
                feed_label = "Full" if pct >= 80 else "Normal"
        self._set_container(self.feed_bin_card, "feed", feed_label, sensor_ok=bin_ok, dim=data_stale)

        water_bin_state = state.get("water_bin_state") or (
            "low" if state.get("container_empty_active") else "ok")
        water_label = state.get("water_bin_label") or ("Low" if water_bin_state == "low" else "Normal")
        if state.get("container_level") is None and not state.get("water_bin_state"):
            water_label = "--"
        self._set_container(self.water_bin_card, "water", water_label, dim=data_stale)

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
            msg, color = "Drinker is not filling - check valve / float", ACCENT_RED
        elif feed_label == "Empty" and bin_ok:
            msg, color = "Feed container is EMPTY - please refill", ACCENT_RED
        elif water_label == "Low":
            msg, color = "Water container is low - please refill", ACCENT_RED
        elif feed_label == "Low" and bin_ok:
            msg, color = "Feed container is low - please refill soon", ACCENT_AMBER
        self.status_label.config(text=msg, fg=color)


if __name__ == "__main__":
    app = PoultryDashboard()
    app.mainloop()