"""Diagnostic des curseurs GraphQL du fil d'un groupe Facebook.

Ce script ne tente PAS encore de rejouer les requêtes. Il observe une courte
session réelle et enregistre uniquement les métadonnées nécessaires pour
vérifier que Facebook expose bien un curseur de pagination réutilisable :
- nom de requête GraphQL ;
- doc_id ;
- variables NON sensibles ;
- end_cursor / has_next_page de la réponse ;
- nombre de posts détectés.

Les jetons de session (fb_dtsg, jazoest, cookies, etc.) ne sont jamais écrits
sur disque.

Exemple :
    python scripts/diagnostic_graphql_cursor.py \
      --group-id 364054065107714 \
      --scrolls 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from dotenv import load_dotenv
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

import config
import scraper

SENSITIVE_FORM_FIELDS = {
    "fb_dtsg",
    "jazoest",
    "__user",
    "__a",
    "__req",
    "__hs",
    "dpr",
    "__ccg",
    "__rev",
    "__s",
    "__hsi",
    "__dyn",
    "__csr",
    "__comet_req",
    "lsd",
}


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def _extract_page_infos(payload: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()

    for node in _walk(payload):
        page_info = node.get("page_info")
        if not isinstance(page_info, dict):
            continue
        end_cursor = page_info.get("end_cursor")
        start_cursor = page_info.get("start_cursor")
        has_next_page = page_info.get("has_next_page")
        if end_cursor is None and start_cursor is None:
            continue

        key = (start_cursor, end_cursor, has_next_page)
        if key in seen:
            continue
        seen.add(key)

        found.append(
            {
                "start_cursor": start_cursor,
                "end_cursor": end_cursor,
                "has_next_page": has_next_page,
            }
        )

    return found


def _extract_cursor_values(obj: Any, path: str = "") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            next_path = f"{path}.{key}" if path else key
            if key.lower() in {"cursor", "after", "before"} and value not in (None, ""):
                out.append({"path": next_path, "value": value})
            out.extend(_extract_cursor_values(value, next_path))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            next_path = f"{path}[{index}]"
            out.extend(_extract_cursor_values(value, next_path))
    return out


def _safe_request_metadata(request: Any) -> dict[str, Any]:
    post_data = request.post_data or ""
    parsed = parse_qs(post_data, keep_blank_values=True)

    friendly_name = (parsed.get("fb_api_req_friendly_name") or [None])[0]
    doc_id = (parsed.get("doc_id") or [None])[0]
    variables_raw = (parsed.get("variables") or [None])[0]

    variables: Any = None
    if variables_raw:
        try:
            variables = json.loads(variables_raw)
        except json.JSONDecodeError:
            variables = {"_raw_invalid_json": variables_raw[:1000]}

    # On conserve seulement les champs explicitement utiles et non sensibles.
    safe_extra = {}
    for key in ("av", "server_timestamps"):
        if key in parsed and key not in SENSITIVE_FORM_FIELDS:
            safe_extra[key] = parsed[key][0]

    return {
        "friendly_name": friendly_name,
        "doc_id": doc_id,
        "variables": variables,
        "request_cursors": _extract_cursor_values(variables),
        "extra": safe_extra,
    }


def _parse_graphql_body(body: str) -> list[Any]:
    body = body.removeprefix("for (;;);")
    payloads: list[Any] = []

    candidates = [body, *body.splitlines()]
    seen_text: set[str] = set()

    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate or candidate in seen_text:
            continue
        seen_text.add(candidate)
        try:
            payloads.append(json.loads(candidate))
        except json.JSONDecodeError:
            continue

    return payloads


async def run(group_id: str, scrolls: int, wait_seconds: float) -> int:
    cookies_json = os.environ.get(config.ENV_FB_COOKIES, "").strip()
    if not cookies_json:
        raise ValueError(
            f"{config.ENV_FB_COOKIES} absente. Ajoutez vos cookies dans .env "
            "ou dans l'environnement avant ce diagnostic."
        )

    cookies_secret = scraper.charger_cookies(cookies_json)
    cached = scraper._charger_cookies_caches()

    traces: list[dict[str, Any]] = []

    async def _tentative(playwright: Any, cookies: list[dict[str, Any]], label: str) -> None:
        tasks: set[asyncio.Task[Any]] = set()
        browser, context = await scraper.creer_navigateur(playwright, cookies)
        page = await context.new_page()

        async def inspect_response(response: Any) -> None:
            if not any(
                fragment in response.url
                for fragment in config.GRAPHQL_URL_FRAGMENTS
            ):
                return

            try:
                body = await response.text()
            except Exception:
                return

            request_meta = _safe_request_metadata(response.request)

            # Facebook peut renvoyer une même requête GraphQL sous forme
            # multipart/defer : les stories arrivent dans un chunk et le
            # page_info dans un autre. L'ancien diagnostic traitait chaque
            # ligne JSON séparément et pouvait donc afficher à tort
            # "Posts + page_info = 0" même quand les deux appartenaient à la
            # même réponse HTTP. On agrège maintenant tous les chunks d'une
            # réponse avant de conclure.
            posts_par_id: dict[str, dict[str, Any]] = {}
            page_infos_agreges: list[dict[str, Any]] = []
            page_infos_vus: set[str] = set()

            for payload in _parse_graphql_body(body):
                for post in scraper.extraire_stories_depuis_json(
                    payload,
                    group_id,
                    f"diagnostic_{group_id}",
                ):
                    post_id = str(post.get("id") or "").strip()
                    if post_id:
                        posts_par_id[post_id] = post

                for page_info in _extract_page_infos(payload):
                    fp = json.dumps(page_info, ensure_ascii=False, sort_keys=True)
                    if fp in page_infos_vus:
                        continue
                    page_infos_vus.add(fp)
                    page_infos_agreges.append(page_info)

            posts = list(posts_par_id.values())

            # Élimine la majorité des GraphQL sans rapport avec le fil.
            if not posts and not page_infos_agreges:
                return

            traces.append(
                {
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "request": request_meta,
                    "response": {
                        "posts_detected": len(posts),
                        "post_ids_sample": [
                            str(post.get("id") or "")
                            for post in posts[:5]
                        ],
                        "page_infos": page_infos_agreges,
                    },
                }
            )

        def on_response(response: Any) -> None:
            task = asyncio.ensure_future(inspect_response(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        page.on("response", on_response)

        try:
            url = f"{config.WEB_FACEBOOK_BASE_URL}/groups/{group_id}/"
            print(f"Ouverture ({label}) : {url}")
            await page.goto(url, wait_until="domcontentloaded")
            await scraper.detecter_blocage_ou_session_expiree(page)

            for index in range(1, scrolls + 1):
                await page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
                await asyncio.sleep(wait_seconds)
                if tasks:
                    await asyncio.gather(*list(tasks), return_exceptions=True)

                cursors = sum(
                    len(t.get("response", {}).get("page_infos", []))
                    for t in traces
                )
                feed_traces = sum(
                    1
                    for t in traces
                    if t.get("response", {}).get("posts_detected", 0) > 0
                )
                print(
                    f"Scroll {index}/{scrolls} : "
                    f"traces utiles={len(traces)}, "
                    f"réponses avec posts={feed_traces}, "
                    f"page_info/cursors={cursors}"
                )

            if tasks:
                await asyncio.gather(*list(tasks), return_exceptions=True)

        finally:
            page.remove_listener("response", on_response)
            try:
                await scraper.sauvegarder_storage_state(context)
            except Exception:
                pass
            await page.close()
            await context.close()
            await browser.close()

    async with async_playwright() as playwright:
        if cached is not None:
            try:
                await _tentative(playwright, cached, "cookies sauvegardés")
            except scraper.SessionExpireeError:
                print(
                    "Session sauvegardée invalide côté Facebook. "
                    "Suppression du storage_state puis nouvel essai avec FB_COOKIES_JSON."
                )
                scraper.invalider_storage_state()
                traces.clear()
                await _tentative(playwright, cookies_secret, "FB_COOKIES_JSON")
        else:
            await _tentative(playwright, cookies_secret, "FB_COOKIES_JSON")

    # Déduplique les traces identiques pour garder un fichier lisible.
    unique: list[dict[str, Any]] = []
    fingerprints: set[str] = set()
    for trace in traces:
        payload = {
            "request": trace["request"],
            "response": trace["response"],
        }
        fingerprint = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        unique.append(trace)

    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = config.LOG_DIR / f"graphql_cursor_{group_id}_{stamp}.json"
    output.write_text(
        json.dumps(unique, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    cursor_traces = [
        trace
        for trace in unique
        if trace.get("response", {}).get("page_infos")
    ]
    feed_with_cursor = [
        trace
        for trace in cursor_traces
        if trace.get("response", {}).get("posts_detected", 0) > 0
    ]

    print("\nDIAGNOSTIC CURSEUR GRAPHQL")
    print("-" * 48)
    print(f"Traces GraphQL utiles    : {len(unique)}")
    print(f"Traces avec page_info    : {len(cursor_traces)}")
    print(f"Posts + page_info        : {len(feed_with_cursor)}")
    print(f"Fichier diagnostic       : {output}")

    if feed_with_cursor:
        latest = feed_with_cursor[-1]
        request = latest["request"]
        page_infos = latest["response"]["page_infos"]
        print(f"Requête candidate        : {request.get('friendly_name')}")
        print(f"doc_id                   : {request.get('doc_id')}")
        print(
            "Curseur requête          : "
            f"{request.get('request_cursors') or 'aucun sur cette page'}"
        )
        print(f"page_info réponse        : {page_infos[-1]}")
        print(
            "\nOK : un curseur exploitable a été observé. "
            "On pourra construire le mode reprise à partir de cette trace."
        )
    else:
        # Affiche les meilleures pistes même si Facebook sépare encore les
        # stories et le page_info entre plusieurs requêtes.
        posts_only = [
            t for t in unique
            if t.get("response", {}).get("posts_detected", 0) > 0
        ]
        page_only = [
            t for t in unique
            if t.get("response", {}).get("page_infos")
        ]
        print(
            "\nToujours aucun couple posts + page_info après agrégation."
        )
        if posts_only:
            req = posts_only[-1]["request"]
            print(
                "Dernière requête avec posts : "
                f"{req.get('friendly_name')} | doc_id={req.get('doc_id')} | "
                f"cursors={req.get('request_cursors') or 'aucun'}"
            )
        if page_only:
            req = page_only[-1]["request"]
            infos = page_only[-1]["response"]["page_infos"]
            print(
                "Dernière requête page_info  : "
                f"{req.get('friendly_name')} | doc_id={req.get('doc_id')} | "
                f"cursors={req.get('request_cursors') or 'aucun'} | "
                f"page_info={infos[-1]}"
            )
        print(
            "Envoyez cette sortie : elle permettra d'identifier si le curseur "
            "vient d'une requête de fil séparée."
        )

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--scrolls", type=int, default=8)
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=5.0,
        help="Pause entre deux scrolls du diagnostic (défaut : 5s).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return asyncio.run(
        run(
            group_id=args.group_id,
            scrolls=max(1, args.scrolls),
            wait_seconds=max(2.0, args.wait_seconds),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
