import faulthandler
import os
import sys
from datetime import datetime
from pathlib import Path


class TeeStream:
    """Write stdout/stderr both to the original stream and to a persistent log file."""

    def __init__(self, original, logfile):
        self.original = original
        self.logfile = logfile
        self.encoding = getattr(original, "encoding", "utf-8")
        self.errors = getattr(original, "errors", None)
        self.name = getattr(original, "name", None)

    def write(self, data):
        try:
            self.original.write(data)
            self.original.flush()
        except Exception:
            pass
        try:
            self.logfile.write(data)
            self.logfile.flush()
        except Exception:
            pass
        return len(data)

    def flush(self):
        try:
            self.original.flush()
        except Exception:
            pass
        try:
            self.logfile.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return self.original.isatty()
        except Exception:
            return False

    def fileno(self):
        """Expose a real file descriptor for libraries that expect file-like streams."""
        try:
            return self.original.fileno()
        except Exception:
            # Fall back to the logfile descriptor if the original stream has none.
            return self.logfile.fileno()

    def writable(self):
        return True


_LOGFILE_HANDLE = None
_LOGGING_INITIALIZED = False


def _candidate_log_paths():
    env_path = os.environ.get("KI_TELEFON_LOG")
    if env_path:
        yield Path(env_path).expanduser()

    # If the service runs as root, this gives us a proper persistent system log.
    yield Path("/var/log/ki-telefon/ki-telefon.log")

    # If the service runs as user pi/ace or another unprivileged account, this still persists across reboot.
    home = Path.home()
    yield home / "ki-telefon.log"

    # Last fallback only. This may be removed on reboot.
    yield Path("/tmp/ki-telefon.log")


def _open_first_writable_logfile():
    errors = []
    for path in _candidate_log_paths():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            return path, open(path, "a", buffering=1, encoding="utf-8")
        except Exception as exc:
            errors.append(f"{path}: {exc}")

    # If even /tmp failed, keep original stdout/stderr and raise a compact error.
    raise RuntimeError("Kein beschreibbarer Logpfad gefunden: " + " | ".join(errors))


def setup_logging():
    """Redirect stdout/stderr to journal/console and a persistent logfile.

    Returns the selected logfile path as string. Safe to call multiple times.
    """
    global _LOGFILE_HANDLE, _LOGGING_INITIALIZED

    if _LOGGING_INITIALIZED:
        return getattr(_LOGFILE_HANDLE, "name", None)

    log_path, _LOGFILE_HANDLE = _open_first_writable_logfile()

    # faulthandler needs a real file object / file descriptor. Do not pass the
    # TeeStream wrapper here. We write fatal tracebacks directly into the logfile.
    try:
        faulthandler.enable(file=_LOGFILE_HANDLE, all_threads=True)
    except Exception as exc:
        # Logging must never prevent the service from starting.
        try:
            sys.__stderr__.write(f"Warnung: faulthandler konnte nicht aktiviert werden: {exc}\n")
            sys.__stderr__.flush()
        except Exception:
            pass

    sys.stdout = TeeStream(sys.__stdout__, _LOGFILE_HANDLE)
    sys.stderr = TeeStream(sys.__stderr__, _LOGFILE_HANDLE)

    _LOGGING_INITIALIZED = True

    print("=" * 80)
    print(f"KI-Telefon Start: {datetime.now().isoformat(timespec='seconds')}")
    print(f"Logfile: {log_path}")
    print("=" * 80)

    return str(log_path)
