"""Contrôle local : code 1 si run/groupes absents, trop anciens ou en erreur."""
import argparse
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def main():
    load_dotenv(ROOT / '.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('OUAGA_PERSISTENT_DIR', str(Path.home() / 'ouaga-etl-data'))))
    parser.add_argument('--max-hours', type=float, default=36)
    args = parser.parse_args()
    failed = False
    with (ROOT / 'groups.csv').open(encoding='utf-8-sig') as stream:
        groups = [r for r in csv.DictReader(stream) if r['actif'].lower() == 'true']
    def stale(value):
        return (datetime.now(timezone.utc) - datetime.fromisoformat(value)).total_seconds() > args.max_hours * 3600
    for account in '12345':
        root = args.data_dir.expanduser().resolve() / f'compte_{account}'
        issues = []
        try:
            status = json.loads((root / 'status.json').read_text())
            if stale(status['fin']):
                issues.append('run trop ancien')
            if status['statut'] != 'collecte_terminee':
                issues.append(status['statut'])
            evidence = json.loads((root / 'state' / f'compte_{account}' / 'groupes.json').read_text())
            for group in [g for g in groups if g['compte'] == account]:
                data = evidence.get(group['id'])
                if not data or stale(data['fin']) or data['statut'] != 'termine':
                    issues.append(f"groupe {group['id']} à vérifier")
            count = sum(v.get('nouveaux_posts', 0) for v in evidence.values() if not stale(v['fin']))
            if count == 0:
                issues.append('aucune nouveauté récente : vérifier activité et extraction')
        except (OSError, ValueError, KeyError, TypeError):
            issues.append('suivi absent ou illisible')
        failed |= bool(issues)
        print(f"Compte {account} : " + (' ; '.join(issues) if issues else 'OK'))
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
