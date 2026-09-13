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
    page = SimpleNamespace(get_by_role=lambda *a, **kw: SimpleNamespace(count=AsyncMock(return_value=0)))
    assert await gs.select_recent(page) is False
