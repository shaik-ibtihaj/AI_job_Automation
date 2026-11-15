"""Windows launcher bootstrap; install only missing project dependencies."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None):
    if sys.version_info < (3, 11):
        print("ERROR: Python 3.11 or newer is required.", file=sys.stderr)
        return 1
    from src.run_store import RunLock, AlreadyRunning
    try:
        with RunLock(ROOT / "data/.environment.lock"):
            python = ROOT / ".venv/Scripts/python.exe"
            if not python.exists():
                print("Creating the project .venv...")
                result = subprocess.run([sys.executable, "-m", "venv", str(ROOT / ".venv")], cwd=ROOT)
                if result.returncode:
                    print("ERROR: Virtual environment creation failed. Check Python's venv support and folder permissions.")
                    return 1
            check = subprocess.run([str(python), "-c",
                "import sys,importlib.metadata; v=importlib.metadata.version('pypdf'); "
                "assert sys.version_info >= (3,11) and sys.prefix != sys.base_prefix; assert 6 <= int(v.split('.')[0]) < 8"],
                cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if check.returncode:
                print("Checking/installing requirements.txt into .venv (no resume data is transmitted)...")
                result = subprocess.run([str(python), "-m", "pip", "--disable-pip-version-check", "install", "--timeout", "20",
                                         "--retries", "1", "-r", str(ROOT / "requirements.txt")], cwd=ROOT)
                if result.returncode:
                    print("ERROR: Dependency setup failed. Check network access, then run .venv\\Scripts\\python.exe -m pip install -r requirements.txt")
                    return 1
                check = subprocess.run([str(python), "-c", "import pypdf,sys; assert sys.version_info >= (3,11)"], cwd=ROOT)
                if check.returncode:
                    print("ERROR: The environment is incompatible; recreate .venv using Python 3.11 or newer.")
                    return 1
        return subprocess.run([str(python), "-m", "src.workflow", *(sys.argv[1:] if argv is None else argv)], cwd=ROOT).returncode
    except (OSError, AlreadyRunning) as exc:
        print(f"ERROR: Environment setup could not complete ({type(exc).__name__}). Check another active run and folder permissions.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
