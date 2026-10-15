"""Unit tests: `python -m unittest`. They don't need Android, and they keep out of the real log."""
import tempfile
from pathlib import Path

from androiddesktop import log

_logs = tempfile.TemporaryDirectory(prefix="androiddesktop-tests-")
log.LOGS = Path(_logs.name)
