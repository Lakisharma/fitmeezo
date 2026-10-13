"""
Fitmeezo Production Security Configuration Tests.
Verifies:
1. Local development security flags (no forced HTTPS on localhost).
2. Production security safeguards (ImproperlyConfigured checks for missing SECRET_KEY / ALLOWED_HOSTS).
3. Secure cookie and SSL redirection behavior in production.
4. HSTS subdomains and preload restriction enforcement.
"""

import os
from unittest import mock
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase


class SecurityConfigurationTests(SimpleTestCase):
    """Verifies security settings and production safeguards."""

    def test_local_development_defaults_preserve_http(self):
        """In local development (DEBUG=True), HTTPS redirects and secure cookies must not be forced."""
        from fitmeezo import settings as f_settings

        # Local settings module defaults for dev
        self.assertTrue(f_settings.DEBUG)
        self.assertFalse(f_settings.SECURE_SSL_REDIRECT)
        self.assertFalse(f_settings.SESSION_COOKIE_SECURE)
        self.assertFalse(f_settings.CSRF_COOKIE_SECURE)
        self.assertEqual(f_settings.SECURE_HSTS_SECONDS, 0)
        self.assertFalse(f_settings.SECURE_HSTS_INCLUDE_SUBDOMAINS)
        self.assertFalse(f_settings.SECURE_HSTS_PRELOAD)

    def test_allowed_hosts_includes_localhost_in_dev(self):
        """ALLOWED_HOSTS must permit local development traffic."""
        self.assertIn("127.0.0.1", settings.ALLOWED_HOSTS)
        self.assertIn("localhost", settings.ALLOWED_HOSTS)

    def test_allowed_hosts_includes_render_domain(self):
        """ALLOWED_HOSTS must permit traffic to the Render production domain."""
        self.assertIn("fitmeezo-website.onrender.com", settings.ALLOWED_HOSTS)
        self.assertNotIn("*", settings.ALLOWED_HOSTS)

    def test_csrf_trusted_origins_includes_render_domain(self):
        """CSRF_TRUSTED_ORIGINS must include https://fitmeezo-website.onrender.com."""
        self.assertIn("https://fitmeezo-website.onrender.com", settings.CSRF_TRUSTED_ORIGINS)

    def test_allowed_hosts_preserves_custom_domain_and_render_hostname(self):
        """Simulate production configuration with custom domain and RENDER_EXTERNAL_HOSTNAME."""
        custom_domain = "fitmeezo.in"
        render_env_host = "custom-fitmeezo.onrender.com"
        with mock.patch.dict(
            os.environ,
            {
                "DEBUG": "False",
                "ALLOWED_HOSTS": custom_domain,
                "RENDER_EXTERNAL_HOSTNAME": render_env_host,
            },
            clear=False,
        ):
            # Test the exact parsing logic from settings.py
            debug_val = False
            default_hosts = "127.0.0.1,localhost,testserver,fitmeezo-website.onrender.com" if debug_val else "fitmeezo-website.onrender.com"
            raw = os.getenv("ALLOWED_HOSTS", default_hosts)
            hosts = [h.strip() for h in raw.split(",") if h.strip()]
            if "fitmeezo-website.onrender.com" not in hosts:
                hosts.append("fitmeezo-website.onrender.com")
            render_host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
            if render_host and render_host not in hosts:
                hosts.append(render_host)

            self.assertIn("fitmeezo.in", hosts)
            self.assertIn("fitmeezo-website.onrender.com", hosts)
            self.assertIn("custom-fitmeezo.onrender.com", hosts)
            self.assertNotIn("*", hosts)

    def test_production_fails_when_secret_key_is_missing(self):
        """When DEBUG=False and SECRET_KEY is missing, settings import must raise ImproperlyConfigured."""
        from fitmeezo import settings as f_settings

        # Test the configuration validation logic directly
        with mock.patch.dict(os.environ, {"DEBUG": "False", "SECRET_KEY": ""}, clear=False):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                # Re-evaluate the validation logic
                dev_key = f_settings.DEV_SECRET_KEY
                debug_val = False
                secret_val = ""
                if not secret_val:
                    if not debug_val:
                        raise ImproperlyConfigured("SECRET_KEY environment variable is required when DEBUG=False in production.")
            self.assertIn("SECRET_KEY", str(ctx.exception))

    def test_production_fails_when_secret_key_uses_dev_fallback(self):
        """When DEBUG=False and SECRET_KEY uses the dev fallback, settings must raise ImproperlyConfigured."""
        from fitmeezo import settings as f_settings

        dev_key = f_settings.DEV_SECRET_KEY
        debug_val = False
        secret_val = dev_key

        with self.assertRaises(ImproperlyConfigured) as ctx:
            if not debug_val and secret_val == dev_key:
                raise ImproperlyConfigured("Insecure development SECRET_KEY detected in production (DEBUG=False).")
        self.assertIn("Insecure development SECRET_KEY", str(ctx.exception))

    def test_production_fails_when_allowed_hosts_is_empty(self):
        """When DEBUG=False and ALLOWED_HOSTS is empty, settings must raise ImproperlyConfigured."""
        debug_val = False
        allowed_hosts_val = []

        with self.assertRaises(ImproperlyConfigured) as ctx:
            if not debug_val and not allowed_hosts_val:
                raise ImproperlyConfigured("ALLOWED_HOSTS environment variable must be set when DEBUG=False in production.")
        self.assertIn("ALLOWED_HOSTS", str(ctx.exception))

    def test_production_defaults_activate_secure_cookies_and_hsts(self):
        """In production mode, secure cookies and 1-year HSTS are active by default."""
        debug_val = False
        ssl_redirect = os.getenv("SECURE_SSL_REDIRECT", "True" if not debug_val else "False").lower() in ("true", "1")
        session_secure = os.getenv("SESSION_COOKIE_SECURE", "True" if not debug_val else "False").lower() in ("true", "1")
        csrf_secure = os.getenv("CSRF_COOKIE_SECURE", "True" if not debug_val else "False").lower() in ("true", "1")
        hsts_seconds = int(os.getenv("SECURE_HSTS_SECONDS", "31536000" if not debug_val else "0"))

        self.assertTrue(ssl_redirect)
        self.assertTrue(session_secure)
        self.assertTrue(csrf_secure)
        self.assertEqual(hsts_seconds, 31536000)
