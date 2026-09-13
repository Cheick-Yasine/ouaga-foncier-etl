import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from scripts import session_local

@pytest.mark.parametrize('reason,allowed', [
    ('session expirée sur groupe: déconnexion', True),
    ('blocage détecté sur groupe: checkpoint', False),
    ('autre raison', False),
])
def test_pause_selective(tmp_path, reason, allowed):
    path = tmp_path / 'pause.json'
    path.write_text(json.dumps({'raison': reason}))
    assert session_local.cooldown_session(path) is allowed

@pytest.mark.asyncio
@pytest.mark.parametrize('actor,valid', [('123', True), ('999', False), ('0', False), ('', False)])
async def test_preuve_positive_session(monkeypatch, actor, valid):
    import playwright.async_api
    import scraper
    cookies = [{'name': 'c_user', 'value': '123'}]
    page = SimpleNamespace(goto=AsyncMock(), content=AsyncMock(return_value='"USER_ID":"'+actor+'"'))
    context = SimpleNamespace(add_cookies=AsyncMock(), new_page=AsyncMock(return_value=page), cookies=AsyncMock(return_value=cookies))
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    manager = AsyncMock()
    manager.__aenter__.return_value = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
    monkeypatch.setattr(playwright.async_api, 'async_playwright', lambda: manager)
    monkeypatch.setattr(scraper, 'detecter_blocage_ou_session_expiree', AsyncMock())
    if valid:
        assert await session_local.verifier_session(cookies) == cookies
    else:
        with pytest.raises(ValueError):
            await session_local.verifier_session(cookies)
    browser.close.assert_awaited_once()
    page.goto.assert_awaited_once()
