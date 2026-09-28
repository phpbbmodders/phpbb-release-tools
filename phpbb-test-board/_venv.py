"""
Run the calling script inside this folder's own virtualenv.

Scripts call ensure_venv() before importing any third-party package. On the
first run it creates phpbb-test-board/.venv and installs requirements.txt;
after that it re-executes the script with the virtualenv's Python, so nobody
has to create or activate anything by hand. Uses only the standard library.
"""
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
REQUIREMENTS = HERE / "requirements.txt"
STAMP = VENV / ".requirements-installed"


def ensure_venv() -> None:
    """Re-exec the running script inside .venv, creating it first if needed."""
    python = VENV / "bin" / "python"
    if Path(sys.prefix).resolve() == VENV.resolve():
        return
    if not python.exists():
        print(f"Creating virtualenv in {VENV} ...", file=sys.stderr)
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    if not STAMP.exists() or STAMP.read_text() != REQUIREMENTS.read_text():
        subprocess.run([str(python), "-m", "pip", "install", "-q", "-r", str(REQUIREMENTS)], check=True)
        STAMP.write_text(REQUIREMENTS.read_text())
    os.execv(str(python), [str(python), *sys.argv])
