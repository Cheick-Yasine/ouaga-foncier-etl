import json
import pytest
import psycopg
from archive_neon import archive_raw


def test_archive_idempotent(tmp_path, monkeypatch, base_test_isolee):
    monkeypatch.setenv('DATABASE_URL', base_test_isolee)
    monkeypatch.setenv('COMPTE', '2')
    path = tmp_path / 'brut.json'
    path.write_text(json.dumps([{'id':'archive-test-only','texte':'test'}]))
    archive_raw(path)
    archive_raw(path)
    with psycopg.connect(base_test_isolee) as conn:
        rows = conn.execute("SELECT compte, contenu FROM etl_backfill_raw WHERE fichier='brut.json'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == '2'
    assert rows[0][1][0]['id'] == 'archive-test-only'


def test_archive_failure_is_explicit_without_secret(tmp_path, monkeypatch):
    path = tmp_path / 'brut.json'
    path.write_text('[]')
    monkeypatch.setenv('DATABASE_URL', 'sensitive-test-value')
    monkeypatch.setattr(psycopg, 'connect', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('sensitive-test-value')))
    with pytest.raises(RuntimeError, match='Archivage brut Neon impossible') as exc:
        archive_raw(path)
    assert 'sensitive-test-value' not in str(exc.value)
    assert exc.value.__suppress_context__
