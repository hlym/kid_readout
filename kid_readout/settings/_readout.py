"""
This is template local settings file for the computer `readout` running the STAR Cryo cryostat in 1132.
"""

CRYOSTAT = 'STARCryo'

TEMPERATURE_LOG_DIR = '/data/readout/SRS'

# The ROACH1 is connected in baseband mode.
from kid_readout.settings._roach import ROACH1_IP, ROACH1_VALON, ROACH1_HOST_IP