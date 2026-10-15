"""Contrôle quotidien et sauvegarde vers le dossier choisi dans .env."""
import os
from pathlib import Path
import subprocess
import sys
from dotenv import load_dotenv
ROOT = Path(__file__).resolve().parents[1]


def main():
    load_dotenv(ROOT / '.env')
    health = subprocess.run([sys.executable, str(ROOT / 'scripts/health.py')], cwd=ROOT).returncode
    destination = os.environ.get('OUAGA_BACKUP_DIR', '').strip()
    if not destination:
        print('Sauvegarde non configurée : compléter OUAGA_BACKUP_DIR dans .env.', file=sys.stderr)
        return 1
    backup = subprocess.run([sys.executable, str(ROOT / 'scripts/backup.py'), '--destination', destination], cwd=ROOT).returncode
    return int(bool(health or backup))


if __name__ == '__main__':
    raise SystemExit(main())
