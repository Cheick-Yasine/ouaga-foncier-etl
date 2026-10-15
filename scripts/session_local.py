"""Importer des cookies locaux sans les afficher ni les envoyer à GitHub."""
import argparse
import asyncio
import re
import json
import os
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from durable import atomic_json, account_lock


def cooldown_session(path):
    """Ne jamais lever un blocage ou une pause dont la raison est inconnue."""
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return str(value.get('raison', '')).startswith('session expirée sur ')
    except (OSError, ValueError, AttributeError):
        return False


async def verifier_session(cookies):
    """Une seule navigation ; preuve positive du compte connecté obligatoire."""
    from playwright.async_api import async_playwright
    from scraper import detecter_blocage_ou_session_expiree
    expected = next(c['value'] for c in cookies if c['name'] == 'c_user')
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(locale='fr-FR', timezone_id='Africa/Ouagadougou')
            await context.add_cookies(cookies)
            page = await context.new_page()
            await page.goto('https://www.facebook.com/', wait_until='domcontentloaded', timeout=30000)
            await detecter_blocage_ou_session_expiree(page)
            content = await page.content()
            actors = re.findall(r'"(?:USER_ID|actorID)"\s*:\s*"([0-9]+)"', content)
            if expected == '0' or expected not in actors:
                raise ValueError('Connexion non confirmée : pause conservée. Vérifier le compte dans Facebook.')
            return await context.cookies()
        finally:
            await browser.close()


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compte', choices=list('12345'), required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--export', type=Path)
    source.add_argument('--interactive', action='store_true', help='Ouvre Facebook pour une connexion manuelle, puis enregistre la session localement.')
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('OUAGA_PERSISTENT_DIR', str(Path.home() / 'ouaga-etl-data'))))
    args = parser.parse_args()
    root = args.data_dir.expanduser().resolve() / f'compte_{args.compte}'
    os.environ['OUAGA_DATA_DIR'] = str(root)
    import config
    from scraper import charger_cookies
    from scripts.maj_cookies import valider_cookies_captures, capturer_session_interactive
    with account_lock(root / 'daily.lock'):
        contenu = capturer_session_interactive() if args.interactive else args.export.read_text(encoding='utf-8')
        cookies = charger_cookies(contenu)
        valider_cookies_captures(cookies)
        pause = config.cooldown_path(args.compte)
        lever_pause = cooldown_session(pause)
        if lever_pause:
            print('Vérification unique de la nouvelle session Facebook...', flush=True)
            cookies = asyncio.run(verifier_session(cookies))
        atomic_json(config.storage_state_path(args.compte), {'cookies': cookies, 'origins': []}, private=True)
        if lever_pause:
            pause.unlink(missing_ok=True)
            print('Connexion confirmée : pause pour session expirée levée. Reprise possible.')
    print(f'Session du compte {args.compte} enregistrée localement. Aucun cookie affiché. Les pauses pour blocage restent respectées.')


if __name__ == '__main__':
    main()
