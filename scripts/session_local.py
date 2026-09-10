"""Importer des cookies locaux sans les afficher ni les envoyer à GitHub."""
import argparse
import json
import os
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from durable import atomic_json, account_lock


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compte', choices=list('12345'), required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--export', type=Path)
    source.add_argument('--interactive', action='store_true', help='Ouvre Facebook pour une connexion manuelle, puis enregistre la session localement.')
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('OUAGA_PERSISTENT_DIR', str(Path.home() / 'ouaga-etl-data'))))
    args = parser.parse_args()
    root = args.data_dir.expanduser().resolve() / f'compte_{args.compte}'
    os.environ['OUAGA_DATA_DIR'] = str(root)
    import config
    from scraper import charger_cookies
    from scripts.maj_cookies import valider_cookies_captures, capturer_session_interactive
    with account_lock(root / 'daily.lock'):
        contenu = capturer_session_interactive() if args.interactive else args.export.read_text(encoding='utf-8')
        cookies = charger_cookies(contenu)
        valider_cookies_captures(cookies)
        atomic_json(config.storage_state_path(args.compte), {'cookies': cookies, 'origins': []}, private=True)
    print(f'Session du compte {args.compte} enregistrée localement. Aucun cookie affiché. Le cooldown éventuel reste respecté.')


if __name__ == '__main__':
    main()
