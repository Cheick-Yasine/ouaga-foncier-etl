"""Recherche de groupe : tri confirmé par l'état visible, sans filtre URL inventé."""
import re
import logging
from urllib.parse import urlencode, urlparse, parse_qs

TERMS = ('terrain', 'parcelle')
RECENT = re.compile(r'^(Plus récent|Publications (?:les plus )?récentes|Les plus récentes|Most recent(?: posts)?|Recent posts|Newest posts)$', re.I)


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


async def select_recent(page):
    """Clique un contrôle sémantique et exige une preuve de sélection.

    Pas de repli sur « Activité récente », qui peut classer par commentaires.
    Une variante d'interface inconnue doit être inspectée, jamais acceptée à tort.
    """
    from playwright.async_api import expect
    for role in ('checkbox', 'switch', 'radio', 'menuitemradio', 'button'):
        controls = page.get_by_role(role, name=RECENT)
        for i in range(await controls.count()):
            control = controls.nth(i)
            if not await control.is_visible():
                continue
            attribute = 'aria-pressed' if role == 'button' else 'aria-checked'
            if role == 'button' and await control.get_attribute(attribute) is None:
                continue  # un simple bouton de menu n'est pas une preuve de tri
            if role in ('checkbox', 'radio'):
                if not await control.is_checked():
                    await control.click(timeout=5000)
                await expect(control).to_be_checked(timeout=5000)
            else:
                if await control.get_attribute(attribute) != 'true':
                    await control.click(timeout=5000)
                await expect(control).to_have_attribute(attribute, 'true', timeout=5000)
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


async def assert_search_scope(page, group, term):
    from scraper import _verifier_domaine_facebook
    _verifier_domaine_facebook(page.url)
    try:
        assert_search_url(page.url, group.url, term)
        return
    except ValueError:
        pass
    # Variante d'URL : exiger les preuves rendues de recherche ET de groupe ET de mot.
    parsed = urlparse(page.url)
    group_path = urlparse(group.url).path.rstrip('/')
    if parsed.path.rstrip('/') != group_path and not parsed.path.startswith(group_path + '/'):
        raise ValueError('Recherche hors du groupe attendu ; collecte interrompue.')
    heading = result_title(page)
    await heading.first.wait_for(state='visible', timeout=10000)
    normalize = lambda value: ' '.join(value.split()).casefold()
    if normalize(group.nom) not in normalize(await page.locator('body').inner_text()):
        raise ValueError('Nom du groupe absent des résultats de recherche.')
    inputs = page.locator('input')
    for i in range(await inputs.count()):
        field = inputs.nth(i)
        if await field.is_visible() and (await field.input_value()).strip().casefold() == term.casefold():
            return
    raise ValueError('Mot recherché non confirmé dans la page de résultats.')


async def search_via_group_button(page, group, term):
    from scraper import detecter_blocage_ou_session_expiree, _verifier_domaine_facebook
    parsed = urlparse(group.url)
    await page.goto(f'https://www.facebook.com{parsed.path}', wait_until='domcontentloaded')
    _verifier_domaine_facebook(page.url)
    await detecter_blocage_ou_session_expiree(page)
    if urlparse(page.url).path.rstrip('/') != parsed.path.rstrip('/'):
        raise ValueError('Impossible d’ouvrir le groupe avant la recherche.')
    names = re.compile(r'^(Rechercher dans (?:ce|le) groupe|Rechercher dans le groupe|Search (?:this|in this) group)$', re.I)
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
            logging.getLogger(__name__).info('Bouton mobile « Rechercher » ouvert ; validation du groupe exigée après saisie.')
    if not clicked:
        raise ValueError('Loupe du groupe non reconnue : vérifier son libellé accessible dans cette interface.')
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
    await fields.first.press('Enter')
    await result_title(page).first.wait_for(state='visible', timeout=15000)
    await detecter_blocage_ou_session_expiree(page)
    await assert_search_scope(page, group, term)


async def configure_search(page, group, term):
    from scraper import detecter_blocage_ou_session_expiree, _verifier_domaine_facebook
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    await page.goto(search_url(group.url, term), wait_until='domcontentloaded')
    _verifier_domaine_facebook(page.url)
    await detecter_blocage_ou_session_expiree(page)
    try:
        await assert_search_scope(page, group, term)
    except (ValueError, PlaywrightTimeoutError):
        logging.getLogger(__name__).info('Recherche directe non confirmée (chemin reçu : %s) ; essai par la loupe du groupe.', urlparse(page.url).path)
        await search_via_group_button(page, group, term)
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
    await detecter_blocage_ou_session_expiree(page)
    await assert_search_scope(page, group, term)


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
