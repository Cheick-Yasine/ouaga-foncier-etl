from datetime import date, datetime, timedelta, timezone

import config
from scripts import backfill_missing_period as backfill


def _groupe(group_id: str, nom: str) -> config.Groupe:
    return config.Groupe(
        id=group_id,
        nom=nom,
        url=f"https://web.facebook.com/groups/{group_id}/",
        actif=True,
    )


def test_dans_periode_inclusive_sur_les_deux_jours(monkeypatch):
    monkeypatch.setattr(
        backfill,
        "_TARGET_START",
        datetime(2026, 8, 31, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        backfill,
        "_TARGET_END_EXCLUSIVE",
        datetime(2026, 9, 7, tzinfo=timezone.utc),
    )

    assert backfill._dans_periode(
        {"date_publication": "2026-08-31T00:00:00+00:00"}
    )
    assert backfill._dans_periode(
        {"date_publication": "2026-09-06T23:59:59+00:00"}
    )
    assert not backfill._dans_periode(
        {"date_publication": "2026-08-30T23:59:59+00:00"}
    )
    assert not backfill._dans_periode(
        {"date_publication": "2026-09-07T00:00:00+00:00"}
    )


def test_choisit_le_premier_groupe_non_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill, "COVERAGE_PATH", tmp_path / "coverage.json")
    monkeypatch.setattr(backfill, "_PERIOD_KEY", "2026-08-31__2026-09-06")
    monkeypatch.setattr(
        backfill,
        "_TARGET_START",
        datetime(2026, 8, 31, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        backfill,
        "_TARGET_END_EXCLUSIVE",
        datetime(2026, 9, 7, tzinfo=timezone.utc),
    )

    groupes = [_groupe("1", "G1"), _groupe("2", "G2"), _groupe("3", "G3")]
    couverture = {
        "periods": {
            backfill._PERIOD_KEY: {
                "start_date": "2026-08-31",
                "end_date": "2026-09-06",
                "groups": {
                    "1": {"status": "complete"},
                    "2": {"status": "session_expired"},
                },
            }
        }
    }
    backfill._sauvegarder_couverture(couverture)

    choisi = backfill._choisir_prochain_groupe(groupes)
    assert choisi is not None
    assert choisi.id == "2"


def test_retourne_none_quand_tous_les_groupes_sont_complets(tmp_path, monkeypatch):
    monkeypatch.setattr(backfill, "COVERAGE_PATH", tmp_path / "coverage.json")
    monkeypatch.setattr(backfill, "_PERIOD_KEY", "2026-09-07__2026-09-13")
    monkeypatch.setattr(
        backfill,
        "_TARGET_START",
        datetime(2026, 9, 7, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        backfill,
        "_TARGET_END_EXCLUSIVE",
        datetime(2026, 9, 14, tzinfo=timezone.utc),
    )

    groupes = [_groupe("1", "G1"), _groupe("2", "G2")]
    backfill._sauvegarder_couverture(
        {
            "periods": {
                backfill._PERIOD_KEY: {
                    "start_date": "2026-09-07",
                    "end_date": "2026-09-13",
                    "groups": {
                        "1": {"status": "complete"},
                        "2": {"status": "complete"},
                    },
                }
            }
        }
    )

    assert backfill._choisir_prochain_groupe(groupes) is None
