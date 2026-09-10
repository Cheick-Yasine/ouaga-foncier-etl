from scripts import github_backfill
from types import SimpleNamespace


def test_selects_only_requested_account(monkeypatch, tmp_path):
    for key, value in {'COMPTE':'2', 'DAYS_BACK':'10', 'COOKIES_COMPTE':'test-only', 'DATABASE_URL':'test-db', 'OPENAI_API_KEY':'test-key', 'OUAGA_PERSISTENT_DIR':str(tmp_path)}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv('OUAGA_ARCHIVE_NEON', '0')
    monkeypatch.setenv('FB_COOKIES_JSON_2', '')
    calls = []
    monkeypatch.setattr(github_backfill.subprocess, 'run', lambda args, **kwargs: calls.append((args,kwargs)) or SimpleNamespace(returncode=0))
    assert github_backfill.main() == 0
    import os
    assert os.environ['FB_COOKIES_JSON_2'] == 'test-only'
    assert 'COOKIES_COMPTE' not in os.environ
    assert calls[0][0][-6:] == ['--compte', '2', '--backfill-days', '10', '--collect-timeout', '10800']
    assert calls[0][1]['stdout'] == calls[0][1]['stderr']
    monkeypatch.delenv('FB_COOKIES_JSON_2')
    monkeypatch.delenv('OUAGA_ARCHIVE_NEON')


def test_missing_secret_stops_before_collecting(monkeypatch, capsys):
    monkeypatch.setenv('COMPTE', '1')
    monkeypatch.setenv('DAYS_BACK', '10')
    monkeypatch.delenv('COOKIES_COMPTE', raising=False)
    assert github_backfill.main() == 1
    assert 'FB_COOKIES_JSON_1' in capsys.readouterr().err
