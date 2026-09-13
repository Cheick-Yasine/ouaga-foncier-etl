"""Recherche de groupe : tri confirmé par l'état visible, sans filtre URL inventé."""
import re
from urllib.parse import urlencode, urlparse, parse_qs

TERMS = ('terrain', 'parcelle')
RECENT = re.compile(r'^(Publications (?:les plus )?récentes|Les plus récentes|Most recent(?: posts)?|Recent posts|Newest posts)$', re.I)


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
    return False


async def configure_search(page, group, term):
    from scraper import detecter_blocage_ou_session_expiree, _verifier_domaine_facebook
    await page.goto(search_url(group.url, term), wait_until='domcontentloaded')
    _verifier_domaine_facebook(page.url)
    await detecter_blocage_ou_session_expiree(page)
    assert_search_url(page.url, group.url, term)
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
    assert_search_url(page.url, group.url, term)


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
