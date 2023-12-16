# Controlmyspa Pörssäri.fi

This project uses https://porssari.fi for time- and price-based temperature control of https://github.com/arska/controlmyspa[Balboa ControlMySpa] based Whirlpools.

## Usage

Clone this git repo or pull the docker image:

docker run -p 8080:8080 ghcr.io/arska/controlmyspa-porssari

## Configuration

Configure using environment variables, for local development you can put them into an ".env" file:

TEMP_LOW=30 # temperature to set during "expensive" hours, when porssari says "off"

TEMP_HIGH=36 # temperature to set during "cheap" hours, when porssari says "on"

TEMP_OVERRIDE=0 # override the temperature logic, for example, during vacation

CONTROLMYSPA_USER=user@example.com # your username to log in to https://controlmyspa.com

CONTROLMYSPA_PASS=SuperSecretPassword # your password to log in to https://controlmyspa.com

PORSSARI_MAC=A1B2C3D4E5F6 # MAC address as registered on porssari.fi, for example, the MAC address of your controlmyspa gateway or laptop (needs to be unique on the porssari.fi platform)

On porssari.fi, create a new device as type "PICO W" with the MAC address defined above. The script currently only supports one control channel.

You can then configure the "number of cheapest hours per day" to heat your pool to TEMP_HIGH, then let the pool cool down no lower than TEMP_LOW.

The script provides a web server, by default at http://127.0.0.1:8080/, showing the porssari control instructions and current and set temperatures of the pool.
