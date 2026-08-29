#!/usr/bin/env python3
"""Applique les patches anti-détection sur scraper.py (mode mobile, scroll humain, etc.).

Usage (depuis la racine du dépôt, branche anti-detection-mobile-humain) :
    python scripts/apply_anti_detection_scraper.py
    git add scraper.py && git commit -m "Apply anti-detection scraper patches"
"""
from __future__ import annotations

import pathlib
import urllib.request

URL = (
    "https://raw.githubusercontent.com/Cheick-Yasine/ouaga-foncier-etl/"
    "main/scraper.py"
)


def main() -> None:
    print("Téléchargement de scraper.py depuis main...")
    text = urllib.request.urlopen(URL, timeout=60).read().decode("utf-8")

    patches: list[tuple[str, str, str]] = []

    # 1. creer_navigateur -> mode mobile
    patches.append(
        (
            "creer_navigateur",
            (
                "    navigateur = await playwright.chromium.launch(\n"
                "        headless=True,\n"
                "        args=[\"--disable-blink-features=AutomationControlled\"],\n"
                "        proxy=proxy,\n"
                "    )\n"
                "    contexte = await navigateur.new_context(\n"
                "    # Viewport desktop : cohérent avec le User-Agent Chrome/Windows utilisé\n"
                "    # ci-dessous (config.MBASIC_USER_AGENT, malgré son nom historique, est\n"
                "    # en réalité un UA Chrome desktop standard depuis le passage à Comet -\n"
                "    # voir historique dans config.py). Un viewport mobile (360x640) combiné\n"
                "    # à un UA desktop était un signal incohérent facilement détectable par\n"
                "    # Facebook, corrigé le 2026-08-06.\n"
                "    viewport={\"width\": 1366, \"height\": 900},\n"
                "    locale=\"fr-FR\",\n"
                "    timezone_id=\"Africa/Ouagadougou\",\n"
                "    user_agent=config.MBASIC_USER_AGENT,\n"
                "    storage_state={\"cookies\": [], \"origins\": _charger_origins_sauvegardees(compte)},\n"
                ")\n"
            ),
            (
                "    user_agent, viewport = config.choisir_fingerprint_mobile()\n"
                "    logger.info(\n"
                "        \"Fingerprint mobile : viewport=%sx%s | UA=%s...\",\n"
                "        viewport[\"width\"],\n"
                "        viewport[\"height\"],\n"
                "        user_agent[:60],\n"
                "    )\n"
                "    navigateur = await playwright.chromium.launch(\n"
                "        headless=True,\n"
                "        args=[\"--disable-blink-features=AutomationControlled\"],\n"
                "        proxy=proxy,\n"
                "    )\n"
                "    contexte = await navigateur.new_context(\n"
                "        viewport=viewport,\n"
                "        user_agent=user_agent,\n"
                "        is_mobile=True,\n"
                "        has_touch=True,\n"
                "        locale=\"fr-FR\",\n"
                "        timezone_id=\"Africa/Ouagadougou\",\n"
                "        storage_state={\"cookies\": [], \"origins\": _charger_origins_sauvegardees(compte)},\n"
                "    )\n"
            ),
        )
    )

    for name, old, new in patches:
        if old not in text:
            raise SystemExit(f"Patch '{name}' : motif introuvable dans scraper.py")
        text = text.replace(old, new, 1)
        print(f"  OK {name}")

    # Scroll humain helper + echauffement : inserts manuels ci-dessous via marqueurs
    # Pour fiabilité, on remplace le corps d'echauffement entier
    old_ech = (
        "    page = await contexte.new_page()\n"
        "    try:\n"
        '        await page.goto(f"{config.WEB_FACEBOOK_BASE_URL}/", wait_until="domcontentloaded")\n'
        "        await asyncio.sleep(random.uniform(3.0, 7.0))\n"
        "    except Exception as exc:\n"
        '        logger.debug("Échauffement ignoré (non bloquant) : %s", exc)\n'
        "    finally:\n"
        "        await page.close()\n"
    )
    new_ech = (
        "    page = await contexte.new_page()\n"
        "    try:\n"
        "        await page.goto(\n"
        '            f"{config.MOBILE_FACEBOOK_BASE_URL}/", wait_until="domcontentloaded"\n'
        "        )\n"
        "        await asyncio.sleep(random.uniform(4.0, 10.0))\n"
        '        await page.evaluate("window.scrollBy(0, window.innerHeight * 1.5)")\n'
        "        await asyncio.sleep(random.uniform(3.0, 8.0))\n"
        "        try:\n"
        "            await page.goto(\n"
        '                f"{config.MOBILE_FACEBOOK_BASE_URL}/notifications",\n'
        '                wait_until="domcontentloaded",\n'
        "            )\n"
        "            await asyncio.sleep(random.uniform(2.0, 6.0))\n"
        "        except Exception:\n"
        "            pass\n"
        "    except Exception as exc:\n"
        '        logger.debug("Échauffement ignoré (non bloquant) : %s", exc)\n'
        "    finally:\n"
        "        await page.close()\n"
        "\n\n"
        "async def _scroll_humain(page: Page, delai_multiplicateur: float = 1.0) -> None:\n"
        '    """Scroll non-linéaire avec distances et micro-pauses variables."""\n'
        '    hauteur = await page.evaluate("window.innerHeight")\n'
        "    for _ in range(random.randint(2, 5)):\n"
        "        distance = random.uniform(0.35, 1.15) * hauteur\n"
        '        await page.evaluate(f"window.scrollBy(0, {distance})")\n'
        "        await asyncio.sleep(\n"
        "            random.uniform(\n"
        "                config.SCROLL_MICRO_PAUSE_MIN_S, config.SCROLL_MICRO_PAUSE_MAX_S\n"
        "            )\n"
        "            * delai_multiplicateur\n"
        "        )\n"
        "    if random.random() < 0.15:\n"
        '        await page.evaluate(f"window.scrollBy(0, -{hauteur * 0.25})")\n'
        "        await asyncio.sleep(random.uniform(0.4, 1.4) * delai_multiplicateur)\n"
    )
    if old_ech not in text:
        raise SystemExit("Patch echauffement : motif introuvable")
    text = text.replace(old_ech, new_ech, 1)
    print("  OK echauffement + _scroll_humain")

    old_scroll = (
        '            await page.evaluate("window.scrollBy(0, window.innerHeight * 3)")\n'
    )
    new_scroll = "            await _scroll_humain(page, delai_multiplicateur)\n"
    if old_scroll not in text:
        raise SystemExit("Patch scroll : motif introuvable")
    text = text.replace(old_scroll, new_scroll, 1)
    print("  OK scroll humain")

    old_open = (
        "        await detecter_blocage_ou_session_expiree(page)\n\n"
        '        # Posts "mis en avant" présents dès le chargement initial.\n'
    )
    new_open = (
        "        await detecter_blocage_ou_session_expiree(page)\n"
        "        await asyncio.sleep(\n"
        "            random.uniform(config.TEMPS_LECTURE_MIN_S, config.TEMPS_LECTURE_MAX_S)\n"
        "            * delai_multiplicateur\n"
        "        )\n\n"
        '        # Posts "mis en avant" (filtrés via seen_ids - pas de ré-export).\n'
    )
    if old_open not in text:
        raise SystemExit("Patch lecture : motif introuvable")
    text = text.replace(old_open, new_open, 1)
    print("  OK temps de lecture")

    # Fermeture navigateur entre groupes
    old_pause = (
        "                    if i < len(lot) - 1:\n"
        "                        await asyncio.sleep(\n"
        "                            random.uniform(\n"
        "                                config.PAUSE_ENTRE_GROUPES_MIN_S,\n"
        "                                config.PAUSE_ENTRE_GROUPES_MAX_S,\n"
        "                            )\n"
        "                            * ajustements.delai_multiplicateur\n"
        "                        )\n"
    )
    new_pause = (
        "                    if i < len(lot) - 1:\n"
        "                        if not session_expiree:\n"
        "                            await sauvegarder_storage_state(contexte, compte)\n"
        "                        await contexte.close()\n"
        "                        await navigateur.close()\n"
        "                        logger.info(\n"
        '                            "Navigateur fermé proprement entre groupes "\n'
        '                            "(simulation déconnexion naturelle)."\n'
        "                        )\n"
        "                        await asyncio.sleep(\n"
        "                            random.uniform(\n"
        "                                config.PAUSE_ENTRE_GROUPES_MIN_S,\n"
        "                                config.PAUSE_ENTRE_GROUPES_MAX_S,\n"
        "                            )\n"
        "                            * ajustements.delai_multiplicateur\n"
        "                        )\n"
        "                        cookies_caches = _charger_cookies_caches(compte)\n"
        "                        cookies = (\n"
        "                            cookies_caches\n"
        "                            if cookies_caches is not None\n"
        "                            else cookies_secret\n"
        "                        )\n"
        "                        navigateur, contexte = await creer_navigateur(\n"
        "                            playwright, cookies, compte, proxy\n"
        "                        )\n"
        "                        await echauffement(contexte)\n"
    )
    if old_pause not in text:
        raise SystemExit("Patch pause/fermeture : motif introuvable")
    text = text.replace(old_pause, new_pause, 1)
    print("  OK fermeture navigateur entre groupes")

    pathlib.Path("scraper.py").write_text(text, encoding="utf-8")
    print(f"Écrit scraper.py ({len(text)} caractères). Committez le fichier.")


if __name__ == "__main__":
    main()
