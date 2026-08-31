from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import config
import scraper


class _PageGeo:
    def __init__(self, pays: str = "BF", ok: bool = True):
        self._pays = pays
        self._reponse = SimpleNamespace(ok=ok, status=200 if ok else 503)
        self.goto = AsyncMock(return_value=self._reponse)
        self.close = AsyncMock()

    def locator(self, _selecteur: str):
        return SimpleNamespace(
            inner_text=AsyncMock(
                return_value=json.dumps({"proxy": {"ip": "192.0.2.10"}, "country": {"code": self._pays}})
            )
        )


@pytest.mark.asyncio
async def test_verification_proxy_accepte_le_pays_attendu(monkeypatch):
    page = _PageGeo("BF")
    contexte = SimpleNamespace(new_page=AsyncMock(return_value=page))
    monkeypatch.setattr(
        config,
        "parametres_regionaux",
        lambda _compte: config.ParametresRegionaux(
            pays="BF", locale="fr-FR", fuseau_horaire="Africa/Ouagadougou"
        ),
    )

    await scraper.verifier_proxy_et_region(contexte, "1")

    page.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_verification_proxy_refuse_un_autre_pays(monkeypatch):
    page = _PageGeo("FR")
    contexte = SimpleNamespace(new_page=AsyncMock(return_value=page))
    monkeypatch.setattr(
        config,
        "parametres_regionaux",
        lambda _compte: config.ParametresRegionaux(
            pays="BF", locale="fr-FR", fuseau_horaire="Africa/Ouagadougou"
        ),
    )

    with pytest.raises(scraper.ProxyIncoherentError, match="attendu=BF"):
        await scraper.verifier_proxy_et_region(contexte, "1")

    page.close.assert_awaited_once()


def test_construction_post_weblite_depuis_fragments_dom():
    maintenant = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)
    fragments = [
        "Gouverneur Kologo",
        "\u200e\u200e4 min\u200e\ue001",
        "🇧🇫 Location TENGANOGO #LOYER : 75milles ... Voir plus",
        "Écrivez un commentaire public...",
    ]

    post = scraper._construire_post_weblite(
        fragments,
        "Gouverneur Kologo",
        "589699498704633",
        "Groupe immobilier",
        maintenant,
    )

    assert post is not None
    assert post["texte"] == "🇧🇫 Location TENGANOGO #LOYER : 75milles"
    assert post["date_publication"] == (
        maintenant - timedelta(minutes=4)
    ).isoformat()
    assert post["date_incertaine"] is False
    assert post["id"].startswith("weblite:")

    meme_post_plus_tard = scraper._construire_post_weblite(
        fragments,
        "Gouverneur Kologo",
        "589699498704633",
        "Groupe immobilier",
        maintenant + timedelta(minutes=1),
    )
    assert meme_post_plus_tard is not None
    assert meme_post_plus_tard["id"] == post["id"]




def test_republication_utilise_la_date_du_groupe_pas_la_date_originale():
    maintenant = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    post = scraper._construire_post_weblite(
        [
            "Agence Exemple",
            "2 h",
            "Nouvelle republication d'une annonce immobilière à Ouagadougou",
            "Publication d'origine",
            "12 août",
            "Ancienne annonce partagée avec davantage de détails sur la parcelle",
        ],
        "Agence Exemple",
        "589699498704633",
        "Groupe immobilier",
        maintenant,
    )

    assert post is not None
    assert post["date_publication"] == (
        maintenant - timedelta(hours=2)
    ).isoformat()
    assert post["date_publication_originale"] == datetime(
        2026, 8, 12, 12, 0, tzinfo=timezone.utc
    ).isoformat()


@pytest.mark.parametrize(
    "href",
    [
        "/groups/589699498704633/posts/123456789012345/",
        "/groups/589699498704633/permalink/123456789012345/",
        "/groups/589699498704633/?multi_permalinks=123456789012345",
    ],
)
def test_extrait_id_reel_depuis_permalien_weblite(href):
    identifiant, url = scraper._extraire_identifiant_url_post_weblite(
        [href], "589699498704633"
    )

    assert identifiant == "123456789012345"
    assert url.startswith("https://m.facebook.com/")


def test_lot_hors_fenetre_exige_que_tous_les_posts_soient_anciens():
    limite = datetime(2026, 8, 30, tzinfo=timezone.utc)
    ancien = {"date_publication": "2026-08-20T00:00:00+00:00"}
    recent = {"date_publication": "2026-08-31T00:00:00+00:00"}
    inconnu = {"date_publication": None}

    assert scraper._lot_entierement_hors_fenetre([ancien, ancien], limite) is True
    assert scraper._lot_entierement_hors_fenetre([ancien, recent], limite) is False
    assert scraper._lot_entierement_hors_fenetre([ancien, inconnu], limite) is False


def test_construction_post_weblite_ignore_carte_sans_texte():
    maintenant = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)

    assert (
        scraper._construire_post_weblite(
            ["Harouna Sana", "À l'instant", "Écrivez un commentaire public..."],
            "Harouna Sana",
            "589699498704633",
            "Groupe immobilier",
            maintenant,
        )
        is None
    )


def test_validation_du_domaine_facebook():
    assert scraper._verifier_domaine_facebook("https://m.facebook.com/groups/1") == "mobile"
    assert scraper._verifier_domaine_facebook("https://web.facebook.com/groups/1") == "comet"
    with pytest.raises(scraper.InterfaceFacebookInattendueError):
        scraper._verifier_domaine_facebook("https://example.com/login")


@pytest.mark.asyncio
async def test_actualise_le_fil_et_revalide_la_session(monkeypatch):
    page = SimpleNamespace(
        url="https://m.facebook.com/groups/1",
        reload=AsyncMock(),
    )
    groupe = config.Groupe(
        "1", "Groupe 1", "https://m.facebook.com/groups/1", True, "1"
    )
    verifier_session = AsyncMock()
    monkeypatch.setattr(scraper, "detecter_blocage_ou_session_expiree", verifier_session)

    await scraper._actualiser_fil_avant_scroll(page, groupe)

    page.reload.assert_awaited_once_with(wait_until="domcontentloaded")
    verifier_session.assert_awaited_once_with(page)




def _locator_libelle(noeud=None):
    nombre = 1 if noeud is not None else 0
    return SimpleNamespace(
        count=AsyncMock(return_value=nombre),
        nth=Mock(return_value=noeud),
    )


@pytest.mark.asyncio
async def test_selectionne_activite_recente_avant_actualisation(monkeypatch):
    declencheur = SimpleNamespace(
        is_visible=AsyncMock(return_value=True),
        click=AsyncMock(),
    )
    option_recente = SimpleNamespace(
        is_visible=AsyncMock(return_value=True),
        click=AsyncMock(),
    )

    def localiser(libelle, exact=True):
        assert exact is True
        if libelle == "Plus pertinentes":
            return _locator_libelle(declencheur)
        if libelle == "Activité récente":
            return _locator_libelle(option_recente)
        return _locator_libelle()

    page = SimpleNamespace(
        get_by_text=Mock(side_effect=localiser),
        wait_for_load_state=AsyncMock(),
    )
    groupe = config.Groupe(
        "1", "Groupe 1", "https://m.facebook.com/groups/1", True, "1"
    )
    monkeypatch.setattr(scraper.asyncio, "sleep", AsyncMock())

    resultat = await scraper._selectionner_activite_recente(page, groupe)

    assert resultat is True
    declencheur.click.assert_awaited_once_with(timeout=3_000)
    option_recente.click.assert_awaited_once_with(timeout=3_000)
    page.wait_for_load_state.assert_awaited_once_with(
        "domcontentloaded", timeout=5_000
    )


@pytest.mark.asyncio
async def test_tri_absent_ne_fait_pas_echouer_une_page():
    page = SimpleNamespace(
        get_by_text=Mock(return_value=_locator_libelle()),
    )
    groupe = config.Groupe(
        "page", "Page sans tri", "https://m.facebook.com/page", True, "4"
    )

    assert await scraper._selectionner_activite_recente(page, groupe) is False


class _GestionnairePlaywright:
    async def __aenter__(self):
        return SimpleNamespace()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize("erreur_second_groupe", [False, True])
async def test_navigateur_est_ferme_apres_chaque_groupe_meme_entre_batches(
    monkeypatch,
    erreur_second_groupe,
):
    groupes = [
        config.Groupe("1", "Groupe 1", "https://m.facebook.com/groups/1", True, "1"),
        config.Groupe("2", "Groupe 2", "https://m.facebook.com/groups/2", True, "1"),
    ]
    navigateurs = [SimpleNamespace(close=AsyncMock()) for _ in groupes]
    contextes = [SimpleNamespace(close=AsyncMock()) for _ in groupes]

    monkeypatch.setenv(
        "FB_COOKIES_JSON_1",
        json.dumps(
            [
                {"name": "c_user", "value": "1", "domain": ".facebook.com"},
                {"name": "xs", "value": "x", "domain": ".facebook.com"},
            ]
        ),
    )
    monkeypatch.setattr(config, "charger_groupes", lambda limite, compte: groupes)
    monkeypatch.setattr(config, "proxy_playwright", lambda compte: {"server": "http://proxy:80"})
    monkeypatch.setattr(scraper, "verifier_cooldown", lambda compte: None)
    monkeypatch.setattr(scraper, "charger_sante", lambda compte: {"niveau_confiance": 1.0})
    monkeypatch.setattr(scraper, "charger_seen_ids", lambda compte: {})
    monkeypatch.setattr(scraper, "charger_dernier_post_connu", lambda compte: {})
    monkeypatch.setattr(scraper, "_charger_cookies_caches", lambda compte: None)
    monkeypatch.setattr(scraper, "async_playwright", lambda: _GestionnairePlaywright())
    monkeypatch.setattr(
        scraper,
        "creer_navigateur",
        AsyncMock(side_effect=list(zip(navigateurs, contextes))),
    )
    monkeypatch.setattr(scraper, "verifier_proxy_et_region", AsyncMock())
    monkeypatch.setattr(scraper, "echauffement", AsyncMock())
    monkeypatch.setattr(
        scraper,
        "scraper_groupe",
        AsyncMock(
            side_effect=[
                ([], None),
                RuntimeError("échec synthétique")
                if erreur_second_groupe
                else ([], None),
            ]
        ),
    )
    monkeypatch.setattr(scraper, "sauvegarder_storage_state", AsyncMock())
    monkeypatch.setattr(scraper, "sauvegarder_seen_ids", Mock())
    monkeypatch.setattr(scraper, "sauvegarder_dernier_post_connu", Mock())
    monkeypatch.setattr(scraper, "sauvegarder_sante", Mock())
    monkeypatch.setattr(scraper.asyncio, "sleep", AsyncMock())

    await scraper.executer_scraping(
        mode="daily",
        days_back=1,
        group_limit=None,
        groups_batch_size=1,
        compte="1",
    )

    assert scraper.creer_navigateur.await_count == 2
    for contexte in contextes:
        contexte.close.assert_awaited_once()
    for navigateur in navigateurs:
        navigateur.close.assert_awaited_once()
