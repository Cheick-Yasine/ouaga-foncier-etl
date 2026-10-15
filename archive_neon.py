"""Archivage des bruts dans la base existante, sans publication GitHub."""
import hashlib
import json
import os
from pathlib import Path
import psycopg


def archive_raw(path: Path):
    payload = path.read_text(encoding='utf-8')
    json.loads(payload)
    digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    try:
        with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=30) as conn:
            # Sérialiser la création initiale lors du départ simultané des comptes.
            conn.execute('SELECT pg_advisory_xact_lock(786241093)')
            conn.execute('''CREATE TABLE IF NOT EXISTS etl_backfill_raw (
                digest TEXT PRIMARY KEY, compte TEXT NOT NULL,
                fichier TEXT NOT NULL, contenu JSONB NOT NULL,
                archive_le TIMESTAMPTZ NOT NULL DEFAULT now()
            )''')
            conn.execute('''INSERT INTO etl_backfill_raw(digest,compte,fichier,contenu)
                VALUES(%s,%s,%s,%s::jsonb) ON CONFLICT(digest) DO NOTHING''',
                (digest, os.environ['COMPTE'], path.name, payload))
    except Exception:
        raise RuntimeError('Archivage brut Neon impossible ; collecte interrompue pour ne pas perdre les fichiers.') from None
