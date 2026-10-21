"""Copy these contents to the WSGI configuration file linked in the EU Web tab."""
import os
import sys

# Change YOUR_USERNAME in BOTH paths. No secrets belong in this file.
project = '/home/YOUR_USERNAME/alarmtrend'
sys.path.insert(0, project)
os.environ['ALARMTREND_ENV_FILE'] = '/home/YOUR_USERNAME/.config/alarmtrend.env'

from alarmtrend import create_app

application = create_app()
