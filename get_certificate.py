"""
download the iot.controlmyspa.com intermediate TLS certificate
during container build time
"""
import controlmyspa
import requests

try:
    api = controlmyspa.ControlMySpa("dummy", "dummy")
except requests.exceptions.HTTPError:
    # catch the 401 Unauthorized error due to dummy credentials
    pass
