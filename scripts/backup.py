"""Archive restaurable des données locales ; sessions et secrets exclus."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import zipfile
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from durable import account_lock
from dotenv import load_dotenv


def main():
    load_dotenv(ROOT / '.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('OUAGA_PERSISTENT_DIR', str(Path.home() / 'ouaga-etl-data'))))
    parser.add_argument('--destination', type=Path, required=True, help='Autre disque ou dossier de sauvegarde protégé.')
    args = parser.parse_args()
    base = args.data_dir.expanduser().resolve()
    args.destination.mkdir(parents=True, exist_ok=True)
    filename = args.destination / ('ouaga-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.zip')
    temp = filename.with_suffix('.tmp')
    try:
        with ExitStack() as stack, tempfile.TemporaryDirectory() as scratch:
            for account in '12345':
                stack.enter_context(account_lock(base / f'compte_{account}' / 'daily.lock'))
            with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED) as archive:
                for account in '12345':
                    root = base / f'compte_{account}'
                    for folder in ['raw', 'processed', 'state']:
                        for file in (root / folder).rglob('*'):
                            if file.is_file() and file.suffix not in ('.tmp',) and file.name != 'storage_state.json':
                                archive.write(file, file.relative_to(base))
                    if (root / 'status.json').exists():
                        archive.write(root / 'status.json', f'compte_{account}/status.json')
                    db_path = root / 'journal.sqlite3'
                    if db_path.exists():
                        snapshot = Path(scratch) / f'{account}.sqlite3'
                        with sqlite3.connect(f'file:{db_path.as_posix()}?mode=ro', uri=True) as source, sqlite3.connect(snapshot) as dest:
                            source.backup(dest)
                        archive.write(snapshot, f'compte_{account}/journal.sqlite3')
                archive.write(ROOT / 'groups.csv', 'groups.csv')
        os.replace(temp, filename)
        print(filename)
    finally:
        temp.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
