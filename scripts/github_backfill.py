"""Adaptateur GitHub : un seul secret de compte et aucune session en artefact."""
import os
from pathlib import Path
import json
import subprocess
import sys
import threading
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from progress_public import render


def follow_progress(path, stop, account):
    groups = {}
    last = time.monotonic()
    with path.open(encoding='utf-8') as source:
        while True:
            finished = stop.is_set()
            for line in source:
                try:
                    message = render(json.loads(line), groups)
                except (ValueError, TypeError, AttributeError):
                    message = None
                if message:
                    print(f'Compte {account} | {message}', flush=True)
                    last = time.monotonic()
            if finished:
                break
            if time.monotonic() - last >= 60:
                print(f'Compte {account} | processus actif ; aucun nouvel événement de collecte depuis 60 s (cela ne confirme pas une extraction).', flush=True)
                last = time.monotonic()
            stop.wait(1)


def main():
    account = os.environ.get('COMPTE', '')
    try:
        days = int(os.environ.get('DAYS_BACK', '10'))
    except ValueError:
        print('DAYS_BACK doit être un entier de 1 à 14.', file=sys.stderr)
        return 1
    if account not in {'1', '2', '3', '4', '5'} or not 1 <= days <= 14:
        print('Compte ou fenêtre de rattrapage invalide.', file=sys.stderr)
        return 1
    missing = [name for name in ['COOKIES_COMPTE', 'DATABASE_URL', 'OPENAI_API_KEY'] if not os.environ.get(name, '').strip()]
    if missing:
        print('Configuration GitHub manquante : ' + ', '.join(missing).replace('COOKIES_COMPTE', f'FB_COOKIES_JSON_{account}'), file=sys.stderr)
        return 1
    os.environ[f'FB_COOKIES_JSON_{account}'] = os.environ.pop('COOKIES_COMPTE')
    os.environ['OUAGA_ARCHIVE_NEON'] = '1'
    base = Path(os.environ['OUAGA_PERSISTENT_DIR'])
    base.mkdir(parents=True, exist_ok=True)
    progress_path = base / f'compte-{account}-progress.jsonl'
    progress_path.write_text('', encoding='utf-8')
    os.environ['OUAGA_PROGRESS_FILE'] = str(progress_path)
    os.environ['PYTHONUNBUFFERED'] = '1'
    stop = threading.Event()
    follower = threading.Thread(target=follow_progress, args=(progress_path, stop, account), daemon=True)
    print(f'Compte {account} | démarrage du rattrapage sur {days} jours ; progression en direct activée.', flush=True)
    follower.start()
    try:
        # Les détails restent privés ; seuls les événements numériques sont affichés.
        with (base / f'compte-{account}.log').open('w', encoding='utf-8') as log:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / 'daily.py'),
                                     '--compte', account, '--backfill-days', str(days),
                                     '--collect-timeout', '10800'], stdout=log, stderr=log).returncode
    finally:
        stop.set()
        follower.join()
    status_path = base / f'compte_{account}' / 'status.json'
    if status_path.exists():
        status = json.loads(status_path.read_text(encoding='utf-8'))
        print(json.dumps({key: status.get(key) for key in
                         ['compte', 'statut', 'collecte_code', 'fichiers_en_attente', 'jours_recherches']}, ensure_ascii=False))
    else:
        print(f'Compte {account} : arrêt avant production du bilan, code {result}.')
    return result


if __name__ == '__main__':
    raise SystemExit(main())
