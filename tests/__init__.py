import logging
import os
import sys

# The package lives in the add-on folder (Home Assistant builds from there only).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "addon"))
logging.getLogger().addHandler(logging.NullHandler())
