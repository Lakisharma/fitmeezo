#!/usr/bin/env bash
# exit on error
set -o errexit

# Install dependencies
pip install -r requirements.txt

# Collect static files
python backend/manage.py collectstatic --no-input

# Apply database migrations safely
python backend/manage.py migrate --no-input

# Load demo data only if explicitly requested via environment variable LOAD_DEMO_DATA=true
if [ "${LOAD_DEMO_DATA:-false}" = "true" ]; then
    python backend/manage.py load_demo_data
fi

# Ensure an active administrator/superuser exists for Management Console / Admin access
python backend/manage.py ensure_admin
