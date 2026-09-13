"""Fallback lightweight implementation of python-dotenv module."""
import os
from pathlib import Path


def dotenv_values(dotenv_path=None, **kwargs):
    path = Path(dotenv_path) if dotenv_path else Path('.env')
    res = {}
    if not path.exists():
        return res
    try:
        content = path.read_text(encoding='utf-8')
    except Exception:
        return res

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' in line:
            key, val = line.split('=', 1)
            key = key.strip()
            val = val.strip()
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            res[key] = val
    return res


def load_dotenv(dotenv_path=None, override=False, **kwargs):
    values = dotenv_values(dotenv_path)
    for k, v in values.items():
        if override or k not in os.environ:
            os.environ[k] = v
    return True


def set_key(dotenv_path, key_to_set, value_to_set, **kwargs):
    path = Path(dotenv_path)
    lines = []
    found = False
    if path.exists():
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except Exception:
            lines = []

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and '=' in stripped:
            k = stripped.split('=', 1)[0].strip()
            if k == key_to_set:
                new_lines.append(f"{key_to_set}={value_to_set}")
                found = True
                continue
        new_lines.append(line)

    if not found:
        new_lines.append(f"{key_to_set}={value_to_set}")

    path.write_text('\n'.join(new_lines) + '\n', encoding='utf-8')
    return True, key_to_set, value_to_set


def unset_key(dotenv_path, key_to_unset, **kwargs):
    path = Path(dotenv_path)
    if not path.exists():
        return True, key_to_unset
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except Exception:
        return True, key_to_unset

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and '=' in stripped:
            k = stripped.split('=', 1)[0].strip()
            if k == key_to_unset:
                continue
        new_lines.append(line)

    path.write_text('\n'.join(new_lines) + '\n', encoding='utf-8')
    return True, key_to_unset
