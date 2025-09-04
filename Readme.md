# Controlmyspa Pörssäri.fi

This project uses https://porssari.fi for time- and price-based temperature control of [Balboa ControlMySpa](https://github.com/arska/controlmyspa) based Whirlpools. This enables the use of market price electricity ("Pörssisähkö"), allowing the spa to be heated when electricity is cheap. In my case, I heat my pool during the cheapest three hours of the day to 37 °C.

## Usage

Clone this git repo or pull the Docker image:

docker run -p 8080:8080 ghcr.io/arska/controlmyspa-porssari

## Configuration

Configure using environment variables. For local development, you can put them into a ".env" file:

TEMP_LOW=27 # temperature to set during "expensive" hours, when Porssari says "off"

TEMP_HIGH=37 # temperature to set during "cheap" hours, when Porssari says "on"

TEMP_OVERRIDE=0 # override the temperature logic, for example, during vacation

CONTROLMYSPA_USER=user@example.com # your username to log in to https://controlmyspa.com

CONTROLMYSPA_PASS=SuperSecretPassword # your password to log in to https://controlmyspa.com

PORSSARI_MAC=A1B2C3D4E5F6 # MAC address as registered on porssari.fi, for example, the MAC address of your controlmyspa gateway or laptop (needs to be unique on the porssari.fi platform)


On porssari.fi, create a new device of type "PICO W" with the MAC address defined above. The script currently only supports one control channel.

You can then configure the "number of cheapest hours per day" to heat your pool to TEMP_HIGH, then let the pool cool down no lower than TEMP_LOW.

The script provides a web server, by default at http://127.0.0.1:8080/, showing the pool's current and set temperatures, as well as the pool's control instructions.

Please note that if your pool set temperature is not set to TEMP_HIGH or TEMP_LOW when starting the script, the porssari control will start 12 hours delayed due to Manual Override detection (see below).

## Manual override

The script detects if somebody manually sets the pool temperature to a value neither TEMP_HIGH nor TEMP_LOW and disables the pörssäri control for 8 hours. If the application restarts during this time, the 8-hour timeout starts again from the restart.

I use this feature to manually set the pool temperature to 36.5 (which, in my case, is neither TEMP_HIGH=37 nor TEMP_LOW=27) through the ControlMySpa mobile app and temporarily disable the Pörssäri controls for eight hours when I plan pool usage, e.g., when expecting guests.

Pörssäri controls resume when the pool temperature is manually set to either TEMP_HIGH or TEMP_LOW, or automatically when the 8h timeout expires.

## References

Based on https://github.com/Porssari/PicoW-client/tree/main/release
Uses the controlmyspa Python module: https://github.com/arska/controlmyspa
