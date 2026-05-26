from pathlib import Path
import os


_PROJECT_ROOT = Path(__file__).parent.parent


def _load_env_file(path: Path):
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(_PROJECT_ROOT / '.env')


class Config:
    def __init__(self):
        self.TESTVECTOR_ROOT = os.environ.get(
            'TESTVECTOR_ROOT', '/home/rebellions/yeun/testvector'
        )
        self.DEFAULT_WORKLOAD_DIR = os.environ.get(
            'DEFAULT_WORKLOAD_DIR', 'cr13/v3.2.0'
        )
        self.JOBS_DIR = os.environ.get('JOBS_DIR', str((_PROJECT_ROOT / 'jobs').resolve()))
        self.RUN_SCRIPT = os.environ.get('RUN_SCRIPT', str((_PROJECT_ROOT / 'run_workloads.sh').resolve()))
        self.LOG_DIR = os.environ.get('LOG_DIR', str((_PROJECT_ROOT / 'logs').resolve()))
        self.PORT = int(os.environ.get('PORT', 5000))
        self.HOST = os.environ.get('HOST', '0.0.0.0')
        self.DEBUG = os.environ.get('DEBUG', 'false').lower() == 'true'

        pwd = os.environ.get('POWEROFF_SUDO_PASSWORD', '')
        self.POWEROFF_SUDO_PASSWORD = pwd
        if pwd:
            self.SUDO_CMD = ('sudo', '-S', '-p', '')
            self.SUDO_INPUT = pwd + '\n'
        else:
            # No password: try without sudo first (sensor.py handles fallback)
            self.SUDO_CMD = ()
            self.SUDO_INPUT = None
