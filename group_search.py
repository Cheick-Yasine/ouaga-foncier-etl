"""Recherche de groupe : tri confirmé par l'état visible, sans filtre URL inventé."""
import re
import logging
import asyncio
import time
from urllib.parse import urlencode, urlparse, parse_qs

TERMS = ('terrain', 'parcelle')
RECENT = re.compile(r'^(Plus récent(?:e)?s?|Publications (?:les plus )?récentes|Les plus récentes|Most recent(?: posts)?|Recent posts|Newest posts)$', re.I)
RESULTS = re.compile(r'^(?:Résultats de recherche|Search results|Dans le groupe|In (?:the|this) group)$|' + RECENT.pattern, re.I)


def search_url(group_url, term):
    parsed = urlparse(group_url)
    match = re.fullmatch(r'/groups/([^/]+)/?', parsed.path)
    if not match:
        raise ValueError('Cette cible est une Page, pas un groupe : recherche de groupe indisponible.')
    return f'{parsed.scheme}://{parsed.netloc}/groups/{match[1]}/search/?{urlencode({"q": term})}'


def assert_search_url(url, group_url, term):
    expected = urlparse(search_url(group_url, term))
    current = urlparse(url)
    if current.path.rstrip('/') != expected.path.rstrip('/') or parse_qs(current.query).get('q') != [term]:
        raise ValueError('Facebook a quitté la recherche demandée ; collecte interrompue.')


async def select_recent(page, *, open_menu=True):
    """Clique un contrôle sémantique et exige une preuve de sélection.

    Pas de repli sur « Activité récente », qui peut classer par commentaires.
    Une variante d'interface inconnue doit être inspectée, jamais acceptée à tort.
    """
    from playwright.async_api import expect
    menu_buttons = []
    for role in ('checkbox', 'switch', 'radio', 'menuitemradio', 'tab', 'button'):
        controls = page.get_by_role(role, name=RECENT)
        for i in range(await controls.count()):
            control = controls.nth(i)
            if not await control.is_visible():
                continue
            attribute = {'button': 'aria-pressed', 'tab': 'aria-selected'}.get(role, 'aria-checked')
            if role in ('button', 'tab') and await control.get_attribute(attribute) is None:
                if role == 'button':
                    menu_buttons.append(control)
                continue  # un clic sans état de sélection n'est pas une preuve de tri
            if role == 'tab':
                # La barre de filtres WebLite défile horizontalement.
                await control.scroll_into_view_if_needed(timeout=5000)
            if role in ('checkbox', 'radio'):
                if not await control.is_checked():
                    await control.click(timeout=5000)
                await expect(control).to_be_checked(timeout=5000)
            else:
                if await control.get_attribute(attribute) != 'true':
                    await control.click(timeout=5000)
                await expect(control).to_have_attribute(attribute, 'true', timeout=5000)
            logging.getLogger('ouaga_foncier_etl.group_search').info('Tri par publications récentes confirmé (%s).', role)
            return True
    # Le libellé et le switch peuvent être des éléments frères (capture utilisateur).
    labels = page.get_by_text(RECENT, exact=True)
    for i in range(await labels.count()):
        row = labels.nth(i)
        for _ in range(3):
            row = row.locator('..')
            switches = row.locator('[role="switch"], [role="checkbox"], input[type="checkbox"]')
            if await switches.count() != 1:
                continue
            control = switches.first
            if not await control.is_visible():
                continue
            if await control.get_attribute('type') == 'checkbox':
                if not await control.is_checked():
                    await control.click(timeout=5000)
                await expect(control).to_be_checked(timeout=5000)
            else:
                if await control.get_attribute('aria-checked') != 'true':
                    await control.click(timeout=5000)
                await expect(control).to_have_attribute('aria-checked', 'true', timeout=5000)
            return True
    if open_menu and len(menu_buttons) == 1:
        await menu_buttons[0].scroll_into_view_if_needed(timeout=5000)
        await menu_buttons[0].click(timeout=5000)
        # Certains boutons ouvrent un choix : exiger ensuite son état sélectionné.
        return await select_recent(page, open_menu=False)
    return False


async def click_unique_visible(candidates):
    visible = [candidates.nth(i) for i in range(await candidates.count())
               if await candidates.nth(i).is_visible()]
    if len(visible) != 1:
        return False
    await visible[0].click(timeout=5000)
    return True


def result_title(page):
    # WebLite affiche parfois un simple div, sans rôle heading.
    return page.get_by_text(re.compile(r'^(Résultats de recherche|Search results)$', re.I), exact=True)


async def wait_search_results(page, timeout=15):
    """Le panneau récent mobile remplace le titre bureau comme preuve de résultats."""
    markers = page.get_by_text(RESULTS, exact=True)
    deadline = time.monotonic() + timeout
    while True:
        for i in range(await markers.count()):
            if await markers.nth(i).is_visible():
                return
        if time.monotonic() >= deadline:
            raise ValueError('Après envoi, ni résultats ni filtre récent visibles : recherche non confirmée.')
        await asyncio.sleep(0.25)


async def submit_search(page, field):
    buttons = page.get_by_role('button', name=re.compile(r'^(Envoyer la recherche|Submit search)$', re.I))
    if await click_unique_visible(buttons):
        logging.getLogger("ouaga_foncier_etl.group_search").info('Clic sur « Envoyer la recherche » effectué.')
    elif await buttons.count():
        raise ValueError('Bouton Envoyer la recherche ambigu ou invisible.')
    else:
        await field.press('Enter')
        logging.getLogger("ouaga_foncier_etl.group_search").info('Recherche envoyée par Entrée (aucun bouton explicite).')


async def assert_search_scope(page, group, term, *, submitted_from_group=False):
    from scraper import _verifier_domaine_facebook
    _verifier_domaine_facebook(page.url)
    try:
        assert_search_url(page.url, group.url, term)
    except ValueError:
        pass
    else:
        # Facebook peut conserver cette adresse tout en affichant le fil du
        # groupe. Une URL attendue ne remplace pas les résultats rendus.
        await wait_search_results(page, timeout=10)
        return
    # Variante d'URL : exiger les preuves rendues de recherche ET de groupe ET de mot.
    parsed = urlparse(page.url)
    if parsed.path.rstrip('/') == '/search_results':
        # WebLite omet l'identifiant du groupe dans cette route. L'URL seule ne
        # suffit pas : il faut avoir ouvert le groupe exact et utilisé son champ.
        if not submitted_from_group or parse_qs(parsed.query).get('q') != [term]:
            raise ValueError('Origine de la recherche mobile non confirmée pour ce groupe.')
        fields = page.get_by_placeholder('Rechercher dans ce groupe', exact=True)
        visible = [fields.nth(i) for i in range(await fields.count())
                   if await fields.nth(i).is_visible()]
        if len(visible) != 1 or (await visible[0].input_value()).strip().casefold() != term.casefold():
            raise ValueError('Champ et mot de la recherche mobile non confirmés.')
        await wait_search_results(page, timeout=10)
        return
    group_path = urlparse(group.url).path.rstrip('/')
    if parsed.path.rstrip('/') != group_path and not parsed.path.startswith(group_path + '/'):
        raise ValueError('Recherche hors du groupe attendu ; collecte interrompue.')
    await wait_search_results(page, timeout=10)
    normalize = lambda value: ' '.join(value.split()).casefold()
    if normalize(group.nom) not in normalize(await page.locator('body').inner_text()):
        raise ValueError('Nom du groupe absent des résultats de recherche.')
    inputs = page.locator('input')
    for i in range(await inputs.count()):
        field = inputs.nth(i)
        if await field.is_visible() and (await field.input_value()).strip().casefold() == term.casefold():
            return
    raise ValueError('Mot recherché non confirmé dans la page de résultats.')


async def open_desktop_group_search(page, names):
    """Loupe nommée du groupe ou bouton situé près de ses onglets uniquement."""
    for role in ('button', 'link'):
        if await click_unique_visible(page.get_by_role(role, name=names)):
            return
    anchors = page.get_by_role('tab', name=re.compile(r'^Discussions?$', re.I)).or_(
        page.get_by_role('link', name=re.compile(r'^Discussions?$', re.I)))
    for i in range(await anchors.count()):
        row = anchors.nth(i)
        if not await row.is_visible():
            continue
        for _ in range(6):
            row = row.locator('..')
            # Ne jamais élargir jusqu'à la barre globale Facebook ou tout le document.
            if await row.evaluate("el => ['BODY', 'HTML'].includes(el.tagName) || el.getAttribute('role') === 'banner' || !!el.closest('[role=banner]') || !!el.querySelector('[role=banner]')"):
                break
            buttons = row.get_by_role('button', name=re.compile(r'^(Rechercher|Search)$', re.I))
            if await click_unique_visible(buttons):
                return
    raise ValueError('Loupe près des onglets du groupe non identifiée. La recherche générale Facebook ne sera pas utilisée.')


async def desktop_group_search_field(page, names, timeout=10):
    """Champ nommé du groupe ou champ du dialogue ouvert par sa loupe."""
    named = page.get_by_role('searchbox', name=names).or_(page.get_by_role('textbox', name=names)).or_(
        page.get_by_placeholder(re.compile(r'^(Rechercher dans (?:ce|le) groupe|Search this group)$', re.I)))
    dialog = page.get_by_role('dialog').locator(
        'input:not([type]), input[type="text"], input[type="search"]')
    deadline = time.monotonic() + timeout
    while True:
        for candidates in (named, dialog):
            visible = []
            for i in range(await candidates.count()):
                field = candidates.nth(i)
                if not await field.is_visible():
                    continue
                labels = [(await field.get_attribute(attr) or '').strip().casefold()
                          for attr in ('placeholder', 'aria-label')]
                if any(label in {'rechercher sur facebook', 'search facebook'} for label in labels):
                    continue
                visible.append(field)
            if len(visible) > 1:
                raise ValueError('Plusieurs champs possibles dans la recherche du groupe.')
            if visible:
                return visible[0]
        if time.monotonic() >= deadline:
            raise ValueError('Champ de recherche du groupe absent du dialogue. Aucun texte saisi dans la recherche générale.')
        await asyncio.sleep(0.25)


async def search_via_group_button(page, group, term):
    import config
    from scraper import detecter_blocage_ou_session_expiree, _verifier_domaine_facebook
    parsed = urlparse(group.url)
    await page.goto(f'https://www.facebook.com{parsed.path}', wait_until='domcontentloaded')
    _verifier_domaine_facebook(page.url)
    await detecter_blocage_ou_session_expiree(page)
    if urlparse(page.url).path.rstrip('/') != parsed.path.rstrip('/'):
        raise ValueError('Impossible d’ouvrir le groupe avant la recherche.')
    names = re.compile(r'^(Rechercher dans (?:ce|le) groupe|Rechercher dans le groupe|Search (?:this|in this) group)$', re.I)
    if config.mode_navigateur() == 'desktop':
        await open_desktop_group_search(page, names)
        logging.getLogger('ouaga_foncier_etl.group_search').info('Loupe du groupe ouverte en mode ordinateur ; saisie dans son champ dédié.')
        field = await desktop_group_search_field(page, names)
        await field.fill(term)
        await field.press('Enter')
        await wait_search_results(page)
        await detecter_blocage_ou_session_expiree(page)
        await assert_search_scope(page, group, term, submitted_from_group=True)
        return True
    clicked = False
    for role in ('button', 'link'):
        buttons = page.get_by_role(role, name=names)
        for i in range(await buttons.count()):
            if await buttons.nth(i).is_visible():
                await buttons.nth(i).click(timeout=5000)
                clicked = True
                break
        if clicked:
            break
    if not clicked:
        # Sur l'interface bureau, la loupe du groupe peut s'appeler simplement Rechercher.
        # Limiter au contenu principal exclut la barre globale Facebook.
        candidates = page.get_by_role('main').get_by_role('button', name=re.compile(r'^(Rechercher|Search)$', re.I))
        if await candidates.count() == 1 and await candidates.first.is_visible():
            await candidates.first.click(timeout=5000)
            clicked = True
    if not clicked:
        # Diagnostic réel WebLite : un seul div[role=button][aria-label=Rechercher], sans main.
        candidates = page.get_by_role('button', name=re.compile(r'^(Rechercher|Search)$', re.I))
        clicked = await click_unique_visible(candidates)
        if clicked:
            logging.getLogger("ouaga_foncier_etl.group_search").info('Bouton mobile « Rechercher » ouvert ; validation du groupe exigée après saisie.')
    if not clicked:
        raise ValueError('Loupe du groupe non reconnue : vérifier son libellé accessible dans cette interface.')
    fields = page.get_by_placeholder('Rechercher dans ce groupe', exact=True)
    group_field_confirmed = await fields.count() == 1
    if await fields.count() == 0:
        fields = page.get_by_role('searchbox', name=names).or_(page.get_by_role('textbox', name=names))
    if await fields.count() == 0:
        # Le champ du dialogue de la loupe n’est pas la recherche globale Facebook.
        fields = page.get_by_role('dialog').locator('input:not([type="hidden"])')
    if await fields.count() == 0:
        # WebLite n'expose pas toujours searchbox ou dialog. Ne remplir qu'un champ visible unique.
        fields = page.locator('input[type="search"]:visible, input[type="text"]:visible, input:not([type]):visible')
        await fields.first.wait_for(state='visible', timeout=10000)
    if await fields.count() != 1:
        raise ValueError('Champ de recherche du groupe non identifié sans ambiguïté.')
    await fields.first.fill(term)
    await submit_search(page, fields.first)
    await wait_search_results(page)
    await detecter_blocage_ou_session_expiree(page)
    await assert_search_scope(page, group, term, submitted_from_group=group_field_confirmed)
    return group_field_confirmed


async def configure_search(page, group, term, *, on_results_ready=None):
    from scraper import detecter_blocage_ou_session_expiree
    search_url(group.url, term)  # Valider une cible groupe ; ne pas naviguer vers cette URL.
    logger = logging.getLogger('ouaga_foncier_etl.group_search')
    logger.info('Recherche via la loupe du groupe : ouverture, saisie et envoi de « %s ».', term)
    submitted_from_group = await search_via_group_button(page, group, term)
    if on_results_ready is not None:
        on_results_ready()  # écouter AVANT le chargement déclenché par le filtre
    logger.info('Résultats de recherche confirmés ; sélection du filtre « Plus récentes ».')
    # Attend le panneau rendu ; l'absence de cette interface doit rester explicite.
    try:
        await page.get_by_text(RECENT).first.wait_for(state='visible', timeout=10000)
    except Exception:
        pass
    if not await select_recent(page):
        # Certaines interfaces placent les choix dans un menu « Trier » / « Filtres ».
        from scraper import _cliquer_premier_libelle_visible
        await _cliquer_premier_libelle_visible(page, ('Trier', 'Trier par', 'Sort', 'Sort by', 'Filtres', 'Filters'))
        if not await select_recent(page):
            raise ValueError('Tri « Publications récentes » non confirmé : aucun scroll effectué. Une capture du panneau de filtres est nécessaire.')
    from search_runtime import lire_apres_navigation
    async def validate():
        await detecter_blocage_ou_session_expiree(page)
        await assert_search_scope(page, group, term, submitted_from_group=submitted_from_group)
    from playwright.async_api import Error as PlaywrightError
    from search_runtime import navigation_interrompue
    try:
        await lire_apres_navigation(page, validate)
    except PlaywrightError as exc:
        if navigation_interrompue(exc):
            raise ValueError('Recherche instable après le tri : navigation non terminée après trois vérifications.') from exc
        raise


def matching_post(post, term, group_id):
    text = post.get('texte', post.get('texte_nettoye', '')) or ''
    if not re.search(r'\b' + re.escape(term) + r's?\b', text, re.I):
        return False
    match = re.search(r'/groups/([^/]+)/', post.get('url', '') or '')
    return match is None or match[1] == str(group_id)


def in_window(post, cutoff):
    from datetime import datetime, timezone
    if post.get('date_incertaine') or not post.get('date_publication'):
        return True  # conserver explicitement l'incertitude plutôt qu'inventer une date
    try:
        date = datetime.fromisoformat(post['date_publication'])
        if date.tzinfo is None:
            return True
        return cutoff <= date <= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return True
