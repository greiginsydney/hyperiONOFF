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

HOST     = "localhost"
PORT     = 8090
URL      = f"http://{HOST}:{PORT}/json-rpc" # API endpoint
HEADERS  = {'Content-type': 'application/json', 'Accept': 'text/plain'}
TRIGGER_PIN  = 25    # Define pin number
DEBOUNCE_MS  = 50    # Debounce time in milliseconds

# CEC: how long to wait (seconds) before assuming TV is off if no CEC traffic seen at startup
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
state_lock    = threading.Lock()
gpio_active   = False   # True when GPIO pin is LOW (signal present)
tv_on         = False   # True when CEC reports TV is powered on

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
    """Return True only if both the GPIO signal is active AND the TV is on."""
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

    Opcodes observed on this Samsung TV:
      - 0x36  Standby        — TV broadcasting that it is going to standby (TV OFF)
      - 0x87  Give Vendor ID — TV polling devices on wake-up (TV ON)
      - 0x90  Report Power Status — standard power status frame (kept as fallback)

    Note: this TV does not send 0x04 (Image View On) or 0x0D (Text View On) on wake.
    Power-on is inferred from the 0x87 vendor ID poll that the TV broadcasts immediately
    after waking, before any other traffic appears.

    We also watch for the "power status changed" lines that libCEC emits as plain text,
    though at -d 8 log level these may not appear.
    """

    # libCEC plain-text power status changes (may appear at higher log levels)
    # Examples:
    #   "power status changed from 'standby' to 'on'"
    #   "power status changed from 'on' to 'standby'"
    POWER_STATUS_RE = re.compile(
        r"power status changed from '(\w[\w ]+)' to '(\w[\w ]+)'", re.IGNORECASE
    )

    # CEC Report Power Status opcode (0x90) — kept as a fallback for other TV brands.
    # Parameter: 0x00 = on, 0x01 = standby, 0x02 = transitioning to on, 0x03 = transitioning to standby
    REPORT_POWER_RE = re.compile(r">> ([\da-fA-F]{2}):([\da-fA-F]{2}):90:([\da-fA-F]{2})")

    # 0x36 Standby — match the opcode only at end-of-frame or followed by a space/newline,
    # to avoid false matches against :36: appearing mid-frame in vendor payloads.
    STANDBY_RE = re.compile(r">> [\da-fA-F]{2}:36\s*$", re.IGNORECASE)

    # 0x87 Give Device Vendor ID — TV broadcasts this to all devices immediately on wake.
    # Format: >> 0f:87:VV:VV:VV  (VV = vendor ID bytes, e.g. 08:00:46 for Samsung)
    VENDOR_ID_RE = re.compile(r">> [\da-fA-F]{2}:87:", re.IGNORECASE)

    POWER_ON_STATES  = {'on', 'in transition standby to on'}
    POWER_OFF_STATES = {'standby', 'in transition on to standby'}

    def __init__(self):
        self._proc    = None
        self._thread  = None
        self._stop    = threading.Event()

    def start(self):
        """Launch cec-client and start the reader thread."""
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
            print("         TV power state will be assumed ON — GPIO alone controls LEDs.")
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

        # Raw CEC 0x90 Report Power Status frames (fallback for other TV brands)
        m = self.REPORT_POWER_RE.search(line)
        if m:
            param = int(m.group(3), 16)
            # 0x00 = on, 0x02 = transitioning to on → treat as on
            powered = param in (0x00, 0x02)
            print(f"CEC 0x90 Report Power Status: param=0x{param:02X} → TV {'ON' if powered else 'OFF'}")
            self._set_tv_on(powered)
            return

        # 0x36 Standby — TV going to standby. Use regex to avoid matching :36:
        # appearing mid-frame in Samsung vendor payloads.
        if self.STANDBY_RE.search(line):
            print("CEC 0x36 Standby received → TV OFF")
            self._set_tv_on(False)
            return

        # 0x87 Give Device Vendor ID — TV broadcasts this immediately on wake.
        # This is the most reliable power-on signal for this Samsung TV.
        if self.VENDOR_ID_RE.search(line):
            print("CEC 0x87 Give Vendor ID received → TV ON")
            self._set_tv_on(True)
            return

    def _set_tv_on(self, powered: bool):
        """Update shared tv_on state and trigger LED update if it changed."""
        global tv_on
        with state_lock:
            changed  = (tv_on != powered)
            tv_on    = powered
        if changed:
            update_leds(reason="CEC TV power change")

    def query_power_status(self):
        """
        Ask the TV to report its power status over CEC. Call once at startup
        so we don't have to wait for the TV to broadcast spontaneously.
        Send the 'pow' command to cec-client's stdin.
        """
        if self._proc and self._proc.stdin:
            try:
                self._proc.stdin.write("pow 0\n")
                self._proc.stdin.flush()
            except Exception as e:
                print(f"Could not send CEC power query: {e}")


# ////////////////////////////////
# /////////// MAIN ///////////////
# ////////////////////////////////

cec = CecMonitor()

try:
    # Start CEC monitoring first so we know TV state before acting on GPIO
    cec.start()

    # Give cec-client a moment to initialise, then ask the TV its power state.
    # This fills in tv_on before we read the GPIO pin, avoiding a false LED-off
    # at startup if the TV is already on.
    time.sleep(2)

    # Register edge detection callback (fires on both rising and falling edges)
    GPIO.add_event_detect(TRIGGER_PIN, GPIO.BOTH, callback=pin_changed, bouncetime=DEBOUNCE_MS)

    # Read and act on the initial pin state at startup
    initial_state = GPIO.input(TRIGGER_PIN)
    with state_lock:
        gpio_active = (initial_state == GPIO.LOW)
    print("Startup pin state:", "LOW (active)" if gpio_active else "HIGH (inactive)")
    print(f"Startup TV state:  {'ON' if tv_on else 'OFF (waiting up to ' + str(CEC_STARTUP_TIMEOUT) + 's for CEC response)'}")

    # Wait briefly for CEC to report the TV's power state, then act regardless
    deadline = time.time() + CEC_STARTUP_TIMEOUT
    while not tv_on and time.time() < deadline:
        time.sleep(0.5)

    if not tv_on:
        print("No CEC power-on response received at startup — assuming TV is off.")

    update_leds(reason="startup")

    print("Monitoring GPIO pin", TRIGGER_PIN, "and CEC bus — press Ctrl+C to exit")
    while True:
        time.sleep(1)

except KeyboardInterrupt:
    print("Exiting...")
finally:
    cec.stop()
    GPIO.cleanup()
    print("GPIO cleaned up")
