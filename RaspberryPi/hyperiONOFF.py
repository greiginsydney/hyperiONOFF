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

trigger_pin = 17 # Define pin number

GPIO.setmode(GPIO.BCM) # Set up GPIO mode
GPIO.setup(trigger_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

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


try:
    if TurnON:
      response = requests.post(url, json=payloadON, timeout=5)
    else:
      response = requests.post(url, json=payloadOFF, timeout=5)
    response.raise_for_status()
    result = response.json()
    print("Response:", json.dumps(result, indent=2))
except requests.exceptions.RequestException as e:
    print("Error communicating with Hyperion:", e)
