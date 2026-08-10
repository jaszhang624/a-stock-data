"""Application configuration."""
import os

# Upstream metadata
UPSTREAM_REPO = "https://github.com/simonlin1212/a-stock-data"
UPSTREAM_VERSION = "3.5.1"
UPSTREAM_COMMIT = "281fc69a0b733ffc6458fe2cf9f1ea56804aa886"
API_VERSION = "v1"

# Server
HOST = os.getenv("ASTOCK_HOST", "0.0.0.0")
PORT = int(os.getenv("ASTOCK_PORT", "8000"))

# API Key (optional in dev mode)
ASTOCK_API_KEY = os.getenv("ASTOCK_API_KEY", "")

# iwencai API Key (optional)
IWENCAI_API_KEY = os.getenv("IWENCAI_API_KEY", "")

# Cache
CACHE_ENABLED = os.getenv("CACHE_ENABLED", "false").lower() == "true"

# Data/Cache directories
DATA_DIR = os.getenv("ASTOCK_DATA_DIR", "/app/data")
CACHE_DIR = os.getenv("ASTOCK_CACHE_DIR", "/app/cache")

# Logging
LOG_LEVEL = os.getenv("ASTOCK_LOG_LEVEL", "INFO")
