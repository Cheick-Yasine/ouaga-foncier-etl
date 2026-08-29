from __future__ import annotations

import json
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
                return_value=json.dumps({"ip": "192.0.2.10", "country_code": self._pays})
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


def test_validation_du_domaine_facebook():
    assert scraper._verifier_domaine_facebook("https://m.facebook.com/groups/1") == "mobile"
    assert scraper._verifier_domaine_facebook("https://web.facebook.com/groups/1") == "comet"
    with pytest.raises(scraper.InterfaceFacebookInattendueError):
        scraper._verifier_domaine_facebook("https://example.com/login")


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
