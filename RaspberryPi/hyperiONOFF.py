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

# ////////////////////////////////
# /////////// STATICS ////////////
# ////////////////////////////////

HOST     = "localhost"
PORT     = 8090
URL      = f"http://{HOST}:{PORT}/json-rpc" # API endpoint
HEADERS  = {'Content-type': 'application/json', 'Accept': 'text/plain'}

TRIGGER_PIN  = 25    # Define pin number
DEBOUNCE_MS  = 50    # Debounce time in milliseconds

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
# ////////// FUNCTIONS ///////////
# ////////////////////////////////

def send_to_hyperion(TurnON):
    """Send the appropriate payload to Hyperion based on pin state."""
    try:
        if TurnON:
            response = requests.post(URL, json=payloadON, headers=HEADERS, timeout=5)
        else:
            response = requests.post(URL, json=payloadOFF, headers=HEADERS, timeout=5)
        response.raise_for_status()
        result = response.json()
        print("Response:", json.dumps(result, indent=2))
    except requests.exceptions.RequestException as e:
        print("Error communicating with Hyperion:", e)


def pin_changed(channel):
    """Callback fired by GPIO edge detection when the pin state changes."""
    pin_state = GPIO.input(TRIGGER_PIN)
    if pin_state == GPIO.LOW:
        print("Pin LOW - sending ON")
        send_to_hyperion(TurnON=True)
    else:
        print("Pin HIGH - sending OFF")
        send_to_hyperion(TurnON=False)


# ////////////////////////////////
# /////////// MAIN ///////////////
# ////////////////////////////////

try:
    # Register edge detection callback (fires on both rising and falling edges)
    GPIO.add_event_detect(TRIGGER_PIN, GPIO.BOTH, callback=pin_changed, bouncetime=DEBOUNCE_MS)

    # Read and act on the initial pin state at startup, so we don't have to
    # wait for the first edge transition before sending the correct payload
    initial_state = GPIO.input(TRIGGER_PIN)
    print("Startup pin state:", "LOW" if initial_state == GPIO.LOW else "HIGH")
    send_to_hyperion(TurnON=(initial_state == GPIO.LOW))

    print("Monitoring pin", TRIGGER_PIN, "- press Ctrl+C to exit")

    # Sleep indefinitely - the GPIO callback handles everything asynchronously
    while True:
        time.sleep(1)

except KeyboardInterrupt:
    print("Exiting...")

finally:
    GPIO.cleanup()
    print("GPIO cleaned up")
