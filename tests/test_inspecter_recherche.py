from scripts.inspecter_recherche import route_sans_secrets
import pytest


def test_route_does_not_expose_query_tokens():
    route = route_sans_secrets('https://m.facebook.com/groups/123/search/?q=terrain&token=SECRET&fb_dtsg=SECRET')
    assert route == {'hote': 'm.facebook.com', 'chemin': '/groups/123/search/', 'q': 'terrain'}
    assert 'SECRET' not in str(route)


async def test_visible_browser_uses_requested_mode(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    import scraper
    import config
    context = SimpleNamespace(add_init_script=AsyncMock(), add_cookies=AsyncMock(), set_default_navigation_timeout=Mock())
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context))
    launch = AsyncMock(return_value=browser)
    monkeypatch.setattr(config, 'choisir_fingerprint_mobile', lambda _: ('test-UA', {'width': 360, 'height': 780}))
    monkeypatch.setattr(config, 'parametres_regionaux', lambda _: SimpleNamespace(locale='fr-FR', fuseau_horaire='Africa/Ouagadougou'))
    monkeypatch.setattr(scraper, '_charger_origins_sauvegardees', lambda _: [])
    await scraper.creer_navigateur(SimpleNamespace(chromium=SimpleNamespace(launch=launch)), [], '1', headless=False)
    assert launch.call_args.kwargs['headless'] is False


@pytest.mark.parametrize('group_id', ['101', '202'])
async def test_inspection_loads_real_csv_for_selected_account(monkeypatch, tmp_path, group_id):
    from functools import partial
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    import config
    import scraper
    import group_search
    import playwright.async_api
    from scripts import inspecter_recherche as diagnostic

    csv = tmp_path / 'groups.csv'
    csv.write_text(
        'id,nom,url,actif,compte\n'
        '101,Groupe un,https://m.facebook.com/groups/101/,1,1\n'
        '202,Groupe deux,https://m.facebook.com/groups/202/,1,2\n',
        encoding='utf-8',
    )
    # Use the real CSV reader and account filtering, replacing only its file path.
    monkeypatch.setattr(config, 'charger_groupes', partial(config.charger_groupes, chemin=csv))
    monkeypatch.setattr(config, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(scraper, 'verifier_cooldown', lambda _: None)
    monkeypatch.setattr(scraper, '_charger_cookies_caches', lambda _: [])
    page = SimpleNamespace(is_closed=Mock(return_value=False))
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), close=AsyncMock())
    browser = SimpleNamespace(close=AsyncMock())
    create_browser = AsyncMock(return_value=(browser, context))
    monkeypatch.setattr(scraper, 'creer_navigateur', create_browser)
    manager = AsyncMock()
    monkeypatch.setattr(playwright.async_api, 'async_playwright', Mock(return_value=manager))
    configure = AsyncMock()
    monkeypatch.setattr(group_search, 'configure_search', configure)
    snapshots = AsyncMock()
    monkeypatch.setattr(diagnostic, 'snapshot', snapshots)
    monkeypatch.setattr('builtins.input', lambda _: '')
    args = SimpleNamespace(compte='1', groupe=group_id, mot='terrain')

    if group_id == '202':
        with pytest.raises(ValueError, match='Groupe absent'):
            await diagnostic.inspect(args)
        create_browser.assert_not_awaited()
        return

    await diagnostic.inspect(args)
    selected_group = configure.await_args.args[1]
    assert (selected_group.id, selected_group.compte) == ('101', '1')
    assert create_browser.await_args.kwargs['headless'] is False
    assert [call.args[2] for call in snapshots.await_args_list] == ['automatique', 'final']
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
