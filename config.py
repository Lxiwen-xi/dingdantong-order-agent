"""Project-local settings. API keys are never returned to the browser or logs."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values
from secret_store import SecretStoreError, read_deepseek_key

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default='', repr=False)
    model: str = 'deepseek-flash'
    base_url: str = 'https://api.deepseek.com'
    key_source: str = 'none'
    key_error: str = ''


def read_settings() -> Settings:
    values = dotenv_values(ROOT / '.env', interpolate=False)
    def get(name, default=''):
        return str(os.environ.get(name, values.get(name) or default)).strip()
    # A project explicitly set to encrypted storage never falls back to plaintext.
    storage = str(values.get('DEEPSEEK_KEY_STORAGE') or
                  os.environ.get('DEEPSEEK_KEY_STORAGE', '')).strip()
    error = ''
    if storage == 'windows_dpapi':
        source = 'windows_dpapi'
        try:
            key = read_deepseek_key()
        except SecretStoreError:
            key, error = '', 'secret_storage'
    elif storage in ('', 'environment'):
        key = get('DEEPSEEK_API_KEY') if not storage else os.environ.get('DEEPSEEK_API_KEY', '').strip()
        source = ('environment' if os.environ.get('DEEPSEEK_API_KEY') else 'env_file') if key else 'none'
    else:
        key, source, error = '', 'none', 'secret_storage'
    return Settings(key, get('DEEPSEEK_MODEL', 'deepseek-flash'),
                    get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com'), source, error)
