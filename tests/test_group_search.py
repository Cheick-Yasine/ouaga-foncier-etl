from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
import group_search as gs
import scraper


def test_search_url_and_redirect_scope():
    group = 'https://m.facebook.com/groups/123/'
    assert gs.search_url(group, 'terrain').endswith('/groups/123/search/?q=terrain')
    gs.assert_search_url('https://www.facebook.com/groups/123/search/?q=terrain', group, 'terrain')
    for url in ('https://m.facebook.com/groups/123/', 'https://m.facebook.com/groups/456/search/?q=terrain', 'https://m.facebook.com/groups/123/search/?q=parcelle'):
        with pytest.raises(ValueError):
            gs.assert_search_url(url, group, 'terrain')
    with pytest.raises(ValueError):
        gs.search_url('https://m.facebook.com/Immobilier/', 'terrain')


def test_terms_and_dates():
    assert gs.matching_post({'texte': 'TERRAINS à vendre'}, 'terrain', '123')
    assert not gs.matching_post({'texte': 'souterrain'}, 'terrain', '123')
    assert not gs.matching_post({'texte': 'terrain', 'url': '/groups/456/posts/7/'}, 'terrain', '123')
    cutoff = datetime.now(timezone.utc) - timedelta(days=5)
    assert not gs.in_window({'date_publication': (cutoff-timedelta(days=1)).isoformat()}, cutoff)
    assert gs.in_window({'date_publication': (cutoff+timedelta(days=1)).isoformat()}, cutoff)
    assert gs.in_window({'date_publication': None, 'date_incertaine': True}, cutoff)


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_second', [False, True])
async def test_query_order_dedup_and_checkpoint(monkeypatch, fail_second):
    group = SimpleNamespace(id='123', nom='groupe')
    stamp = datetime.now(timezone.utc).isoformat()
    def post(id):
        return {'id': id, 'date_publication': stamp, 'scrape_le': stamp}
    async def collect(*args, **kwargs):
        if kwargs['recherche'] == 'terrain':
            args[3]['a'] = stamp
            return [post('a')], None
        assert 'a' not in args[3]  # ne pas couper parcelle sur les résultats de terrain
        if fail_second:
            raise scraper.StructureFacebookInattendueError('filtre absent')
        return [post('a'), post('b'), post('b')], None
    mock = AsyncMock(side_effect=collect)
    save = Mock(side_effect=lambda posts, id: tuple(p['id'] for p in posts))
    monkeypatch.setattr(scraper, 'scraper_groupe', mock)
    monkeypatch.setattr(scraper, 'sauvegarder_posts_groupe', save)
    files = []
    call = scraper.scraper_recherches_groupe(None, group, 5, {}, fichiers_sauvegardes=files)
    if fail_second:
        with pytest.raises(scraper.StructureFacebookInattendueError):
            await call
        assert files == [('a',)]
    else:
        posts, _ = await call
        assert [p['id'] for p in posts] == ['a', 'b']
        assert files == [('a',), ('b',)]
    assert [c.kwargs['recherche'] for c in mock.await_args_list] == ['terrain', 'parcelle']

@pytest.mark.asyncio
@pytest.mark.parametrize('role,works', [('checkbox', True), ('switch', True), ('radio', True), ('button', True), ('checkbox', False)])
async def test_sort_requires_confirmed_selection(monkeypatch, role, works):
    import playwright.async_api
    state = {'checked': False}
    async def click(**kwargs):
        state['checked'] = works
    control = SimpleNamespace(is_visible=AsyncMock(return_value=True), is_checked=AsyncMock(side_effect=lambda: state['checked']),
                              get_attribute=AsyncMock(side_effect=lambda _: str(state['checked']).lower()), click=AsyncMock(side_effect=click))
    page = SimpleNamespace(get_by_role=lambda r, **kw: SimpleNamespace(count=AsyncMock(return_value=int(r==role)), nth=lambda i: control))
    async def verify(**kwargs):
        assert state['checked'], 'tri non confirmé'
    async def verify_attr(*args, **kwargs):
        await verify()
    monkeypatch.setattr(playwright.async_api, 'expect', lambda _: SimpleNamespace(to_be_checked=verify, to_have_attribute=verify_attr))
    if works:
        assert await gs.select_recent(page)
    else:
        with pytest.raises(AssertionError, match='tri non confirmé'):
            await gs.select_recent(page)
    control.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_activity_recent_is_not_publication_sort():
    assert not gs.RECENT.fullmatch('Activité récente')
    page = SimpleNamespace(get_by_role=lambda *a, **kw: SimpleNamespace(count=AsyncMock(return_value=0)), get_by_text=lambda *a, **kw: SimpleNamespace(count=AsyncMock(return_value=0)))
    assert await gs.select_recent(page) is False


def test_exact_label_from_user_screenshot():
    assert gs.RECENT.fullmatch('Plus récent')
    assert not gs.RECENT.fullmatch('Publications que vous avez vues')

@pytest.mark.asyncio
async def test_neighbor_switch_from_screenshot(monkeypatch):
    import playwright.async_api
    control = SimpleNamespace(is_visible=AsyncMock(return_value=True), get_attribute=AsyncMock(side_effect=lambda k: 'false' if k=='aria-checked' else None), click=AsyncMock())
    switches = SimpleNamespace(count=AsyncMock(return_value=1), first=control)
    row = SimpleNamespace(locator=lambda _: switches)
    label = SimpleNamespace(locator=lambda _: row)
    page = SimpleNamespace(get_by_role=lambda *a, **k: SimpleNamespace(count=AsyncMock(return_value=0)),
                           get_by_text=lambda *a, **k: SimpleNamespace(count=AsyncMock(return_value=1), nth=lambda _: label))
    assertion = AsyncMock()
    monkeypatch.setattr(playwright.async_api, 'expect', lambda _: SimpleNamespace(to_have_attribute=assertion))
    assert await gs.select_recent(page)
    control.click.assert_awaited_once()
    assertion.assert_awaited_once_with('aria-checked', 'true', timeout=5000)


@pytest.mark.asyncio
async def test_redirect_uses_group_search_not_general_feed(monkeypatch):
    page = SimpleNamespace(url='https://m.facebook.com/groups/123/', goto=AsyncMock(),
                           get_by_text=lambda *a, **k: SimpleNamespace(first=SimpleNamespace(wait_for=AsyncMock())))
    group = SimpleNamespace(url=page.url, nom='Groupe')
    scope = AsyncMock(side_effect=[ValueError('route mobile différente'), None])
    fallback = AsyncMock()
    monkeypatch.setattr(gs, 'assert_search_scope', scope)
    monkeypatch.setattr(gs, 'search_via_group_button', fallback)
    monkeypatch.setattr(gs, 'select_recent', AsyncMock(return_value=True))
    monkeypatch.setattr(scraper, 'detecter_blocage_ou_session_expiree', AsyncMock())
    await gs.configure_search(page, group, 'terrain')
    fallback.assert_awaited_once_with(page, group, 'terrain')
    assert scope.await_count == 2


@pytest.mark.asyncio
async def test_rendered_scope_rejects_different_group():
    page = SimpleNamespace(url='https://www.facebook.com/groups/999/search/?query=terrain')
    group = SimpleNamespace(url='https://m.facebook.com/groups/123/', nom='Groupe')
    with pytest.raises(ValueError, match='hors du groupe'):
        await gs.assert_search_scope(page, group, 'terrain')

@pytest.mark.asyncio
async def test_search_setup_failure_does_not_remove_unregistered_listener(monkeypatch, tmp_path):
    import config
    page = SimpleNamespace(content=AsyncMock(return_value='<html>diagnostic</html>'),
                           on=Mock(), remove_listener=Mock(side_effect=KeyError('response')), close=AsyncMock())
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    group = SimpleNamespace(id='123', nom='Groupe', url='https://m.facebook.com/groups/123/')
    monkeypatch.setattr(config, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(gs, 'configure_search', AsyncMock(side_effect=ValueError('Loupe non reconnue')))
    with pytest.raises(scraper.StructureFacebookInattendueError, match='Loupe non reconnue'):
        await scraper.scraper_groupe(context, group, 5, {}, recherche='terrain', rattrapage=True)
    page.remove_listener.assert_not_called()
    page.close.assert_awaited_once()


def test_diagnostic_excludes_secrets_and_retains_search_controls():
    from scripts.diagnostic_recherche import Diagnostic
    import json
    parser = Diagnostic()
    parser.feed('<script>search SECRET_COOKIE</script><input type="hidden" value="SECRET_TOKEN"><div role="button" aria-label="Rechercher dans ce groupe"></div><a href="/groups/123/search/?q=terrain&amp;token=SECRET_TOKEN">Rechercher</a>')
    output = json.dumps(parser.controls)
    assert 'SECRET' not in output
    assert 'Rechercher dans ce groupe' in output
    assert 'terrain' in output
