import os
import unittest
from unittest.mock import patch

from app.config import Settings, settings


class TestSettingsConfiguration(unittest.TestCase):
    """Test suite verifying streamlined settings env var loading, cors_origins parsing, and __getattr__."""

    def test_default_values(self):
        """Verify default settings instantiation."""
        with patch.dict(os.environ, {}, clear=True):
            s = Settings(_env_file=None)
            self.assertEqual(s.PORT, 8000)
            self.assertEqual(s.port, 8000)
            self.assertEqual(s.HOST, "127.0.0.1")
            self.assertEqual(s.host, "127.0.0.1")
            self.assertEqual(s.CORS_ORIGINS, ["*"])
            self.assertEqual(s.cors_origins, ["*"])
            self.assertEqual(s.GEMINI_MODEL, "gemini-2.5-flash")
            self.assertEqual(s.gemini_model, "gemini-2.5-flash")
            self.assertEqual(s.market_cache_ttl_seconds, 21600)
            self.assertEqual(s.MARKET_CACHE_TTL_SECONDS, 21600)
            self.assertEqual(s.desk_name, "Global Agro Arbitrage Desk")
            self.assertEqual(s.DESK_NAME, "Global Agro Arbitrage Desk")

    def test_uppercase_env_vars_loading(self):
        """Verify that standard uppercase environment variables are loaded properly."""
        env_overrides = {
            "GEMINI_API_KEY": "test-gemini-key",
            "EMAIL_USER": "trader@firm.com",
            "EMAIL_PASS": "secret-pass",
            "DESK_EMAIL": "desk@firm.com",
            "PORT": "9090",
            "HOST": "0.0.0.0",
            "DATABASE_URL": "postgresql://user:pass@db:5432/arbitrage",
            "CORS_ORIGINS": '["http://localhost:3000"]',
            "MARKET_CACHE_TTL_SECONDS": "3600",
        }
        with patch.dict(os.environ, env_overrides, clear=True):
            s = Settings(_env_file=None)
            self.assertEqual(s.GEMINI_API_KEY, "test-gemini-key")
            self.assertEqual(s.gemini_api_key, "test-gemini-key")
            self.assertEqual(s.EMAIL_USER, "trader@firm.com")
            self.assertEqual(s.email_user, "trader@firm.com")
            self.assertEqual(s.EMAIL_PASS, "secret-pass")
            self.assertEqual(s.email_pass, "secret-pass")
            self.assertEqual(s.desk_email, "desk@firm.com")
            self.assertEqual(s.DESK_EMAIL, "desk@firm.com")
            self.assertEqual(s.PORT, 9090)
            self.assertEqual(s.port, 9090)
            self.assertEqual(s.HOST, "0.0.0.0")
            self.assertEqual(s.host, "0.0.0.0")
            self.assertEqual(s.DATABASE_URL, "postgresql://user:pass@db:5432/arbitrage")
            self.assertEqual(s.database_url, "postgresql://user:pass@db:5432/arbitrage")
            self.assertEqual(s.CORS_ORIGINS, ["http://localhost:3000"])
            self.assertEqual(s.cors_origins, ["http://localhost:3000"])
            self.assertEqual(s.market_cache_ttl_seconds, 3600)
            self.assertEqual(s.MARKET_CACHE_TTL_SECONDS, 3600)

    def test_cors_origins_comma_separated_string(self):
        """Verify that comma-separated CORS_ORIGINS is parsed without SettingsError."""
        with patch.dict(os.environ, {
            "CORS_ORIGINS": "http://alpha.com, http://beta.com,http://gamma.com",
        }, clear=True):
            s = Settings(_env_file=None)
            self.assertEqual(s.cors_origins, ["http://alpha.com", "http://beta.com", "http://gamma.com"])
            self.assertEqual(s.CORS_ORIGINS, ["http://alpha.com", "http://beta.com", "http://gamma.com"])

    def test_cors_origins_json_array(self):
        """Verify that JSON array CORS_ORIGINS is parsed properly."""
        with patch.dict(os.environ, {
            "CORS_ORIGINS": '["http://localhost:3000", "http://127.0.0.1:8000"]',
        }, clear=True):
            s = Settings(_env_file=None)
            self.assertEqual(s.cors_origins, ["http://localhost:3000", "http://127.0.0.1:8000"])
            self.assertEqual(s.CORS_ORIGINS, ["http://localhost:3000", "http://127.0.0.1:8000"])

    def test_cors_origins_single_string(self):
        """Verify that single string wildcard or domain is wrapped properly."""
        with patch.dict(os.environ, {"CORS_ORIGINS": "*"}, clear=True):
            s = Settings(_env_file=None)
            self.assertEqual(s.cors_origins, ["*"])
            self.assertEqual(s.CORS_ORIGINS, ["*"])

        with patch.dict(os.environ, {"CORS_ORIGINS": "https://example.com"}, clear=True):
            s = Settings(_env_file=None)
            self.assertEqual(s.cors_origins, ["https://example.com"])
            self.assertEqual(s.CORS_ORIGINS, ["https://example.com"])

    def test_settings_getattr_bidirectional(self):
        """Verify that uppercase, lowercase, and mixed-case attributes can be accessed via __getattr__."""
        s = settings
        self.assertEqual(s.PORT, s.port)
        self.assertEqual(s.HOST, s.host)
        self.assertEqual(s.DATABASE_URL, s.database_url)
        self.assertEqual(s.DESK_NAME, s.desk_name)
        self.assertEqual(s.DESK_EMAIL, s.desk_email)
        self.assertEqual(s.GEMINI_API_KEY, s.gemini_api_key)
        self.assertEqual(s.CORS_ORIGINS, s.cors_origins)
        self.assertEqual(s.default_commodity, s.DEFAULT_COMMODITY)
        self.assertEqual(s.Desk_Name, s.desk_name)
        self.assertTrue(hasattr(s, "database_url"))
        self.assertTrue(hasattr(s, "DATABASE_URL"))
        self.assertTrue(hasattr(s, "port"))
        self.assertTrue(hasattr(s, "PORT"))

    def test_settings_getattr_raises_attribute_error_for_unknown(self):
        """Verify that accessing non-existent attribute raises AttributeError."""
        s = settings
        with self.assertRaises(AttributeError):
            _ = s.NON_EXISTENT_ATTRIBUTE_12345


if __name__ == "__main__":
    unittest.main()
