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
@pytest.mark.parametrize('role,works', [('checkbox', True), ('switch', True), ('radio', True), ('button', True), ('tab', True), ('tab', False), ('checkbox', False)])
async def test_sort_requires_confirmed_selection(monkeypatch, role, works):
    import playwright.async_api
    state = {'checked': False}
    async def click(**kwargs):
        state['checked'] = works
    control = SimpleNamespace(is_visible=AsyncMock(return_value=True), is_checked=AsyncMock(side_effect=lambda: state['checked']),
                              get_attribute=AsyncMock(side_effect=lambda _: str(state['checked']).lower()), click=AsyncMock(side_effect=click),
                              scroll_into_view_if_needed=AsyncMock())
    page = SimpleNamespace(get_by_role=lambda r, **kw: SimpleNamespace(count=AsyncMock(return_value=int(r==role)), nth=lambda i: control))
    async def verify(**kwargs):
        assert state['checked'], 'tri non confirmé'
    async def verify_attr(*args, **kwargs):
        if role == 'tab':
            assert args == ('aria-selected', 'true')
        await verify()
    monkeypatch.setattr(playwright.async_api, 'expect', lambda _: SimpleNamespace(to_be_checked=verify, to_have_attribute=verify_attr))
    if works:
        assert await gs.select_recent(page)
    else:
        with pytest.raises(AssertionError, match='tri non confirmé'):
            await gs.select_recent(page)
    control.click.assert_awaited_once()
    if role == 'tab':
        control.scroll_into_view_if_needed.assert_awaited_once()


@pytest.mark.asyncio
async def test_activity_recent_is_not_publication_sort():
    assert not gs.RECENT.fullmatch('Activité récente')
    page = SimpleNamespace(get_by_role=lambda *a, **kw: SimpleNamespace(count=AsyncMock(return_value=0)), get_by_text=lambda *a, **kw: SimpleNamespace(count=AsyncMock(return_value=0)))
    assert await gs.select_recent(page) is False


def test_exact_label_from_user_screenshot():
    assert gs.RECENT.fullmatch('Plus récent')
    assert gs.RECENT.fullmatch('Plus récentes')
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
async def test_configure_always_uses_group_button_before_sort(monkeypatch):
    page = SimpleNamespace(url='https://m.facebook.com/groups/123/', goto=AsyncMock(),
                           get_by_text=lambda *a, **k: SimpleNamespace(first=SimpleNamespace(wait_for=AsyncMock())))
    group = SimpleNamespace(url=page.url, nom='Groupe')
    scope = AsyncMock()
    fallback = AsyncMock(return_value=True)
    monkeypatch.setattr(gs, 'assert_search_scope', scope)
    monkeypatch.setattr(gs, 'search_via_group_button', fallback)
    monkeypatch.setattr(gs, 'select_recent', AsyncMock(return_value=True))
    monkeypatch.setattr(scraper, 'detecter_blocage_ou_session_expiree', AsyncMock())
    await gs.configure_search(page, group, 'terrain')
    fallback.assert_awaited_once_with(page, group, 'terrain')
    page.goto.assert_not_awaited()  # aucune navigation directe /groups/id/search/
    scope.assert_awaited_once_with(page, group, 'terrain', submitted_from_group=True)


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

@pytest.mark.asyncio
@pytest.mark.parametrize('visibility,clicked', [([True], True), ([False,True],True), ([True,True],False), ([],False)])
async def test_weblite_unique_search_button(visibility, clicked):
    buttons = [SimpleNamespace(is_visible=AsyncMock(return_value=v), click=AsyncMock()) for v in visibility]
    candidates = SimpleNamespace(count=AsyncMock(return_value=len(buttons)), nth=lambda i: buttons[i])
    assert await gs.click_unique_visible(candidates) is clicked
    assert sum(b.click.await_count for b in buttons) == int(clicked)


@pytest.mark.asyncio
async def test_root_group_route_requires_rendered_search_evidence():
    title = SimpleNamespace(count=AsyncMock(return_value=1), nth=lambda _: SimpleNamespace(is_visible=AsyncMock(return_value=True)))
    field = SimpleNamespace(is_visible=AsyncMock(return_value=True), input_value=AsyncMock(return_value='terrain'))
    inputs = SimpleNamespace(count=AsyncMock(return_value=1), nth=lambda _: field)
    page = SimpleNamespace(url='https://m.facebook.com/groups/123/?view=search',
                           get_by_text=lambda *a, **k: title,
                           locator=lambda selector: SimpleNamespace(inner_text=AsyncMock(return_value='Résultats de recherche dans Groupe 123')) if selector=='body' else inputs)
    group = SimpleNamespace(url='https://m.facebook.com/groups/123/', nom='Groupe 123')
    await gs.assert_search_scope(page, group, 'terrain')
    field.input_value.return_value = 'parcelle'
    with pytest.raises(ValueError, match='Mot recherché'):
        await gs.assert_search_scope(page, group, 'terrain')

@pytest.mark.asyncio
async def test_loupe_without_main_or_dialog_fills_then_checks_group(monkeypatch):
    class Empty:
        async def count(self): return 0
        def or_(self, other): return self
        def get_by_role(self, *a, **k): return self
        def locator(self, *a, **k): return self
    empty = Empty()
    button = SimpleNamespace(is_visible=AsyncMock(return_value=True), click=AsyncMock())
    buttons = SimpleNamespace(count=AsyncMock(return_value=1), nth=lambda _: button)
    field = SimpleNamespace(wait_for=AsyncMock(), fill=AsyncMock(), press=AsyncMock())
    fields = SimpleNamespace(count=AsyncMock(return_value=1), first=field)
    def roles(role, **kwargs):
        pattern = kwargs.get('name')
        return buttons if role=='button' and pattern and pattern.fullmatch('Rechercher') else empty
    page = SimpleNamespace(url='https://m.facebook.com/groups/123/', goto=AsyncMock(), get_by_role=roles,
                           locator=lambda _: fields, get_by_placeholder=lambda *a, **k: empty,
                           get_by_text=lambda *a, **k: SimpleNamespace(count=AsyncMock(return_value=1), nth=lambda _: SimpleNamespace(is_visible=AsyncMock(return_value=True))))
    group = SimpleNamespace(url=page.url, nom='Groupe')
    scope = AsyncMock()
    monkeypatch.setattr(gs, 'assert_search_scope', scope)
    monkeypatch.setattr(scraper, 'detecter_blocage_ou_session_expiree', AsyncMock())
    await gs.search_via_group_button(page, group, 'terrain')
    button.click.assert_awaited_once()
    field.fill.assert_awaited_once_with('terrain')
    field.press.assert_awaited_once_with('Enter')
    scope.assert_awaited_once_with(page, group, 'terrain', submitted_from_group=False)


@pytest.mark.asyncio
async def test_explicit_mobile_submit_instead_of_enter():
    button = SimpleNamespace(is_visible=AsyncMock(return_value=True), click=AsyncMock())
    page = SimpleNamespace(get_by_role=lambda *a, **k: SimpleNamespace(count=AsyncMock(return_value=1), nth=lambda _: button))
    field = SimpleNamespace(press=AsyncMock())
    await gs.submit_search(page, field)
    button.click.assert_awaited_once()
    field.press.assert_not_awaited()


@pytest.mark.asyncio
async def test_recent_filter_without_desktop_heading_confirms_results():
    def labels(pattern, **kwargs):
        assert pattern.fullmatch('Plus récent')
        assert pattern.fullmatch('Plus récentes')
        return SimpleNamespace(count=AsyncMock(return_value=1), nth=lambda _: SimpleNamespace(is_visible=AsyncMock(return_value=True)))
    await gs.wait_search_results(SimpleNamespace(get_by_text=labels), timeout=0)


@pytest.mark.asyncio
async def test_search_form_alone_does_not_confirm_results():
    page = SimpleNamespace(get_by_text=lambda *a, **k: SimpleNamespace(count=AsyncMock(return_value=0)))
    with pytest.raises(ValueError, match='recherche non confirmée'):
        await gs.wait_search_results(page, timeout=0)


@pytest.mark.parametrize('origin,query,field_value,field_count,accepted', [
    (True, 'terrain', 'terrain', 1, True),
    (False, 'terrain', 'terrain', 1, False),
    (True, 'parcelle', 'terrain', 1, False),
    (True, 'terrain', 'parcelle', 1, False),
    (True, 'terrain', 'terrain', 0, False),
    (True, 'terrain', 'terrain', 2, False),
])
async def test_mobile_search_results_requires_group_origin_and_query(origin, query, field_value, field_count, accepted):
    field = SimpleNamespace(is_visible=AsyncMock(return_value=True), input_value=AsyncMock(return_value=field_value))
    fields = SimpleNamespace(count=AsyncMock(return_value=field_count), nth=lambda _: field)
    def labels(pattern, **kwargs):
        # The mobile screenshot has no desktop search heading.
        return SimpleNamespace(count=AsyncMock(return_value=int(bool(pattern.fullmatch('Dans le groupe')))),
                               nth=lambda _: SimpleNamespace(is_visible=AsyncMock(return_value=True)))
    page = SimpleNamespace(url=f'https://www.facebook.com/search_results/?q={query}',
                           get_by_placeholder=lambda *a, **k: fields, get_by_text=labels)
    group = SimpleNamespace(url='https://m.facebook.com/groups/123/', nom='Nom configuré différent du titre mobile')
    if accepted:
        await gs.assert_search_scope(page, group, 'terrain', submitted_from_group=origin)
    else:
        with pytest.raises(ValueError):
            await gs.assert_search_scope(page, group, 'terrain', submitted_from_group=origin)


async def test_stateless_recent_button_is_not_accepted_as_sorted():
    empty = SimpleNamespace(count=AsyncMock(return_value=0))
    control = SimpleNamespace(is_visible=AsyncMock(return_value=True), get_attribute=AsyncMock(return_value=None),
                              click=AsyncMock(), scroll_into_view_if_needed=AsyncMock())
    buttons = SimpleNamespace(count=AsyncMock(return_value=1), nth=lambda _: control)
    page = SimpleNamespace(get_by_role=lambda role, **k: buttons if role == 'button' else empty,
                           get_by_text=lambda *a, **k: empty)
    assert not await gs.select_recent(page)
    control.click.assert_awaited_once()  # ouvrir un menu une fois, sans basculer en boucle


def test_diagnostic_includes_mobile_filter_tabs_and_selection():
    from scripts.diagnostic_recherche import Diagnostic
    diagnostic = Diagnostic()
    diagnostic.feed('<div role="tab" aria-selected="false">Plus récentes</div>'
                    '<div role="tab" aria-selected="true">Dans le groupe</div>'
                    '<button aria-pressed="false">Plus récentes</button>')
    assert diagnostic.controls[0] == {'tag': 'div', 'role': 'tab', 'aria-selected': 'false', 'texte': 'Plus récentes'}
    assert diagnostic.controls[1]['aria-selected'] == 'true'
    assert diagnostic.controls[2]['aria-pressed'] == 'false'


async def test_complete_mobile_navigation_and_recent_selection(monkeypatch):
    import playwright.async_api
    empty = SimpleNamespace(count=AsyncMock(return_value=0), get_by_role=lambda *a, **k: empty)
    def collection(control):
        return SimpleNamespace(count=AsyncMock(return_value=1), nth=lambda _: control, first=control)
    state = {'value': '', 'selected': False}
    group = SimpleNamespace(url='https://m.facebook.com/groups/123/', nom='Groupe')
    page = SimpleNamespace(url=group.url)
    async def goto(url, **kwargs):
        assert url == 'https://www.facebook.com/groups/123/'
        page.url = 'https://www.facebook.com/groups/123/'
    async def fill(value):
        state['value'] = value
    async def submit(**kwargs):
        page.url = 'https://www.facebook.com/search_results/?q=' + state['value']
    async def select(**kwargs):
        state['selected'] = True
    field = SimpleNamespace(fill=AsyncMock(side_effect=fill), is_visible=AsyncMock(return_value=True),
                            input_value=AsyncMock(side_effect=lambda: state['value']))
    search = SimpleNamespace(is_visible=AsyncMock(return_value=True), click=AsyncMock())
    send = SimpleNamespace(is_visible=AsyncMock(return_value=True), click=AsyncMock(side_effect=submit))
    recent = SimpleNamespace(is_visible=AsyncMock(return_value=True), wait_for=AsyncMock(),
                             scroll_into_view_if_needed=AsyncMock(), click=AsyncMock(side_effect=select),
                             get_attribute=AsyncMock(side_effect=lambda name: str(state['selected']).lower() if name == 'aria-selected' else None))
    def roles(role, **kwargs):
        pattern = kwargs.get('name')
        for expected_role, name, control in [('button', 'Rechercher', search), ('button', 'Envoyer la recherche', send), ('tab', 'Plus récentes', recent)]:
            if role == expected_role and pattern and pattern.fullmatch(name):
                return collection(control)
        return empty
    def texts(pattern, **kwargs):
        if '/search_results/' in page.url and pattern.fullmatch('Plus récentes'):
            return collection(recent)
        return empty
    page.goto = AsyncMock(side_effect=goto)
    page.get_by_role = roles
    page.get_by_text = texts
    page.get_by_placeholder = lambda *a, **k: collection(field)
    monkeypatch.setattr(scraper, 'detecter_blocage_ou_session_expiree', AsyncMock())
    async def verify_attribute(name, value, **kwargs):
        assert name == 'aria-selected' and value == 'true' and state['selected']
    monkeypatch.setattr(playwright.async_api, 'expect', lambda _: SimpleNamespace(to_have_attribute=verify_attribute))
    # Keep the real scope checks, loupe submission and sorting functions together.
    await gs.configure_search(page, group, 'terrain')
    assert page.url == 'https://www.facebook.com/search_results/?q=terrain'
    search.click.assert_awaited_once()
    send.click.assert_awaited_once()
    recent.scroll_into_view_if_needed.assert_awaited_once()
    recent.click.assert_awaited_once()


@pytest.mark.parametrize('has_results', [False, True])
async def test_expected_search_url_alone_cannot_confirm_results(monkeypatch, has_results):
    # Régression réelle : adresse /groups/id/search/?q=terrain, mais fil général affiché.
    page = SimpleNamespace(url='https://m.facebook.com/groups/123/search/?q=terrain')
    group = SimpleNamespace(url='https://m.facebook.com/groups/123/', nom='Groupe')
    markers = AsyncMock(side_effect=None if has_results else ValueError('recherche non confirmée'))
    monkeypatch.setattr(gs, 'wait_search_results', markers)
    if has_results:
        await gs.assert_search_scope(page, group, 'terrain')
    else:
        with pytest.raises(ValueError, match='recherche non confirmée'):
            await gs.assert_search_scope(page, group, 'terrain')
    markers.assert_awaited_once_with(page, timeout=10)


class DesktopControls:
    def __init__(self, *controls):
        self.controls = list(controls)

    async def count(self):
        return len(self.controls)

    def nth(self, index):
        return self.controls[index]

    def or_(self, other):
        return DesktopControls(*self.controls, *other.controls)


@pytest.mark.parametrize('group_button_present', [True, False])
async def test_desktop_loupe_is_scoped_to_group_tabs(group_button_present):
    import re
    global_search = SimpleNamespace(is_visible=AsyncMock(return_value=True), click=AsyncMock())
    group_search = SimpleNamespace(is_visible=AsyncMock(return_value=True), click=AsyncMock())
    boundary = SimpleNamespace(evaluate=AsyncMock(return_value=True))
    row = SimpleNamespace(evaluate=AsyncMock(return_value=False),
                          get_by_role=lambda *a, **k: DesktopControls(group_search) if group_button_present else DesktopControls(),
                          locator=lambda _: boundary)
    discussion = SimpleNamespace(is_visible=AsyncMock(return_value=True), locator=lambda _: row)
    def roles(role, **kwargs):
        pattern = kwargs.get('name')
        if role == 'tab' and pattern.fullmatch('Discussion'):
            return DesktopControls(discussion)
        if role == 'button' and pattern.fullmatch('Rechercher'):
            return DesktopControls(global_search)  # disponible, mais interdit hors des onglets
        return DesktopControls()
    page = SimpleNamespace(get_by_role=roles)
    names = re.compile(r'^Rechercher dans ce groupe$')
    if group_button_present:
        await gs.open_desktop_group_search(page, names)
        group_search.click.assert_awaited_once()
    else:
        with pytest.raises(ValueError, match='Loupe près des onglets'):
            await gs.open_desktop_group_search(page, names)
    global_search.click.assert_not_awaited()


@pytest.mark.parametrize('group_field_present', [True, False])
async def test_desktop_field_never_uses_facebook_global_search(group_field_present):
    import re
    def field(label):
        return SimpleNamespace(is_visible=AsyncMock(return_value=True),
                               get_attribute=AsyncMock(side_effect=lambda attr: label if attr == 'placeholder' else None),
                               fill=AsyncMock())
    global_field = field('Rechercher sur Facebook')
    group_field = field('Rechercher')  # libellé générique, mais dans le dialogue de la loupe
    dialog_fields = DesktopControls(global_field, *([group_field] if group_field_present else []))
    page = SimpleNamespace(get_by_role=lambda role, **k: SimpleNamespace(locator=lambda _: dialog_fields) if role == 'dialog' else DesktopControls(),
                           get_by_placeholder=lambda *a, **k: DesktopControls())
    if group_field_present:
        selected = await gs.desktop_group_search_field(page, re.compile('groupe'), timeout=0)
        assert selected is group_field
    else:
        with pytest.raises(ValueError, match='Champ de recherche du groupe absent'):
            await gs.desktop_group_search_field(page, re.compile('groupe'), timeout=0)
    global_field.fill.assert_not_awaited()


async def test_desktop_search_submits_in_group_field_and_rejects_global_results(monkeypatch):
    import config
    monkeypatch.setenv('OUAGA_BROWSER_MODE', 'desktop')
    page = SimpleNamespace(url='https://www.facebook.com/groups/123/', goto=AsyncMock())
    group = SimpleNamespace(url=page.url, nom='Groupe')
    field = SimpleNamespace(fill=AsyncMock(), press=AsyncMock())
    async def submit(*args):
        page.url = 'https://www.facebook.com/search/top/?q=terrain'
    field.press.side_effect = submit
    monkeypatch.setattr(gs, 'open_desktop_group_search', AsyncMock())
    monkeypatch.setattr(gs, 'desktop_group_search_field', AsyncMock(return_value=field))
    monkeypatch.setattr(gs, 'wait_search_results', AsyncMock())
    monkeypatch.setattr(scraper, 'detecter_blocage_ou_session_expiree', AsyncMock())
    with pytest.raises(ValueError, match='hors du groupe'):
        await gs.search_via_group_button(page, group, 'terrain')
    field.fill.assert_awaited_once_with('terrain')
    field.press.assert_awaited_once_with('Enter')
