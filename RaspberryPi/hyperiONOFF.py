# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this program.  If not, see
# <http://www.gnu.org/licenses/>.
#
# https://github.com/greiginsydney/hyperiONOFF
# https://greiginsydney.com/hyperiONOFF

import requests
import json                         # For sending to Hyperion
import RPi.GPIO as GPIO
import time
import subprocess
import threading
import re

# ////////////////////////////////
# /////////// STATICS ////////////
# ////////////////////////////////

HOST        = "localhost"
PORT        = 8090
URL         = f"http://{HOST}:{PORT}/json-rpc" # API endpoint
HEADERS     = {'Content-type': 'application/json', 'Accept': 'text/plain'}
TRIGGER_PIN = 25    # GPIO pin for video signal detection (pull-up, active LOW)
DEBOUNCE_MS = 50    # Debounce time in milliseconds

TOGGLE_PIN  = 24    # GPIO pin for the momentary toggle button (pull-up, active LOW)
                    # Connect a momentary push-button between this pin and GND.
                    # If unused, leave unconnected — the pull-up keeps the pin inert.

# Set USE_CEC = True to enable TV power detection via HDMI CEC.
# The Pi's HDMI output must be connected to a spare HDMI input on the TV.
# Requires cec-utils: sudo apt install cec-utils
#
# Set USE_CEC = False to disable CEC entirely and control LEDs via GPIO pin only.
# Use this if you have no spare HDMI input, CEC is unavailable, or CEC causes
# problems on your setup. In GPIO-only mode the LEDs follow the GPIO pin alone.
USE_CEC = True

# CEC: how long to wait (seconds) for each startup power probe attempt.
CEC_STARTUP_TIMEOUT = 10

# If the CEC startup probe cannot determine the TV's power state after all retries,
# this setting controls the assumed state:
#   "on"  — LEDs will be active at startup (correct if TV is usually on when Pi boots)
#   "off" — LEDs will stay off until the next TV power event (safer/conservative default)
# Any value other than "on" or "off" will be treated as "off" and logged.
CEC_PROBE_FALLBACK = "off"

# How many times to retry the CEC startup probe if the result is 'unknown'.
# Valid range: 0-5. Values outside this range will be clamped to 2 and logged.
CEC_PROBE_RETRIES = 3

# Seconds to wait between CEC startup probe retries.
# Valid range: 1-3. Values outside this range will be clamped to 2 and logged.
CEC_PROBE_RETRY_DELAY = 2

GPIO.setmode(GPIO.BCM) # Set up GPIO mode
GPIO.setup(TRIGGER_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(TOGGLE_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

# JSON payload to enable LEDs
payloadON = {
    "command": "componentstate",
    "componentstate": {
        "component": "LEDDEVICE",
        "state": True
    }
}

# JSON payload to disable LEDs
payloadOFF = {
    "command": "componentstate",
    "componentstate": {
        "component": "LEDDEVICE",
        "state": False
    }
}

# ////////////////////////////////
# /////// VALIDATE STATICS ///////
# ////////////////////////////////

def validate_cec_settings():
    """Validate and sanitise CEC-related constants. Logs a warning and applies
    a safe default for any value that is out of range or of the wrong type."""
    global CEC_PROBE_FALLBACK, CEC_PROBE_RETRIES, CEC_PROBE_RETRY_DELAY

    if not isinstance(CEC_PROBE_FALLBACK, str) or CEC_PROBE_FALLBACK.lower() not in ('on', 'off'):
        print(f"WARNING: CEC_PROBE_FALLBACK '{CEC_PROBE_FALLBACK}' is invalid (must be 'on' or 'off'). Defaulting to 'off'.")
        CEC_PROBE_FALLBACK = 'off'
    else:
        CEC_PROBE_FALLBACK = CEC_PROBE_FALLBACK.lower()

    if not isinstance(CEC_PROBE_RETRIES, int) or not (0 <= CEC_PROBE_RETRIES <= 5):
        print(f"WARNING: CEC_PROBE_RETRIES '{CEC_PROBE_RETRIES}' is invalid (must be an integer 0-5). Defaulting to 2.")
        CEC_PROBE_RETRIES = 2

    if not isinstance(CEC_PROBE_RETRY_DELAY, (int, float)) or not (1 <= CEC_PROBE_RETRY_DELAY <= 3):
        print(f"WARNING: CEC_PROBE_RETRY_DELAY '{CEC_PROBE_RETRY_DELAY}' is invalid (must be 1-3 seconds). Defaulting to 2.")
        CEC_PROBE_RETRY_DELAY = 2

# ////////////////////////////////
# ///////// SHARED STATE /////////
# ////////////////////////////////

# Both GPIO and CEC update these; a lock protects concurrent access.
state_lock      = threading.Lock()
gpio_active     = False   # True when TRIGGER_PIN is LOW (signal present)
tv_on           = False   # True when CEC reports TV is powered on (always True if USE_CEC=False)
toggle_override = False   # True when the toggle button has manually set LED state
leds_currently_on = False # Tracks the current LED state, used by toggle to know what to flip

# ////////////////////////////////
# ////////// FUNCTIONS ///////////
# ////////////////////////////////

def send_to_hyperion(TurnON, retries=5, delay=5):
    """Send the appropriate payload to Hyperion, with retries."""
    global leds_currently_on
    for attempt in range(1, retries + 1):
        try:
            if TurnON:
                response = requests.post(URL, json=payloadON, headers=HEADERS, timeout=5)
            else:
                response = requests.post(URL, json=payloadOFF, headers=HEADERS, timeout=5)
            response.raise_for_status()
            result = response.json()
            print("Response:", json.dumps(result, indent=2))
            with state_lock:
                leds_currently_on = TurnON
            return True
        except requests.exceptions.RequestException as e:
            print(f"Attempt {attempt}/{retries} - Error communicating with Hyperion: {e}")
            if attempt < retries:
                print(f"Retrying in {delay} seconds...")
                time.sleep(delay)
    print("Failed to communicate with Hyperion after all retries")
    return False


def should_leds_be_on():
    """Return True if LEDs should be on.
    Toggle override takes priority over all other inputs.
    In CEC mode: both GPIO must be active AND TV must be on.
    In GPIO-only mode: GPIO alone controls the LEDs (tv_on is always True).
    """
    with state_lock:
        if toggle_override:
            return leds_currently_on
        return gpio_active and tv_on


def update_leds(reason=""):
    """Check combined state and send the appropriate command to Hyperion."""
    leds_on = should_leds_be_on()
    print(f"update_leds() called ({reason}): gpio_active={gpio_active}, tv_on={tv_on}, toggle_override={toggle_override} → LEDs {'ON' if leds_on else 'OFF'}")
    send_to_hyperion(TurnON=leds_on)


def pin_changed(channel):
    """Callback fired by GPIO edge detection when TRIGGER_PIN state changes.
    Clears any active toggle override so normal logic resumes."""
    global gpio_active, toggle_override
    pin_state = GPIO.input(TRIGGER_PIN)
    with state_lock:
        gpio_active = (pin_state == GPIO.LOW)
        if toggle_override:
            print("TRIGGER_PIN changed — clearing toggle override.")
            toggle_override = False
    if pin_state == GPIO.LOW:
        print("Pin LOW - signal present")
    else:
        print("Pin HIGH - no signal")
    update_leds(reason="GPIO change")


def toggle_pressed(channel):
    """Callback fired when the toggle button is pressed (TOGGLE_PIN falls LOW).
    Flips the current LED state regardless of GPIO signal or CEC TV state,
    and sets toggle_override so normal logic is suspended until the next
    real input event."""
    global toggle_override, leds_currently_on
    with state_lock:
        new_state = not leds_currently_on
        leds_currently_on = new_state
        toggle_override = True
    print(f"Toggle button pressed → LEDs {'ON' if new_state else 'OFF'} (override active)")
    send_to_hyperion(TurnON=new_state)


# ////////////////////////////////
# ///////// CEC MONITOR //////////
# ////////////////////////////////

class CecMonitor:
    """
    Runs cec-client in the background and watches its output for TV power state changes.

    cec-client is run in 'monitoring' mode (-m) so it doesn't announce itself as an
    active source on the CEC bus — it just listens passively.

    TV power-off is detected via:
      - 0x36  Standby — TV broadcasting that it is going to standby

    TV power-on is detected via any of the following (different TVs use different signals):
      - 0x04  Image View On — standard CEC wake opcode (some TVs e.g. LG, Philips)
      - 0x0D  Text View On  — standard CEC wake opcode, text mode (some TVs)
      - 0x87  Give Device Vendor ID — TV polls devices on wake (e.g. Samsung)
      - 0x80  Report Physical Address — TV re-announces itself on wake (e.g. Sony)
      - 0x90  Report Power Status — explicit power status frame (some TVs)

    We also watch for the "power status changed" lines that libCEC emits as plain text,
    though at -d 8 log level these may not appear on all systems.

    Note: 0x80 Report Physical Address is used cautiously — we only treat it as a
    power-on signal if it arrives as a broadcast (source address 0f) and tv_on is
    currently False, to avoid false triggers from other devices announcing themselves.
    """

    # libCEC plain-text power status changes (may appear at higher log levels)
    # Examples:
    #   "power status changed from 'standby' to 'on'"
    #   "power status changed from 'on' to 'standby'"
    POWER_STATUS_RE = re.compile(
        r"power status changed from '(\w[\w ]+)' to '(\w[\w ]+)'", re.IGNORECASE
    )

    # 0x90 Report Power Status — explicit power state frame (some TV brands)
    # Parameter: 0x00 = on, 0x01 = standby, 0x02 = transitioning to on, 0x03 = transitioning to standby
    REPORT_POWER_RE = re.compile(r">> [\da-fA-F]{2}:[\da-fA-F]{2}:90:([\da-fA-F]{2})", re.IGNORECASE)

    # 0x36 Standby — match the opcode only at end-of-frame or followed by whitespace,
    # to avoid false matches against :36: appearing mid-frame in vendor payloads.
    STANDBY_RE = re.compile(r">> [\da-fA-F]{2}:36\s*$", re.IGNORECASE)

    # 0x04 Image View On / 0x0D Text View On — standard CEC wake opcodes.
    # Match only when the opcode is the last byte in the frame.
    VIEW_ON_RE = re.compile(r">> [\da-fA-F]{2}:0[4dD]\s*$", re.IGNORECASE)

    # 0x87 Give Device Vendor ID — TV polls all devices immediately on wake (e.g. Samsung).
    # Format: >> XX:87:VV:VV:VV
    VENDOR_ID_RE = re.compile(r">> [\da-fA-F]{2}:87:", re.IGNORECASE)

    # 0x80 Report Physical Address — TV re-announces itself on wake (e.g. Sony).
    # Only match broadcasts (source 0f) to avoid triggering on other devices announcing.
    # Format: >> 0f:80:AA:BB:CC:DD
    PHYSICAL_ADDR_RE = re.compile(r">> 0f:80:", re.IGNORECASE)

    # Startup power probe: match 'power status: on' or 'power status: standby'
    # from the output of 'cec-client -s' with 'pow 0' command.
    PROBE_ON_RE      = re.compile(r"power status:\s*on", re.IGNORECASE)
    PROBE_STANDBY_RE = re.compile(r"power status:\s*standby", re.IGNORECASE)

    POWER_ON_STATES  = {'on', 'in transition standby to on'}
    POWER_OFF_STATES = {'standby', 'in transition on to standby'}

    def __init__(self):
        self._proc   = None
        self._thread = None
        self._stop   = threading.Event()

    def probe_power_status(self):
        """
        Query the TV's current power status at startup using cec-client in single-command
        mode (-s). This runs cec-client as a normal (non-monitor) client so it announces
        itself on the CEC bus and the TV responds to the 'pow 0' query.

        Retries up to CEC_PROBE_RETRIES times with CEC_PROBE_RETRY_DELAY seconds between
        attempts if the result is unknown.

        Returns True if TV is on, False if standby, or None if all attempts fail.
        """
        attempts = CEC_PROBE_RETRIES + 1   # total attempts = 1 initial + retries
        for attempt in range(1, attempts + 1):
            suffix = f" (attempt {attempt}/{attempts})" if attempts > 1 else ""
            print(f"CEC startup probe: querying TV power status{suffix}...")
            try:
                result = subprocess.run(
                    ['cec-client', '-s', '-d', '8'],
                    input='pow 0\n',
                    capture_output=True,
                    text=True,
                    timeout=CEC_STARTUP_TIMEOUT
                )
                output = result.stdout + result.stderr
                if self.PROBE_ON_RE.search(output):
                    print("CEC startup probe: TV is ON")
                    return True
                elif self.PROBE_STANDBY_RE.search(output):
                    print("CEC startup probe: TV is in standby")
                    return False
                else:
                    print("CEC startup probe: no power status response received")
                    if attempt < attempts:
                        print(f"CEC startup probe: retrying in {CEC_PROBE_RETRY_DELAY}s...")
                        time.sleep(CEC_PROBE_RETRY_DELAY)
            except FileNotFoundError:
                print("WARNING: cec-client not found. Install with: sudo apt install cec-utils")
                return None
            except subprocess.TimeoutExpired:
                print("CEC startup probe: timed out waiting for TV response")
                if attempt < attempts:
                    print(f"CEC startup probe: retrying in {CEC_PROBE_RETRY_DELAY}s...")
                    time.sleep(CEC_PROBE_RETRY_DELAY)
            except Exception as e:
                print(f"CEC startup probe failed: {e}")
                return None

        print(f"CEC startup probe: all {attempts} attempt(s) inconclusive.")
        return None

    def start(self):
        """Launch cec-client in monitor mode and start the reader thread."""
        try:
            self._proc = subprocess.Popen(
                ['cec-client', '-m', '-d', '8'],   # -m = monitor mode, -d 8 = log level notice
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1                           # line-buffered
            )
            self._thread = threading.Thread(target=self._reader, daemon=True, name="cec-reader")
            self._thread.start()
            print("CEC monitor started (cec-client pid:", self._proc.pid, ")")
        except FileNotFoundError:
            print("WARNING: cec-client not found. Install with: sudo apt install cec-utils")
            print("         Falling back to GPIO-only mode — tv_on assumed True.")
            self._set_tv_on(True)

    def stop(self):
        """Shut down cec-client cleanly."""
        self._stop.set()
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        print("CEC monitor stopped.")

    def _reader(self):
        """Background thread: read cec-client stdout line by line."""
        for line in self._proc.stdout:
            if self._stop.is_set():
                break
            line = line.rstrip()
            if not line:
                continue
            self._parse_line(line)

    def _parse_line(self, line):
        """Parse a single line of cec-client output for power state changes."""

        # libCEC plain-text power status changes (appear at higher log levels)
        m = self.POWER_STATUS_RE.search(line)
        if m:
            new_state = m.group(2).lower()
            print(f"CEC power status: {m.group(1)} → {m.group(2)}")
            if new_state in self.POWER_ON_STATES:
                self._set_tv_on(True)
            elif new_state in self.POWER_OFF_STATES:
                self._set_tv_on(False)
            return

        # 0x90 Report Power Status — explicit power state from TV (some brands)
        # Parameter: 0x00 = on, 0x01 = standby, 0x02 = transitioning to on, 0x03 = transitioning to standby
        m = self.REPORT_POWER_RE.search(line)
        if m:
            param = int(m.group(1), 16)
            powered = param in (0x00, 0x02)
            print(f"CEC 0x90 Report Power Status: param=0x{param:02X} → TV {'ON' if powered else 'OFF'}")
            self._set_tv_on(powered)
            return

        # 0x36 Standby — TV going to standby. Regex avoids matching :36: mid-frame
        # in Samsung vendor payloads.
        if self.STANDBY_RE.search(line):
            print("CEC 0x36 Standby received → TV OFF")
            self._set_tv_on(False)
            return

        # 0x04 Image View On / 0x0D Text View On — standard CEC wake opcodes.
        # Used by some TVs (LG, Philips etc) on power-on.
        if self.VIEW_ON_RE.search(line):
            print("CEC 0x04/0x0D View On received → TV ON")
            self._set_tv_on(True)
            return

        # 0x87 Give Device Vendor ID — TV polls all devices immediately on wake.
        # Observed on Samsung TVs.
        if self.VENDOR_ID_RE.search(line):
            print("CEC 0x87 Give Vendor ID received → TV ON")
            self._set_tv_on(True)
            return

        # 0x80 Report Physical Address — TV re-announces itself on wake.
        # Observed on Sony TVs. Only treat as power-on if currently off, and only
        # for broadcasts (source 0f), to avoid false triggers from other devices.
        if self.PHYSICAL_ADDR_RE.search(line):
            with state_lock:
                currently_on = tv_on
            if not currently_on:
                print("CEC 0x80 Physical Address broadcast received → TV ON")
                self._set_tv_on(True)
            return

    def _set_tv_on(self, powered: bool):
        """Update shared tv_on state and trigger LED update if it changed.
        Clears any active toggle override so normal CEC logic resumes."""
        global tv_on, toggle_override
        with state_lock:
            changed = (tv_on != powered)
            tv_on   = powered
            if changed and toggle_override:
                print("CEC TV power change — clearing toggle override.")
                toggle_override = False
        if changed:
            update_leds(reason="CEC TV power change")


# ////////////////////////////////
# /////////// MAIN ///////////////
# ////////////////////////////////

# Validate CEC settings before use (logs warnings and applies safe defaults)
if USE_CEC:
    validate_cec_settings()

cec = CecMonitor() if USE_CEC else None

try:
    if USE_CEC:
        # Phase 1: probe the TV's current power state using cec-client -s.
        # This runs before the monitor starts so we know tv_on at startup
        # without waiting for the TV to spontaneously broadcast.
        probe_result = cec.probe_power_status()
        with state_lock:
            if probe_result is True:
                tv_on = True
            elif probe_result is False:
                tv_on = False
            else:
                # All probe attempts inconclusive — apply the configured fallback
                fallback_on = (CEC_PROBE_FALLBACK == 'on')
                tv_on = fallback_on
                print(f"CEC startup probe inconclusive — applying CEC_PROBE_FALLBACK: TV assumed {'ON' if fallback_on else 'OFF'}.")

        # Phase 2: start the background monitor for ongoing event detection
        cec.start()
    else:
        # GPIO-only mode: tv_on is permanently True so GPIO alone controls LEDs
        with state_lock:
            tv_on = True
        print("CEC disabled (USE_CEC=False) — running in GPIO-only mode.")

    # Register TRIGGER_PIN edge detection (fires on both rising and falling edges)
    GPIO.add_event_detect(TRIGGER_PIN, GPIO.BOTH, callback=pin_changed, bouncetime=DEBOUNCE_MS)

    # Register TOGGLE_PIN edge detection (fires on falling edge only — button press)
    GPIO.add_event_detect(TOGGLE_PIN, GPIO.FALLING, callback=toggle_pressed, bouncetime=DEBOUNCE_MS)

    # Read and act on the initial pin state at startup, so we don't have to
    # wait for the first edge transition before sending the correct payload
    initial_state = GPIO.input(TRIGGER_PIN)
    with state_lock:
        gpio_active = (initial_state == GPIO.LOW)
    print("Startup pin state:", "LOW (active)" if gpio_active else "HIGH (inactive)")
    print(f"Startup TV state:  {'ON' if tv_on else 'OFF'}")

    update_leds(reason="startup")

    mode_str = "GPIO pin " + str(TRIGGER_PIN) + " and CEC bus" if USE_CEC else "GPIO pin " + str(TRIGGER_PIN) + " only"
    mode_str += f", toggle button on pin {TOGGLE_PIN}"
    print(f"Monitoring {mode_str} — press Ctrl+C to exit")

    while True:
        time.sleep(1)

except KeyboardInterrupt:
    print("Exiting...")
finally:
    if cec:
        cec.stop()
    GPIO.cleanup()
    print("GPIO cleaned up")
