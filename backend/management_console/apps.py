"""
Management Console App - Fitmeezo Independent Third Admin Panel.
"""

from django.apps import AppConfig


class ManagementConsoleConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "management_console"
    verbose_name = "Fitmeezo Management Console"
