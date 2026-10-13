#!/usr/bin/env bash
# exit on error
set -o errexit

# Install dependencies
pip install -r requirements.txt

# Collect static files
python backend/manage.py collectstatic --no-input

# Apply database migrations safely
python backend/manage.py migrate --no-input
