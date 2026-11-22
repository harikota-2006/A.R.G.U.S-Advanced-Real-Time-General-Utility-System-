"""Configuration only; never performs network calls or logs credentials."""
from __future__ import annotations
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    if os.getenv('ARGUS_PUBLIC_DEPLOYMENT') != '1':
        load_dotenv(BASE_DIR / '.env', override=False)
except ImportError:
    # The full dotenv parser is installed by requirements; imports remain diagnostic-safe.
    pass

def env(name: str, default: str = '') -> str:
    return os.environ.get(name, default).strip()

DATA_DIR = Path(env('ARGUS_DATA_DIR', str(BASE_DIR / 'data'))).expanduser().resolve()

def ntfy_url(direction: str) -> str:
    value = env(f'NTFY_{direction}_URL')
    if direction == 'TX' and not value and env('NTFY_TOPIC'):
        return env('NTFY_BASE_URL', 'https://ntfy.sh').rstrip('/') + '/' + env('NTFY_TOPIC')
    return value.rstrip('/')
