"""Download the iot.controlmyspa.com intermediate TLS certificate.

Downloaded during container build time.
"""

import controlmyspa
import requests

try:
    api = controlmyspa.ControlMySpa("dummy", "dummy")
except requests.exceptions.SSLError:
    # the certificate will be downloaded and added to the certifi trust store
    # but it will only be available on the next invocation
    pass
except requests.exceptions.HTTPError:
    # catch the 401 Unauthorized error due to dummy credentials
    pass
