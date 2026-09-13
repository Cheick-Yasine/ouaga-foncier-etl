from scripts.inspecter_recherche import route_sans_secrets


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
