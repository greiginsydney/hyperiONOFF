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
TRIGGER_PIN = 25    # Define pin number
DEBOUNCE_MS = 50    # Debounce time in milliseconds

# Set USE_CEC = True to enable TV power detection via HDMI CEC.
# The Pi's HDMI output must be connected to a spare HDMI input on the TV.
# Requires cec-utils: sudo apt install cec-utils
#
# Set USE_CEC = False to disable CEC entirely and control LEDs via GPIO pin only.
# Use this if you have no spare HDMI input, CEC is unavailable, or CEC causes
# problems on your setup. In GPIO-only mode the LEDs follow the GPIO pin alone.
USE_CEC = True

# CEC: how long to wait (seconds) for the startup power probe before assuming TV is off.
CEC_STARTUP_TIMEOUT = 10

GPIO.setmode(GPIO.BCM) # Set up GPIO mode
GPIO.setup(TRIGGER_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

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
# ///////// SHARED STATE /////////
# ////////////////////////////////

# Both GPIO and CEC update these; a lock protects concurrent access.
state_lock  = threading.Lock()
gpio_active = False   # True when GPIO pin is LOW (signal present)
tv_on       = False   # True when CEC reports TV is powered on (always True if USE_CEC=False)

# ////////////////////////////////
# ////////// FUNCTIONS ///////////
# ////////////////////////////////

def send_to_hyperion(TurnON, retries=5, delay=5):
    """Send the appropriate payload to Hyperion, with retries."""
    for attempt in range(1, retries + 1):
        try:
            if TurnON:
                response = requests.post(URL, json=payloadON, headers=HEADERS, timeout=5)
            else:
                response = requests.post(URL, json=payloadOFF, headers=HEADERS, timeout=5)
            response.raise_for_status()
            result = response.json()
            print("Response:", json.dumps(result, indent=2))
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
    In CEC mode: both GPIO must be active AND TV must be on.
    In GPIO-only mode: GPIO alone controls the LEDs (tv_on is always True).
    """
    with state_lock:
        return gpio_active and tv_on


def update_leds(reason=""):
    """Check combined state and send the appropriate command to Hyperion."""
    leds_on = should_leds_be_on()
    print(f"update_leds() called ({reason}): gpio_active={gpio_active}, tv_on={tv_on} → LEDs {'ON' if leds_on else 'OFF'}")
    send_to_hyperion(TurnON=leds_on)


def pin_changed(channel):
    """Callback fired by GPIO edge detection when the pin state changes."""
    global gpio_active
    pin_state = GPIO.input(TRIGGER_PIN)
    with state_lock:
        gpio_active = (pin_state == GPIO.LOW)
    if pin_state == GPIO.LOW:
        print("Pin LOW - signal present")
    else:
        print("Pin HIGH - no signal")
    update_leds(reason="GPIO change")


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

        Returns True if TV is on, False if standby, or None if the query fails or
        the TV does not respond.
        """
        print("CEC startup probe: querying TV power status...")
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
                return None
        except FileNotFoundError:
            print("WARNING: cec-client not found. Install with: sudo apt install cec-utils")
            return None
        except subprocess.TimeoutExpired:
            print("CEC startup probe: timed out waiting for TV response")
            return None
        except Exception as e:
            print(f"CEC startup probe failed: {e}")
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
        """Update shared tv_on state and trigger LED update if it changed."""
        global tv_on
        with state_lock:
            changed = (tv_on != powered)
            tv_on   = powered
        if changed:
            update_leds(reason="CEC TV power change")


# ////////////////////////////////
# /////////// MAIN ///////////////
# ////////////////////////////////

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
                # No response — assume off; monitor will correct this on next TV event
                tv_on = False
                print("CEC startup probe inconclusive — assuming TV is off.")

        # Phase 2: start the background monitor for ongoing event detection
        cec.start()
    else:
        # GPIO-only mode: tv_on is permanently True so GPIO alone controls LEDs
        with state_lock:
            tv_on = True
        print("CEC disabled (USE_CEC=False) — running in GPIO-only mode.")

    # Register edge detection callback (fires on both rising and falling edges)
    GPIO.add_event_detect(TRIGGER_PIN, GPIO.BOTH, callback=pin_changed, bouncetime=DEBOUNCE_MS)

    # Read and act on the initial pin state at startup, so we don't have to
    # wait for the first edge transition before sending the correct payload
    initial_state = GPIO.input(TRIGGER_PIN)
    with state_lock:
        gpio_active = (initial_state == GPIO.LOW)
    print("Startup pin state:", "LOW (active)" if gpio_active else "HIGH (inactive)")
    print(f"Startup TV state:  {'ON' if tv_on else 'OFF'}")

    update_leds(reason="startup")

    mode_str = "GPIO pin " + str(TRIGGER_PIN) + " and CEC bus" if USE_CEC else "GPIO pin " + str(TRIGGER_PIN) + " only"
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
