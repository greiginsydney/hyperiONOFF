# hyperiONOFF
A python script that reads a GPIO pin and/or CEC HDMI signals to turn your Hyperion LEDs on or off.

## Truth table

| GPIO | USE_CEC | TV is on (CEC result) | LEDs |
|---|---|---|---|
| LOW | False | N/A (always True) | **ON** |
| HIGH | False | N/A (always True) | OFF |
| LOW | True | True | **ON** |
| LOW | True | False | OFF |
| HIGH | True | True | OFF |
| HIGH | True | False | OFF |

# References
- [https:///greiginsydney.com/hyperiONOFF](https:///greiginsydney.com/hyperiONOFF)


# Credits 
- Claude.ai provided some guidance in the development of this project, and wrote most of the python script.

\- Greig.
