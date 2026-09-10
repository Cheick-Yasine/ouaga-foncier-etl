from scripts import github_backfill
from types import SimpleNamespace


def test_selects_only_requested_account(monkeypatch, tmp_path):
    for key, value in {'COMPTE':'2', 'DAYS_BACK':'10', 'COOKIES_COMPTE':'test-only', 'DATABASE_URL':'test-db', 'OPENAI_API_KEY':'test-key', 'OUAGA_PERSISTENT_DIR':str(tmp_path)}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv('OUAGA_PROGRESS_FILE', '')
    monkeypatch.setenv('PYTHONUNBUFFERED', '0')
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


def test_progress_only_displays_numeric_counts(tmp_path, capsys):
    import json
    import threading
    from progress_public import render
    assert render({'event': 'open', 'group': 'secret-cookie'}, {}) is None
    path = tmp_path / 'progress.jsonl'
    events = [
        {'event':'open', 'group':'123', 'text':'PRIVATE'},
        {'event':'scroll', 'group':'123', 'scroll':2, 'network':50,
         'graphql':1, 'dom':12, 'captured':10, 'selected':5, 'cookie':'PRIVATE'},
        {'event':'archived', 'group':'123', 'archived':5},
    ]
    path.write_text(''.join(json.dumps(e) + '\n' for e in events), encoding='utf-8')
    stop = threading.Event()
    stop.set()
    github_backfill.follow_progress(path, stop, '2')
    output = capsys.readouterr().out
    assert 'publications capturées=10' in output
    assert 'retenues pour ce groupe=5' in output
    assert 'Neon confirmé : 5' in output
    assert 'PRIVATE' not in output
    assert '123' not in output
