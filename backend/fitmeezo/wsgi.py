"""
WSGI config for fitmeezo project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/wsgi/

NOTE: Database migrations are intentionally NOT run here.
Migrations run once during the Render build step via build.sh:
    python backend/manage.py migrate --no-input
Running migrate inside each Gunicorn worker causes race conditions,
unnecessary database load on every restart, and increased startup latency.
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fitmeezo.settings')

application = get_wsgi_application()
