"""Adaptateur GitHub : un seul secret de compte et aucune session en artefact."""
import os
from pathlib import Path
import json
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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
    # Les logs détaillés restent éphémères : pas de texte d'annonce sur GitHub.
    with (base / f'compte-{account}.log').open('w', encoding='utf-8') as log:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / 'daily.py'),
                                 '--compte', account, '--backfill-days', str(days),
                                 '--collect-timeout', '10800'], stdout=log, stderr=log).returncode
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
