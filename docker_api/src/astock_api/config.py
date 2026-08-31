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

# Update cycle automation (P9.4 Step 2)
# Default OFF until wired + regression-tested. Interval = minutes between
# triggers; tick = seconds between cheap in-memory due-checks in the service loop.
UPDATE_CYCLE_ENABLED = os.getenv("UPDATE_CYCLE_ENABLED", "false").lower() == "true"
UPDATE_CYCLE_INTERVAL_MIN = int(os.getenv("UPDATE_CYCLE_INTERVAL_MIN", "1440"))
UPDATE_CYCLE_TICK_SECONDS = int(os.getenv("UPDATE_CYCLE_TICK_SECONDS", "60"))

# Data/Cache directories
DATA_DIR = os.getenv("ASTOCK_DATA_DIR", "/app/data")
CACHE_DIR = os.getenv("ASTOCK_CACHE_DIR", "/app/cache")

# Logging
LOG_LEVEL = os.getenv("ASTOCK_LOG_LEVEL", "INFO")

# Build metadata (injected at Docker build time via ENV)
BUILD_SERVICE = os.getenv("BUILD_SERVICE", "a-stock-data-api")
BUILD_IMAGE = os.getenv("BUILD_IMAGE", "")
BUILD_PHASE = os.getenv("BUILD_PHASE", "")
BUILD_RELEASE = os.getenv("BUILD_RELEASE", "")
BUILD_COMMIT = os.getenv("BUILD_COMMIT", "")
BUILD_TIME = os.getenv("BUILD_TIME", "")
