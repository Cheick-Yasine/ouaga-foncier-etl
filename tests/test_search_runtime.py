import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import Error as PlaywrightError

import config
import scraper
import group_search
from search_runtime import graphql_payloads, lire_apres_navigation, statistiques_dates


def test_graphql_single_object_is_not_counted_twice():
    assert list(graphql_payloads('for (;;);{"data": {"id": "1"}}')) == [{'data': {'id': '1'}}]
    assert list(graphql_payloads('{"id":1}\n{"id":2}')) == [{'id': 1}, {'id': 2}]


async def test_navigation_race_retries_read_then_detects_expired_session(monkeypatch):
    page = SimpleNamespace(url='https://www.facebook.com/groups/1/search/?q=terrain',
        content=AsyncMock(side_effect=[PlaywrightError('Execution context was destroyed'), '{"USER_ID":"0"}']),
        wait_for_load_state=AsyncMock())
    monkeypatch.setattr('search_runtime.asyncio.sleep', AsyncMock())
    with pytest.raises(scraper.SessionExpireeError):
        await scraper.detecter_blocage_ou_session_expiree(page)
    page.wait_for_load_state.assert_awaited_once()


async def test_navigation_race_on_login_locator_is_retried(monkeypatch):
    count = AsyncMock(side_effect=[PlaywrightError('Execution context was destroyed'), 0])
    page = SimpleNamespace(url='https://www.facebook.com/groups/1/', content=AsyncMock(return_value='page connectée'),
                           locator=lambda _: SimpleNamespace(count=count), wait_for_load_state=AsyncMock())
    monkeypatch.setattr('search_runtime.asyncio.sleep', AsyncMock())
    await scraper.detecter_blocage_ou_session_expiree(page)
    assert count.await_count == 2


async def test_navigation_retry_is_bounded_and_does_not_swallow_other_errors(monkeypatch):
    page = SimpleNamespace(wait_for_load_state=AsyncMock())
    monkeypatch.setattr('search_runtime.asyncio.sleep', AsyncMock())
    operation = AsyncMock(side_effect=PlaywrightError('Execution context was destroyed'))
    with pytest.raises(PlaywrightError):
        await lire_apres_navigation(page, operation)
    assert operation.await_count == 3
    operation = AsyncMock(side_effect=PlaywrightError('Target page has been closed'))
    with pytest.raises(PlaywrightError):
        await lire_apres_navigation(page, operation)
    operation.assert_awaited_once()


def test_date_report_distinguishes_retained_and_exportable_posts():
    now = datetime.now(timezone.utc)
    posts = [{'id': 'old', 'date_publication': (now - timedelta(days=20)).isoformat()},
             {'id': 'new', 'date_publication': now.isoformat()}, {'id': 'unknown', 'date_publication': None}]
    report = statistiques_dates(posts + posts, now - timedelta(days=5))
    assert report['dans_fenetre'] == report['hors_fenetre'] == report['dates_incertaines'] == 1


async def test_stagnation_saves_partial_query_then_tries_parcelle(monkeypatch):
    stamp = datetime.now(timezone.utc).isoformat()
    post = {'id': '1', 'date_publication': stamp, 'scrape_le': stamp}
    calls = []
    async def collect(*args, **kwargs):
        calls.append(kwargs['recherche'])
        if kwargs['recherche'] == 'terrain':
            raise scraper.RechercheIncompleteError('stagnation', [post])
        return [], None
    monkeypatch.setattr(scraper, 'scraper_groupe', collect)
    save = Mock(return_value='raw-partial.json')
    monkeypatch.setattr(scraper, 'sauvegarder_posts_groupe', save)
    files = []
    with pytest.raises(scraper.RechercheIncompleteError, match='terrain'):
        await scraper.scraper_recherches_groupe(None, SimpleNamespace(id='123', nom='Groupe'), 5, {},
                                               rattrapage=True, fichiers_sauvegardes=files)
    assert calls == ['terrain', 'parcelle'] and files == ['raw-partial.json']
    save.assert_called_once_with([post], '123')


async def test_capture_listener_precedes_filter_and_keeps_late_response(monkeypatch, tmp_path):
    """Traverse le vrai scraper : premier lot au filtre, second entre deux scrolls."""
    monkeypatch.setenv('OUAGA_BROWSER_MODE', 'mobile')
    monkeypatch.setattr(config, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(config, 'MAX_PAGES_SANS_NOUVEAU_POST', 1)
    handlers, sleepers = {}, {'n': 0}
    def on(event, callback):
        handlers[event] = callback
    page = SimpleNamespace(on=on, remove_listener=Mock(), close=AsyncMock(), content=AsyncMock(return_value=''),
                           screenshot=AsyncMock())
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    def response(id):
        return SimpleNamespace(url='https://www.facebook.com/api/graphql/', status=200,
                               text=AsyncMock(return_value=json.dumps({'post': id})))
    original_sleep = asyncio.sleep
    async def configure(page, group, term, *, on_results_ready):
        assert 'response' not in handlers  # le fil général n'a pas été écouté
        on_results_ready()
        handlers['response'](response('initial'))
    async def sleep(_):
        sleepers['n'] += 1
        if sleepers['n'] == 2:
            handlers['response'](response('late'))
        await original_sleep(0)
    stamp = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(group_search, 'configure_search', configure)
    monkeypatch.setattr(scraper, 'extraire_stories_depuis_json', lambda payload, *a: [
        {'id': payload['post'], 'texte': 'terrain', 'date_publication': stamp, 'scrape_le': stamp}])
    monkeypatch.setattr(scraper, '_extraire_stories_depuis_scripts_json', lambda *a: [])
    monkeypatch.setattr(scraper, '_extraire_posts_weblite_dom', AsyncMock(return_value=[]))
    monkeypatch.setattr(scraper, '_sauvegarder_html_debug', AsyncMock())
    monkeypatch.setattr(scraper, 'detecter_blocage_ou_session_expiree', AsyncMock())
    monkeypatch.setattr(scraper, '_scroll_humain', AsyncMock())
    monkeypatch.setattr(scraper.asyncio, 'sleep', sleep)
    with pytest.raises(scraper.RechercheIncompleteError) as error:
        await scraper.scraper_groupe(context, SimpleNamespace(id='1', nom='Groupe', url='https://m.facebook.com/groups/1/'),
                                     5, {}, recherche='terrain', rattrapage=True)
    assert {p['id'] for p in error.value.posts} == {'initial', 'late'}
    page.remove_listener.assert_called_once()
    page.close.assert_awaited_once()


@pytest.mark.parametrize('nested', [True, False])
async def test_real_firefox_scrolls_results_and_loads_next_cards(nested):
    """Page locale : vrai DOM, défilement et chargement paresseux, sans Facebook."""
    from pathlib import Path
    import os
    from playwright.async_api import async_playwright
    from search_runtime import defiler_resultats
    if os.environ.get('OUAGA_TEST_BROWSER') != '1':
        pytest.skip('Test navigateur local optionnel : OUAGA_TEST_BROWSER=1')
    async with async_playwright() as playwright:
        if not Path(playwright.firefox.executable_path).exists():
            pytest.skip('Firefox Playwright non installé')
        browser = await playwright.firefox.launch(headless=True, timeout=15000)
        try:
            page = await asyncio.wait_for(browser.new_page(viewport={'width': 1000, 'height': 400}), timeout=15)
            style = 'height:300px;overflow-y:auto;' if nested else ''
            body = 'overflow:hidden' if nested else ''
            await page.set_content(f'''<style>body{{margin:0;{body}}} article{{height:180px}}
                nav{{position:fixed;width:200px;height:200px;overflow:auto}}</style>
                <nav role="navigation"><div style="height:2000px">Filtres</div></nav>
                <main role="main" style="margin-left:220px;{style}">
                <article role="article">A</article><article role="article">B</article><article role="article">C</article></main>''')
            await page.evaluate('''nested => {
                const main = document.querySelector('main');
                const target = nested ? main : document.scrollingElement;
                (nested ? main : document).addEventListener('scroll', () => {
                    if (target.scrollTop + target.clientHeight >= target.scrollHeight - 5 && main.children.length < 8) {
                        const article = document.createElement('article');
                        article.setAttribute('role', 'article'); article.textContent='Chargé ensuite'; main.append(article);
                    }
                });
            }''', nested)
            report = await defiler_resultats(page)
            assert report['deplacement'] > 0
            assert report['apres']['cible'] == ('conteneur_resultats' if nested else 'document')
            assert await page.locator('article').count() > 3
            assert await page.locator('nav').evaluate('el => el.scrollTop') == 0
            if nested:
                assert await page.evaluate('document.scrollingElement.scrollTop') == 0
        finally:
            await asyncio.wait_for(browser.close(), timeout=5)
