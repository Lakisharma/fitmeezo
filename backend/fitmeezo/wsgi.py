"""
WSGI config for fitmeezo project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/wsgi/
"""

import logging
import os

from django.core.management import call_command
from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fitmeezo.settings')

application = get_wsgi_application()

# Automatic startup migration check
# Ensures all essential database tables (django_session, auth, core)
# are created even if the Render Build Command was not updated in the dashboard.
try:
    call_command('migrate', interactive=False)
except Exception as exc:
    logging.getLogger(__name__).warning("Startup migration check notice: %s", exc)
