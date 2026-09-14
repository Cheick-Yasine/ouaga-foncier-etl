"""Défilement mesuré et lectures pendant les navigations de la recherche."""
import asyncio
import json
from datetime import datetime, timezone

from playwright.async_api import Error as PlaywrightError


def navigation_interrompue(exc):
    return isinstance(exc, PlaywrightError) and any(message in str(exc).lower() for message in (
        'execution context was destroyed', 'cannot find context with specified id',
        'unable to retrieve content because the page is navigating',
    ))


async def lire_apres_navigation(page, operation, attempts=3):
    """Rejoue uniquement une lecture interrompue, jamais un clic ni un blocage."""
    for attempt in range(attempts):
        try:
            return await operation()
        except PlaywrightError as exc:
            if not navigation_interrompue(exc) or attempt == attempts - 1:
                raise
            await page.wait_for_load_state('domcontentloaded', timeout=15000)
            await asyncio.sleep(0.25)


def graphql_payloads(body):
    body = body.removeprefix('for (;;);').strip()
    try:
        yield json.loads(body)
        return  # ne pas parser une seconde fois le même objet sur sa ligne
    except json.JSONDecodeError:
        pass
    for line in body.splitlines():
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def statistiques_dates(posts, cutoff):
    dates, incertaines = [], 0
    now = datetime.now(timezone.utc)
    for post in {p['id']: p for p in posts}.values():
        try:
            date = datetime.fromisoformat(post.get('date_publication') or '')
            if date.tzinfo is None or post.get('date_incertaine'):
                raise ValueError
        except (ValueError, TypeError):
            incertaines += 1
            continue
        dates.append(date)
    return dict(dans_fenetre=sum(cutoff <= date <= now for date in dates),
                hors_fenetre=sum(date < cutoff or date > now for date in dates),
                dates_incertaines=incertaines,
                date_min=min(dates).isoformat() if dates else None,
                date_max=max(dates).isoformat() if dates else None)


SCROLL_TARGET = r"""() => {
    const main = document.querySelector('[role="main"]') || document.querySelector('[role="feed"]');
    if (!main) return document.scrollingElement;
    const anchor = main.querySelector('[role="article"]') || main.querySelector('[role="feed"]') || main;
    for (let node = anchor; node && node !== document.body; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (node.clientHeight > 100 && node.scrollHeight > node.clientHeight + 2 &&
            /^(auto|scroll)$/.test(style.overflowY) && !node.closest('[role="navigation"], [role="banner"]'))
            return node;
    }
    return document.scrollingElement;
}"""

SCROLL_STATE = r"""el => ({
    cible: el === document.scrollingElement ? 'document' : 'conteneur_resultats',
    position: Math.round(el.scrollTop), hauteur: el.scrollHeight, visible: el.clientHeight,
    bas: el.scrollTop + el.clientHeight >= el.scrollHeight - 4,
    cartes: document.querySelectorAll('[role="main"] [role="article"], [role="feed"] [role="article"]').length
})"""


async def defiler_resultats(page, *, reprendre=False):
    """Défile le conteneur des cartes, mesure le mouvement et ne touche pas aux filtres."""
    target = await page.evaluate_handle(SCROLL_TARGET)
    try:
        before = await target.evaluate(SCROLL_STATE)
        if reprendre:
            # Une seule remontée locale avant de repasser au bas pour réamorcer
            # un chargement paresseux. Pas de rechargement ni de nouvelle recherche.
            await target.evaluate('(el) => el.scrollBy({top: -el.clientHeight * .5, behavior: "instant"})')
            await asyncio.sleep(0.4)
        for _ in range(2):
            await target.evaluate('(el) => el.scrollBy({top: el.clientHeight * .85, behavior: "instant"})')
            await asyncio.sleep(0.4)
        after = await target.evaluate(SCROLL_STATE)
        return dict(avant=before, apres=after, deplacement=after['position'] - before['position'], reprise=reprendre)
    finally:
        await target.dispose()


async def diagnostic_stagnation(page, directory, report):
    """Rapport sans cookies, URL complète, corps GraphQL ni texte de publication."""
    from durable import atomic_json
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory / 'progression.json', report, private=True)
    try:
        await page.screenshot(path=str(directory / 'page.png'), full_page=False, timeout=5000)
    except PlaywrightError:
        pass  # le rapport chiffré reste disponible si la capture échoue
