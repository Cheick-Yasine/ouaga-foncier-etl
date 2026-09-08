import json
from pathlib import Path
import pytest
from durable import atomic_json, account_lock
from scripts import daily


def test_atomic_failure_preserves_previous(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    atomic_json(path, {'old': True})
    import os
    monkeypatch.setattr(os, 'replace', lambda *a: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        atomic_json(path, {'new': True})
    assert json.loads(path.read_text()) == {'old': True}
    assert not list(tmp_path.glob('*.tmp'))


def test_lock_released_on_exception(tmp_path):
    path = tmp_path / 'lock'
    with pytest.raises(ValueError):
        with account_lock(path):
            raise ValueError()
    with account_lock(path):
        pass


def test_failed_processing_replayed_after_collection_failure(tmp_path, monkeypatch):
    root = tmp_path / 'compte_1'
    (root / 'raw').mkdir(parents=True)
    raw = root / 'raw' / 'old.json'
    raw.write_text('[]')
    codes = iter([2, 1, 2, 0])
    monkeypatch.setattr(daily, 'run_child', lambda *a: next(codes))
    assert daily.execute_account('1', tmp_path, 10, 10) == 1
    db = daily.journal(root / 'journal.sqlite3')
    assert daily.pending_files(db, root) == [raw]
    db.close()
    assert daily.execute_account('1', tmp_path, 10, 10) == 1  # session toujours invalide
    db = daily.journal(root / 'journal.sqlite3')
    assert daily.pending_files(db, root) == []
    assert raw.exists()  # archive brute conservée
    db.close()


def test_success_not_reprocessed_and_account_isolation(tmp_path, monkeypatch):
    for account in ['1', '2']:
        root = tmp_path / f'compte_{account}' / 'raw'
        root.mkdir(parents=True)
        (root / 'same.json').write_text('[]')
    calls = []
    monkeypatch.setattr(daily, 'run_child', lambda args, *a: calls.append(args) or 0)
    assert daily.execute_account('1', tmp_path, 10, 10, True) == 0
    assert daily.execute_account('1', tmp_path, 10, 10, True) == 0
    assert len(calls) == 1
    assert daily.execute_account('2', tmp_path, 10, 10, True) == 0
    assert len(calls) == 2


def test_interrupted_run_visible(tmp_path, monkeypatch):
    root = tmp_path / 'compte_1'
    root.mkdir()
    db = daily.journal(root / 'journal.sqlite3')
    db.execute("INSERT INTO runs(id,started,status) VALUES('old',?,'en_cours')", (daily.now(),))
    db.commit()
    db.close()
    monkeypatch.setattr(daily, 'run_child', lambda *a: 0)
    daily.execute_account('1', tmp_path, 10, 10)
    db = daily.journal(root / 'journal.sqlite3')
    assert db.execute("SELECT status FROM runs WHERE id='old'").fetchone()[0] == 'interrompu'
    db.close()


def test_timeout_terminates_child(tmp_path, monkeypatch):
    import os
    (tmp_path / 'main.py').write_text('import time; time.sleep(60)')
    monkeypatch.setattr(daily, 'ROOT', tmp_path)
    assert daily.run_child([], dict(os.environ), 0.2) == 124


@pytest.mark.asyncio
async def test_llm_failure_not_acknowledged(monkeypatch, tmp_path):
    import config
    import processor
    from unittest.mock import AsyncMock, Mock
    raw = tmp_path / 'raw.json'
    raw.write_text('[{"id":"1","texte":"Terrain à vendre à Ouaga"}]')
    monkeypatch.setattr(config, 'DATABASE_URL', 'postgresql://unused')
    monkeypatch.setattr(processor, 'structurer_lot', AsyncMock(return_value=([], [{'motif_rejet':'echec_api_ou_validation'}])))
    save = Mock()
    monkeypatch.setattr(processor, 'upsert_annonces', save)
    with pytest.raises(RuntimeError, match='incomplet'):
        await processor.executer_traitement([raw])
    save.assert_not_called()


def test_backup_restores_journal_and_excludes_sessions(tmp_path):
    import subprocess
    import sys
    import zipfile
    base = tmp_path / 'data'
    root = base / 'compte_1'
    (root / 'raw').mkdir(parents=True)
    (root / 'raw' / 'one.json').write_text('[]')
    session = root / 'state' / 'compte_1'
    session.mkdir(parents=True)
    (session / 'storage_state.json').write_text('{"secret":true}')
    db = daily.journal(root / 'journal.sqlite3')
    db.execute("INSERT INTO processed VALUES('one.json','2026-09-08')")
    db.commit()
    db.close()
    dest = tmp_path / 'backups'
    subprocess.run([sys.executable, str(daily.ROOT / 'scripts/backup.py'), '--data-dir', str(base), '--destination', str(dest)], check=True)
    archive = next(dest.glob('*.zip'))
    with zipfile.ZipFile(archive) as z:
        assert not any('storage_state' in n for n in z.namelist())
        assert 'compte_1/raw/one.json' in z.namelist()
        z.extractall(tmp_path / 'restore')
    db = daily.journal(tmp_path / 'restore/compte_1/journal.sqlite3')
    assert db.execute('SELECT path FROM processed').fetchone()[0] == 'one.json'
    db.close()
