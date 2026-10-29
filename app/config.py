"""Settings, read once from environment variables (and .env if present)."""
import os
import re

from dotenv import load_dotenv

load_dotenv()


def _str(name: str, default: str = "") -> str:
    """A setting with any inline "# comment" removed (python-dotenv keeps those as part of the value)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = re.sub(r"\s+#.*$", "", raw).strip()
    return raw if raw else default


def _int(name: str, default: int) -> int:
    raw = _str(name)
    return int(raw) if raw else default


NEBIUS_API_KEY = _str("NEBIUS_API_KEY", "")
NEBIUS_BASE_URL = _str("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/")
MODEL_FAST = _str("MODEL_FAST", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B")
MODEL_REASON = _str("MODEL_REASON", "nvidia/nemotron-3-super-120b-a12b")

TAVILY_API_KEY = _str("TAVILY_API_KEY", "")

MAX_INPUT_CHARS = _int("MAX_INPUT_CHARS", 24000)
MAX_TOKENS_EXTRACT = _int("MAX_TOKENS_EXTRACT", 6000)
MAX_TOKENS_CRITIQUE = _int("MAX_TOKENS_CRITIQUE", 3000)
RATE_LIMIT_PER_HOUR = _int("RATE_LIMIT_PER_HOUR", 5)
DAILY_CAP = _int("DAILY_CAP", 50)
ASK_PER_HOUR = _int("ASK_PER_HOUR", 20)        # dataset questions per IP per hour
ASK_DAILY_CAP = _int("ASK_DAILY_CAP", 400)     # dataset questions across all users per day
ADMIN_TOKEN = _str("ADMIN_TOKEN")

DB_PATH = _str("DB_PATH", "opendag.db")
SCHEMA_VERSION = "1.0"
