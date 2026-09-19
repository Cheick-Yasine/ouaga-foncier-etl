"""Rattrapage historique par pagination GraphQL avec curseur persistant.

Objectif
--------
Ne plus repartir du haut du groupe à chaque interruption. Le script capture
UNE requête de pagination fraîche depuis la page Facebook authentifiée, puis
réutilise uniquement son gabarit en remplaçant le curseur page après page.

Sécurité / persistance
----------------------
- aucun cookie, fb_dtsg, jazoest ou corps de requête n'est sauvegardé ;
- seuls le curseur opaque et les compteurs de progression sont persistés ;
- les RAW de la période cible sont écrits dans raw_posts_checkpoint AVANT la
  progression du curseur : si le processus meurt entre les deux, la page sera
  rejouée au run suivant mais les doublons restent sans conséquence ;
- le curseur est sauvegardé localement ET dans Neon quand DATABASE_URL existe.

Exemple de test court :
    python scripts/backfill_graphql_cursor.py \
      --group-id 364054065107714 \
      --start-date 2026-08-31 \
      --end-date 2026-09-13 \
      --max-pages 5

Relancer exactement la même commande reprend au dernier curseur sauvegardé.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from datetime import date, datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode

import psycopg
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

import config
import scraper

FRIENDLY_NAME = "GroupsCometFeedRegularStoriesPaginationQuery"
OLD_PAGE_CONFIRMATIONS = 5

CREATE_CURSOR_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS facebook_backfill_cursor (
    groupe_id TEXT NOT NULL,
    period_key TEXT NOT NULL,
    cursor TEXT,
    pages_done INTEGER NOT NULL DEFAULT 0,
    target_posts_saved INTEGER NOT NULL DEFAULT 0,
    oldest_seen TIMESTAMPTZ,
    has_next_page BOOLEAN,
    status TEXT NOT NULL DEFAULT 'running',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (groupe_id, period_key)
)
"""

CREATE_RAW_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS raw_posts_checkpoint (
    post_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    groupe_id TEXT,
    payload JSONB NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed BOOLEAN NOT NULL DEFAULT FALSE
)
"""


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Date invalide '{value}'. Format attendu : AAAA-MM-JJ."
        ) from exc


def _dt_start(day: date) -> datetime:
    return datetime.combine(day, dt_time.min, tzinfo=timezone.utc)


def _post_date(post: dict[str, Any]) -> datetime | None:
    raw = post.get("date_publication")
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def _extract_page_infos(payload: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for node in _walk(payload):
        page_info = node.get("page_info")
        if not isinstance(page_info, dict):
            continue
        start_cursor = page_info.get("start_cursor")
        end_cursor = page_info.get("end_cursor")
        has_next_page = page_info.get("has_next_page")
        if start_cursor is None and end_cursor is None:
            continue
        key = (start_cursor, end_cursor, has_next_page)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "start_cursor": start_cursor,
                "end_cursor": end_cursor,
                "has_next_page": has_next_page,
            }
        )
    return out


def _parse_graphql_body(body: str) -> list[Any]:
    body = body.removeprefix("for (;;);")
    out: list[Any] = []
    seen: set[str] = set()
    for candidate in (body, *body.splitlines()):
        candidate = candidate.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            out.append(json.loads(candidate))
        except json.JSONDecodeError:
            continue
    return out


def _aggregate_response(
    body: str,
    group_id: str,
    group_name: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    posts_by_id: dict[str, dict[str, Any]] = {}
    page_infos: list[dict[str, Any]] = []
    seen_infos: set[str] = set()

    for payload in _parse_graphql_body(body):
        for post in scraper.extraire_stories_depuis_json(
            payload,
            group_id,
            group_name,
        ):
            post_id = str(post.get("id") or "").strip()
            if post_id:
                posts_by_id[post_id] = post

        for info in _extract_page_infos(payload):
            fp = json.dumps(info, sort_keys=True, ensure_ascii=False)
            if fp in seen_infos:
                continue
            seen_infos.add(fp)
            page_infos.append(info)

    return list(posts_by_id.values()), page_infos


def _request_friendly_name(post_data: str | None) -> str | None:
    if not post_data:
        return None
    parsed = parse_qs(post_data, keep_blank_values=True)
    return (parsed.get("fb_api_req_friendly_name") or [None])[0]


def _cursor_from_post_data(post_data: str) -> str | None:
    parsed = parse_qs(post_data, keep_blank_values=True)
    raw_variables = (parsed.get("variables") or [None])[0]
    if not raw_variables:
        return None
    try:
        variables = json.loads(raw_variables)
    except json.JSONDecodeError:
        return None
    cursor = variables.get("cursor")
    return str(cursor) if cursor not in (None, "") else None


def _patch_cursor(post_data: str, cursor: str) -> str:
    pairs = parse_qsl(post_data, keep_blank_values=True)
    patched: list[tuple[str, str]] = []
    replaced = False

    for key, value in pairs:
        if key != "variables":
            patched.append((key, value))
            continue

        variables = json.loads(value)
        variables["cursor"] = cursor
        patched.append(
            (
                key,
                json.dumps(
                    variables,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        )
        replaced = True

    if not replaced:
        raise RuntimeError("Champ GraphQL 'variables' absent de la requête capturée.")

    return urlencode(patched)


def _state_path(group_id: str, period_key: str) -> Path:
    safe_period = period_key.replace(":", "_")
    return config.STATE_DIR / f"graphql_backfill_{group_id}_{safe_period}.json"


def _load_local_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _save_local_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _load_neon_state(
    conn: psycopg.Connection | None,
    group_id: str,
    period_key: str,
) -> dict[str, Any] | None:
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cursor, pages_done, target_posts_saved, oldest_seen,
                   has_next_page, status, updated_at
            FROM facebook_backfill_cursor
            WHERE groupe_id = %s AND period_key = %s
            """,
            (group_id, period_key),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "cursor": row[0],
        "pages_done": row[1],
        "target_posts_saved": row[2],
        "oldest_seen": row[3].isoformat() if row[3] else None,
        "has_next_page": row[4],
        "status": row[5],
        "updated_at": row[6].isoformat() if row[6] else None,
    }


def _save_neon_state(
    conn: psycopg.Connection | None,
    group_id: str,
    period_key: str,
    state: dict[str, Any],
) -> None:
    if conn is None:
        return
    conn.execute(
        """
        INSERT INTO facebook_backfill_cursor (
            groupe_id, period_key, cursor, pages_done, target_posts_saved,
            oldest_seen, has_next_page, status, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (groupe_id, period_key) DO UPDATE SET
            cursor = EXCLUDED.cursor,
            pages_done = EXCLUDED.pages_done,
            target_posts_saved = EXCLUDED.target_posts_saved,
            oldest_seen = EXCLUDED.oldest_seen,
            has_next_page = EXCLUDED.has_next_page,
            status = EXCLUDED.status,
            updated_at = NOW()
        """,
        (
            group_id,
            period_key,
            state.get("cursor"),
            int(state.get("pages_done") or 0),
            int(state.get("target_posts_saved") or 0),
            state.get("oldest_seen"),
            state.get("has_next_page"),
            state.get("status") or "running",
        ),
    )


def _load_existing_annonce_ids(conn: psycopg.Connection | None) -> set[str]:
    if conn is None:
        return set()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM annonces")
        return {str(row[0]) for row in cur.fetchall()}


def _persist_raw_posts(
    conn: psycopg.Connection | None,
    run_id: str,
    group_id: str,
    posts: list[dict[str, Any]],
) -> int:
    if conn is None or not posts:
        return 0
    rows = [
        (str(post["id"]), run_id, group_id, Jsonb(post))
        for post in posts
        if post.get("id")
    ]
    if not rows:
        return 0

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO raw_posts_checkpoint
                (post_id, run_id, groupe_id, payload, captured_at, processed)
            VALUES (%s, %s, %s, %s, NOW(), FALSE)
            ON CONFLICT (post_id) DO UPDATE SET
                run_id = EXCLUDED.run_id,
                groupe_id = EXCLUDED.groupe_id,
                payload = EXCLUDED.payload,
                captured_at = NOW(),
                processed = FALSE
            """,
            rows,
        )
    return len(rows)


def _load_local_raw(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, list):
        return {}
    return {
        str(post.get("id")): post
        for post in value
        if isinstance(post, dict) and post.get("id")
    }


def _save_local_raw(path: Path, posts_by_id: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(list(posts_by_id.values()), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


async def _capture_fresh_template(
    page: Any,
    group_id: str,
    group_name: str,
    max_scrolls: int = 16,
) -> tuple[str, str]:
    """Capture un gabarit de pagination GraphQL réellement exploitable.

    Facebook peut changer le friendly_name/doc_id ou ne pas émettre exactement
    la requête attendue au premier scroll. On valide donc la réponse elle-même :
    requête POST GraphQL + variables contenant un curseur + réponse contenant
    un page_info/end_cursor. Cela évite de dépendre d'un nom interne unique.
    """
    captured: dict[str, str] = {}
    tasks: set[asyncio.Task[Any]] = set()
    seen_names: set[str] = set()
    graphql_candidates = 0

    async def inspect_response(response: Any) -> None:
        nonlocal graphql_candidates
        if captured:
            return

        request = response.request
        if request.method != "POST":
            return
        if not any(fragment in request.url for fragment in config.GRAPHQL_URL_FRAGMENTS):
            return

        post_data = request.post_data
        if not post_data:
            return

        friendly_name = _request_friendly_name(post_data)
        if friendly_name:
            seen_names.add(friendly_name)

        # Un vrai gabarit de page suivante doit déjà contenir un curseur.
        if not _cursor_from_post_data(post_data):
            return

        graphql_candidates += 1

        try:
            body = await response.text()
        except Exception:
            return

        posts, page_infos = _aggregate_response(
            body,
            group_id,
            group_name,
        )
        has_end_cursor = any(info.get("end_cursor") for info in page_infos)

        if not has_end_cursor:
            return

        captured["url"] = request.url
        captured["post_data"] = post_data
        captured["friendly_name"] = friendly_name or "inconnu"
        captured["posts_detected"] = str(len(posts))

    def on_response(response: Any) -> None:
        task = asyncio.ensure_future(inspect_response(response))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    page.on("response", on_response)
    try:
        for index in range(1, max_scrolls + 1):
            # Aller au bas du DOM courant déclenche plus fiablement la
            # pagination qu'un simple scroll relatif après plusieurs reprises.
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(4.0)

            if tasks:
                await asyncio.gather(*list(tasks), return_exceptions=True)

            if captured:
                print(
                    f"Gabarit GraphQL capturé après {index} scroll(s) | "
                    f"requête={captured.get('friendly_name')} | "
                    f"posts réponse={captured.get('posts_detected')}"
                )
                return captured["url"], captured["post_data"]

            # Petit mouvement inverse pour permettre au navigateur de
            # redéclencher l'observateur de fin de fil au prochain tour.
            await page.evaluate("window.scrollBy(0, -Math.max(250, window.innerHeight * 0.4))")
            await asyncio.sleep(0.8)
    finally:
        page.remove_listener("response", on_response)
        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)

    names = ", ".join(sorted(seen_names)) if seen_names else "aucun"
    raise RuntimeError(
        "Aucun gabarit GraphQL paginé exploitable capturé après "
        f"{max_scrolls} scrolls. Candidats avec curseur={graphql_candidates}; "
        f"friendly_names observés={names}."
    )


async def _fetch_graphql(page: Any, url: str, body: str) -> tuple[int, str]:
    friendly_name = _request_friendly_name(body) or FRIENDLY_NAME
    result = await page.evaluate(
        """async ({url, body, friendlyName}) => {
            const response = await fetch(url, {
                method: "POST",
                credentials: "include",
                headers: {
                    "content-type": "application/x-www-form-urlencoded",
                    "x-fb-friendly-name": friendlyName
                },
                body
            });
            return {
                status: response.status,
                text: await response.text()
            };
        }""",
        {"url": url, "body": body, "friendlyName": friendly_name},
    )
    return int(result["status"]), str(result["text"])


async def run(args: argparse.Namespace) -> int:
    start_dt = _dt_start(args.start_date)
    end_exclusive = _dt_start(date.fromordinal(args.end_date.toordinal() + 1))
    period_key = f"{args.start_date.isoformat()}__{args.end_date.isoformat()}"
    run_id = (
        os.environ.get("GITHUB_RUN_ID")
        or datetime.now(timezone.utc).strftime("cursor-local-%Y%m%dT%H%M%SZ")
    )

    groups = config.charger_groupes(limite=None)
    group = next((g for g in groups if str(g.id) == str(args.group_id)), None)
    group_name = group.nom if group is not None else f"groupe_{args.group_id}"

    dsn = os.environ.get("DATABASE_URL", "").strip()
    conn: psycopg.Connection | None = None
    if dsn:
        conn = psycopg.connect(dsn, autocommit=True, connect_timeout=15)
        conn.execute(CREATE_CURSOR_TABLE_SQL)
        conn.execute(CREATE_RAW_TABLE_SQL)
        print("Neon : checkpoint RAW + curseur persistant activés.")
    else:
        print("ATTENTION : DATABASE_URL absent, persistance locale uniquement.")

    try:
        state_path = _state_path(args.group_id, period_key)
        local_state = _load_local_state(state_path)
        neon_state = _load_neon_state(conn, args.group_id, period_key)

        # Neon prime s'il existe : c'est le checkpoint durable partagé entre
        # local et GitHub Actions. Sinon on reprend l'état local.
        state = neon_state or local_state or {
            "cursor": None,
            "pages_done": 0,
            "target_posts_saved": 0,
            "oldest_seen": None,
            "has_next_page": None,
            "status": "new",
        }

        if args.reset_cursor:
            state = {
                "cursor": None,
                "pages_done": 0,
                "target_posts_saved": 0,
                "oldest_seen": None,
                "has_next_page": None,
                "status": "reset",
            }
            print("Curseur précédent ignoré (--reset-cursor).")

        existing_ids = _load_existing_annonce_ids(conn)
        print(f"Annonces déjà en base chargées : {len(existing_ids)}")

        raw_path = (
            config.RAW_DIR
            / "_live"
            / f"graphql_backfill_{args.group_id}_{period_key}.json"
        )
        raw_by_id = _load_local_raw(raw_path)

        cookies_json = os.environ.get(config.ENV_FB_COOKIES, "").strip()
        if not cookies_json:
            raise ValueError(f"{config.ENV_FB_COOKIES} absente.")

        cookies_secret = scraper.charger_cookies(cookies_json)
        cached = scraper._charger_cookies_caches()
        cookies = cached if cached is not None else cookies_secret

        async with async_playwright() as playwright:
            browser, context = await scraper.creer_navigateur(playwright, cookies)
            page = await context.new_page()
            try:
                url_group = f"{config.WEB_FACEBOOK_BASE_URL}/groups/{args.group_id}/"
                print(f"Ouverture : {url_group}")
                await page.goto(url_group, wait_until="domcontentloaded")
                try:
                    await scraper.detecter_blocage_ou_session_expiree(page)
                except scraper.SessionExpireeError:
                    # Si le cache était utilisé, refaire une seule tentative
                    # avec le secret .env frais.
                    if cached is None:
                        raise
                    await page.close()
                    await context.close()
                    await browser.close()
                    scraper.invalider_storage_state()

                    browser, context = await scraper.creer_navigateur(
                        playwright,
                        cookies_secret,
                    )
                    page = await context.new_page()
                    print("Cache session expiré : nouvel essai avec FB_COOKIES_JSON.")
                    await page.goto(url_group, wait_until="domcontentloaded")
                    await scraper.detecter_blocage_ou_session_expiree(page)

                graphql_url, template_body = await _capture_fresh_template(
                    page,
                    args.group_id,
                    group_name,
                )
                first_cursor = _cursor_from_post_data(template_body)
                if not first_cursor:
                    raise RuntimeError(
                        "La requête GraphQL capturée ne contient aucun curseur."
                    )

                current_cursor = str(state.get("cursor") or first_cursor)
                if state.get("cursor"):
                    print(
                        "REPRISE : curseur persistant chargé "
                        f"(pages déjà faites={state.get('pages_done', 0)})."
                    )
                else:
                    print("Nouveau parcours : départ depuis le premier curseur capturé.")

                old_confirmations = 0
                pages_this_run = 0
                target_added_this_run = 0
                oldest_seen: datetime | None = None
                if state.get("oldest_seen"):
                    try:
                        oldest_seen = datetime.fromisoformat(
                            str(state["oldest_seen"]).replace("Z", "+00:00")
                        )
                    except ValueError:
                        oldest_seen = None

                while pages_this_run < args.max_pages:
                    page_number = int(state.get("pages_done") or 0) + 1
                    posts: list[dict[str, Any]] = []
                    page_infos: list[dict[str, Any]] = []
                    info: dict[str, Any] | None = None

                    # Une réponse GraphQL peut être momentanément incomplète
                    # (page_info absent) après beaucoup de pages. Ne jamais
                    # avancer le curseur dans ce cas : on rejoue exactement
                    # la même page. Au dernier essai, on capture un gabarit
                    # GraphQL frais pour renouveler les paramètres/tokens de
                    # requête tout en conservant notre curseur persistant.
                    for tentative_page in range(1, 5):
                        if tentative_page == 4:
                            print(
                                f"Page {page_number} : rafraîchissement du gabarit "
                                "GraphQL avant dernier essai..."
                            )
                            graphql_url, template_body = await _capture_fresh_template(
                                page,
                                args.group_id,
                                group_name,
                                max_scrolls=8,
                            )

                        body = _patch_cursor(template_body, current_cursor)
                        status_code, response_body = await _fetch_graphql(
                            page,
                            graphql_url,
                            body,
                        )

                        if status_code != 200:
                            print(
                                f"Page {page_number} : HTTP {status_code}, "
                                f"nouvel essai {tentative_page}/4 sans avancer "
                                "le curseur."
                            )
                        else:
                            posts, page_infos = _aggregate_response(
                                response_body,
                                args.group_id,
                                group_name,
                            )
                            info = next(
                                (
                                    item
                                    for item in reversed(page_infos)
                                    if item.get("end_cursor")
                                ),
                                None,
                            )
                            if info is not None:
                                if tentative_page > 1:
                                    print(
                                        f"Page {page_number} récupérée au "
                                        f"{tentative_page}e essai."
                                    )
                                break

                            print(
                                f"Page {page_number} : réponse sans end_cursor/"
                                f"page_info exploitable, nouvel essai "
                                f"{tentative_page}/4 sans avancer le curseur."
                            )

                        if tentative_page < 4:
                            await asyncio.sleep(5.0 * tentative_page)

                    if info is None:
                        raise RuntimeError(
                            f"Page {page_number} impossible après 4 essais. "
                            "Arrêt prudent : le curseur sauvegardé n'a pas été "
                            "avancé, relancer la même commande reprendra cette page."
                        )

                    next_cursor = str(info["end_cursor"])
                    has_next = bool(info.get("has_next_page"))

                    dated = [
                        (post, _post_date(post))
                        for post in posts
                    ]
                    dated_known = [(post, dt) for post, dt in dated if dt is not None]

                    if dated_known:
                        page_oldest = min(dt for _, dt in dated_known)
                        oldest_seen = (
                            page_oldest
                            if oldest_seen is None
                            else min(oldest_seen, page_oldest)
                        )

                    target_posts: list[dict[str, Any]] = []
                    for post, dt in dated_known:
                        if not (start_dt <= dt < end_exclusive):
                            continue
                        post_id = str(post.get("id") or "").strip()
                        if not post_id or post_id in existing_ids:
                            continue
                        target_posts.append(post)
                        existing_ids.add(post_id)
                        raw_by_id[post_id] = post

                    # RAW local puis Neon AVANT d'avancer le curseur.
                    if target_posts:
                        _save_local_raw(raw_path, raw_by_id)
                        _persist_raw_posts(
                            conn,
                            run_id,
                            args.group_id,
                            target_posts,
                        )
                        target_added_this_run += len(target_posts)

                    if dated_known and all(dt < start_dt for _, dt in dated_known):
                        old_confirmations += 1
                    else:
                        old_confirmations = 0

                    pages_this_run += 1
                    state["pages_done"] = int(state.get("pages_done") or 0) + 1
                    state["target_posts_saved"] = (
                        int(state.get("target_posts_saved") or 0)
                        + len(target_posts)
                    )
                    state["cursor"] = next_cursor
                    state["oldest_seen"] = (
                        oldest_seen.isoformat() if oldest_seen else None
                    )
                    state["has_next_page"] = has_next
                    state["status"] = "running"
                    state["updated_at"] = datetime.now(timezone.utc).isoformat()

                    _save_local_state(state_path, state)
                    _save_neon_state(
                        conn,
                        args.group_id,
                        period_key,
                        state,
                    )

                    print(
                        f"Page {state['pages_done']} | posts={len(posts)} | "
                        f"cible+nouv={len(target_posts)} | "
                        f"cible total={state['target_posts_saved']} | "
                        f"plus ancien={state['oldest_seen']} | "
                        f"anciens confirmés={old_confirmations}/{OLD_PAGE_CONFIRMATIONS} | "
                        f"has_next={has_next}"
                    )

                    if old_confirmations >= OLD_PAGE_CONFIRMATIONS:
                        state["status"] = "complete_period_passed"
                        _save_local_state(state_path, state)
                        _save_neon_state(conn, args.group_id, period_key, state)
                        print(
                            "Période dépassée sur 5 pages consécutives : "
                            "rattrapage considéré terminé."
                        )
                        break

                    if not has_next:
                        state["status"] = "end_of_feed"
                        _save_local_state(state_path, state)
                        _save_neon_state(conn, args.group_id, period_key, state)
                        print("Facebook indique has_next_page=false : fin du fil.")
                        break

                    current_cursor = next_cursor
                    await asyncio.sleep(
                        random.uniform(args.pause_min, args.pause_max)
                    )
                else:
                    state["status"] = "paused_max_pages"
                    _save_local_state(state_path, state)
                    _save_neon_state(conn, args.group_id, period_key, state)

                print("\nRÉSUMÉ")
                print("-" * 52)
                print(f"Pages ce run              : {pages_this_run}")
                print(f"Pages cumulées            : {state.get('pages_done', 0)}")
                print(f"RAW cible ajoutés ce run  : {target_added_this_run}")
                print(f"RAW cible cumulés         : {state.get('target_posts_saved', 0)}")
                print(f"Plus ancienne date vue    : {state.get('oldest_seen')}")
                print(f"Statut                    : {state.get('status')}")
                print(f"Checkpoint local          : {state_path}")
                if conn is not None:
                    print("Checkpoint Neon           : facebook_backfill_cursor")

                if state.get("status") == "paused_max_pages":
                    print(
                        "\nRelance exactement la même commande : "
                        "le prochain run reprendra au curseur sauvegardé."
                    )

                try:
                    await scraper.sauvegarder_storage_state(context)
                except Exception:
                    pass
            finally:
                try:
                    await page.close()
                except Exception:
                    pass
                try:
                    await context.close()
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass

        return 0
    finally:
        if conn is not None:
            conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill historique Facebook par curseur GraphQL persistant."
    )
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--start-date", required=True, type=_parse_iso_date)
    parser.add_argument("--end-date", required=True, type=_parse_iso_date)
    parser.add_argument(
        "--max-pages",
        type=int,
        default=5,
        help="Nombre max de pages GraphQL par run (défaut test prudent : 5).",
    )
    parser.add_argument(
        "--pause-min",
        type=float,
        default=3.0,
        help="Pause minimale entre deux pages GraphQL.",
    )
    parser.add_argument(
        "--pause-max",
        type=float,
        default=6.0,
        help="Pause maximale entre deux pages GraphQL.",
    )
    parser.add_argument(
        "--reset-cursor",
        action="store_true",
        help="Ignore le curseur sauvegardé et repart du haut du groupe.",
    )
    args = parser.parse_args()

    if args.end_date < args.start_date:
        parser.error("--end-date doit être >= --start-date.")
    if args.max_pages < 1:
        parser.error("--max-pages doit être >= 1.")
    if args.pause_min < 0 or args.pause_max < args.pause_min:
        parser.error("Pauses invalides.")

    return args


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
