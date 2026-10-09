from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient


class HealthCheckEndpointTest(TestCase):
    """
    Test suite for the Fitmeezo system health check endpoint.
    """

    def setUp(self):
        self.client = APIClient()

    def test_health_check_returns_200(self):
        """Test that /health/ returns HTTP 200 OK."""
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_health_check_payload_structure(self):
        """Test that the response contains expected brand, database, and status fields."""
        response = self.client.get("/health/")
        data = response.json()

        self.assertIn("status", data)
        self.assertEqual(data["status"], "ok")

        self.assertIn("app", data)
        self.assertEqual(data["app"], "Fitmeezo")

        self.assertIn("tagline", data)
        self.assertEqual(data["tagline"], "SHOP SMART • LIVE BETTER")

        self.assertIn("currency", data)
        self.assertEqual(data["currency"], "INR")

        self.assertIn("database_engine", data)
        self.assertIn(data["database_engine"], ["sqlite", "postgresql"])

        self.assertIn("environment", data)
        self.assertIn("timestamp", data)

    def test_health_check_via_named_reverse(self):
        """Test resolution via Django url reverse name."""
        url = reverse("health-check")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
