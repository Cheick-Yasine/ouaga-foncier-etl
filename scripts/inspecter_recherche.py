"""Une recherche visible sur un groupe, sans scroll, LLM ni écriture dans Neon."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def route_sans_secrets(url):
    parsed = urlparse(url)
    result = {'hote': parsed.hostname, 'chemin': parsed.path}
    for key, values in parse_qs(parsed.query).items():
        if key in {'q', 'query'} and values in (['terrain'], ['parcelle']):
            result[key] = values[0]
    return result


async def snapshot(page, directory, suffix):
    from scripts.diagnostic_recherche import Diagnostic
    from durable import atomic_json
    directory.mkdir(parents=True, exist_ok=True)
    html = await page.content()
    diagnostic = Diagnostic()
    diagnostic.feed(html)
    report = dict(route=route_sans_secrets(page.url), roles=dict(diagnostic.roles), controles_recherche=diagnostic.controls)
    atomic_json(directory / f'{suffix}.json', report, private=True)
    await page.screenshot(path=str(directory / f'{suffix}.png'), full_page=False)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print(f'Capture de la page : {directory / (suffix + ".png")}', flush=True)


async def inspect(args):
    import config
    import scraper
    from group_search import configure_search
    from playwright.async_api import async_playwright
    cooldown = scraper.verifier_cooldown(args.compte)
    if cooldown:
        raise ValueError(f'Pause active jusqu’à {cooldown.isoformat()}.')
    groups = config.charger_groupes(compte=args.compte)
    group = next((g for g in groups if str(g.id) == args.groupe), None)
    if group is None:
        raise ValueError('Groupe absent des groupes actifs de ce compte.')
    cookies = scraper._charger_cookies_caches(args.compte)
    if cookies is None:
        raise ValueError('Session locale absente. Importer les cookies du compte avant ce diagnostic.')
    async with async_playwright() as playwright:
        browser, context = await scraper.creer_navigateur(playwright, cookies, args.compte, None, headless=False)
        try:
            page = await context.new_page()
            try:
                await configure_search(page, group, args.mot)
                print('Recherche et filtre confirmés. Diagnostic uniquement : aucun scroll lancé.', flush=True)
            except Exception as exc:
                print(f'Recherche non confirmée ({type(exc).__name__}). La fenêtre reste ouverte pour inspection.', flush=True)
            directory = config.LOG_DIR / f'inspection_{args.groupe}_{args.mot}'
            await snapshot(page, directory, 'automatique')
            await asyncio.to_thread(input, 'Regarde la page et prends une capture. Appuie sur Entrée ici lorsque tu as terminé...')
            if not page.is_closed():
                await snapshot(page, directory, 'final')
        finally:
            await context.close()
            await browser.close()


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compte', choices=list('12345'), required=True)
    parser.add_argument('--browser', choices=['mobile', 'desktop'], help='Interface Facebook (desktop = ordinateur).')
    parser.add_argument('--engine', choices=['chromium', 'firefox'], help='Moteur du navigateur ; Firefox nécessite desktop.')
    parser.add_argument('--groupe', required=True)
    parser.add_argument('--mot', choices=['terrain', 'parcelle'], default='terrain')
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('OUAGA_PERSISTENT_DIR', str(Path.home() / 'ouaga-etl-data'))))
    args = parser.parse_args()
    if args.browser is not None:
        os.environ['OUAGA_BROWSER_MODE'] = args.browser
    if args.engine is not None:
        os.environ['OUAGA_BROWSER_ENGINE'] = args.engine
    if os.environ.get('OUAGA_BROWSER_ENGINE') == 'firefox' and os.environ.get('OUAGA_BROWSER_MODE', 'mobile') != 'desktop':
        parser.error('Firefox nécessite --browser desktop.')
    root = args.data_dir.expanduser().resolve() / f'compte_{args.compte}'
    os.environ['OUAGA_DATA_DIR'] = str(root)
    import config
    config.configurer_logging()
    from durable import account_lock, AccountBusyError
    try:
        with account_lock(root / 'daily.lock'):
            asyncio.run(inspect(args))
    except AccountBusyError:
        parser.exit(1, 'Ce compte est déjà utilisé par une collecte ou un diagnostic. Fermer cet autre processus avant de relancer.\n')


if __name__ == '__main__':
    main()
