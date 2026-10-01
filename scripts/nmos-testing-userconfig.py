# Mounted into the AMWA nmos-testing image at /config/UserConfig.py
# (the image symlinks that path to nmostesting/UserConfig.py).
from . import Config as CONFIG

# FlowXer is a static-registry node (FLOWXER_NMOS_DNS_SD=false). Skip mock
# DNS-SD / registry discovery so IS-04-01 does not sit on browse timeouts.
CONFIG.ENABLE_DNS_SD = False
CONFIG.ENABLE_HTTPS = False
CONFIG.ENABLE_AUTH = False
CONFIG.MAX_TEST_ITERATIONS = 8
CONFIG.MOCK_SERVICES_WARM_UP_DELAY = 0
