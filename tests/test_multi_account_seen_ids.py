from datetime import datetime, timezone

import config
import scraper


def test_seen_ids_sont_isoles_et_persistes_par_compte(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "SEEN_IDS_PATH", tmp_path / "seen_post_ids.json")

    maintenant = datetime.now(timezone.utc).isoformat()
    scraper.sauvegarder_seen_ids({"post-compte-1": maintenant}, compte="1")
    scraper.sauvegarder_seen_ids({"post-compte-2": maintenant}, compte="2")

    assert scraper.charger_seen_ids("1") == {"post-compte-1": maintenant}
    assert scraper.charger_seen_ids("2") == {"post-compte-2": maintenant}
    assert scraper.charger_seen_ids() == {}

    assert (tmp_path / "compte_1" / "seen_post_ids.json").exists()
    assert (tmp_path / "compte_2" / "seen_post_ids.json").exists()
