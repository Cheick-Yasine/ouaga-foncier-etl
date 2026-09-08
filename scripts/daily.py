"""Superviseur quotidien : collecte et reprise du traitement indépendantes.

Chaque compte dispose de son propre répertoire, verrou et journal SQLite.
Aucun changement automatique de compte, d'IP ni réessai d'un contrôle Facebook.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from durable import account_lock, atomic_json


def now():
    return datetime.now(timezone.utc).isoformat()


def journal(path):
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, started TEXT, finished TEXT, collect_code INTEGER, pending INTEGER, status TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS processed (path TEXT PRIMARY KEY, finished TEXT)')
    db.commit()
    return db


def run_child(args, env, timeout):
    # Session/process group dédiée : le timeout ferme aussi Chromium.
    kwargs = {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    child = subprocess.Popen([sys.executable, str(ROOT / 'main.py'), *args], cwd=ROOT, env=env, **kwargs)
    try:
        return child.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'], capture_output=True)
        else:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        return 124


def pending_files(db, root):
    done = {row[0] for row in db.execute('SELECT path FROM processed')}
    return [p for p in sorted((root / 'raw').glob('*.json')) if p.name not in done]


def execute_account(account, base, collect_timeout, process_timeout, process_only=False):
    root = base / f'compte_{account}'
    root.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, OUAGA_DATA_DIR=str(root))
    with account_lock(root / 'daily.lock'):
        db = journal(root / 'journal.sqlite3')
        try:
            # Un run resté sans fin signale un arrêt de la machine/processus.
            db.execute("UPDATE runs SET status='interrompu', finished=? WHERE finished IS NULL", (now(),))
            ident = uuid.uuid4().hex
            db.execute('INSERT INTO runs(id,started,status) VALUES(?,?,?)', (ident, now(), 'en_cours'))
            db.commit()
            atomic_json(root / 'status.json', dict(compte=account, fin=now(), statut='en_cours', collecte_code=None, fichiers_en_attente=len(pending_files(db, root))))
            previous = db.execute('SELECT finished FROM runs WHERE collect_code=0 AND finished IS NOT NULL ORDER BY finished DESC LIMIT 1').fetchone()
            if previous is None:
                previous = db.execute('SELECT started FROM runs ORDER BY started LIMIT 1').fetchone()
            days = 2
            gap_exceeded = False
            if previous:
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(previous[0])).total_seconds() / 86400
                days = max(2, math.ceil(elapsed) + 1)
                gap_exceeded = days > 14
                days = min(days, 14)
            code = None if process_only else run_child(['--compte', account, '--collect-only', '--days-back', str(days)], env, collect_timeout)
            for path in pending_files(db, root):
                result = run_child(['--compte', account, '--process-file', str(path)], env, process_timeout)
                if result:
                    # Pas de boucle infinie payante : reprise au prochain passage.
                    break
                db.execute('INSERT OR REPLACE INTO processed VALUES (?,?)', (path.name, now()))
                db.commit()
            pending = len(pending_files(db, root))
            status = {0: 'collecte_terminee', 2: 'reconnexion_requise', 3: 'blocage_detecte', 4: 'cooldown', 124: 'timeout', None: 'traitement_seul'}.get(code, 'echec_collecte')
            if pending:
                status += '_traitement_en_attente'
            if gap_exceeded:
                status += '_lacune_historique_a_verifier'
            finished = now()
            db.execute('UPDATE runs SET finished=?, collect_code=?, pending=?, status=? WHERE id=?', (finished, code, pending, status, ident))
            db.commit()
            summary = dict(compte=account, fin=finished, collecte_code=code, fichiers_en_attente=pending, statut=status, jours_recherches=days)
            atomic_json(root / 'status.json', summary)
            print(json.dumps(summary, ensure_ascii=False), flush=True)
            return int(code not in (0, None) or pending > 0 or gap_exceeded)
        finally:
            db.close()


def main(argv=None):
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compte', choices=['all','1','2','3','4','5'], default='all')
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('OUAGA_PERSISTENT_DIR', str(Path.home() / 'ouaga-etl-data'))))
    parser.add_argument('--collect-timeout', type=int, default=7200)
    parser.add_argument('--process-timeout', type=int, default=1800)
    parser.add_argument('--process-only', action='store_true')
    args = parser.parse_args(argv)
    if min(args.collect_timeout, args.process_timeout) <= 0:
        parser.error('Les durées doivent être positives.')
    base = args.data_dir.expanduser().resolve()
    accounts = list('12345') if args.compte == 'all' else [args.compte]
    failed = 0
    for account in accounts:
        try:
            failed |= execute_account(account, base, args.collect_timeout, args.process_timeout, args.process_only)
        except Exception as exc:
            print(f'Compte {account} : superviseur interrompu ({type(exc).__name__}). Vérifier le journal local.', file=sys.stderr)
            failed = 1
    return failed


if __name__ == '__main__':
    raise SystemExit(main())
