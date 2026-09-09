"""Print configuration errors without exposing secrets or a traceback."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import config
    config.validate_config()
except Exception as exc:
    print(f'Configuration error: {exc}')
    print(r'Run: .\.venv\Scripts\python.exe scripts\configure.py')
    raise SystemExit(1)

print('Configuration format is valid.')
