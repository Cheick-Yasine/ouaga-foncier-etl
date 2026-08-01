"""Tests de l'Étape A (nettoyage + filtrage regex) - aucun appel réseau."""

from __future__ import annotations

import processor


class TestNettoyerTexte:
    def test_retire_les_urls(self):
        texte = "Parcelle à vendre https://exemple.com/annonce/123 contact whatsapp"
        assert "https://" not in processor.nettoyer_texte(texte)

    def test_compresse_les_espaces_multiples(self):
        assert processor.nettoyer_texte("Terrain   à    vendre\n\n\nOuaga") == "Terrain à vendre Ouaga"

    def test_texte_none_retourne_chaine_vide(self):
        assert processor.nettoyer_texte(None) == ""

    def test_texte_vide_retourne_chaine_vide(self):
        assert processor.nettoyer_texte("   ") == ""


class TestDedupliquerParTexte:
    def test_supprime_les_doublons_stricts(self):
        posts = [
            {"id": "a", "texte_nettoye": "Parcelle à vendre Ouaga 2000"},
            {"id": "b", "texte_nettoye": "Parcelle à vendre Ouaga 2000"},
            {"id": "c", "texte_nettoye": "Autre annonce différente"},
        ]
        uniques, nb_doublons = processor.dedupliquer_par_texte(posts)
        assert nb_doublons == 1
        assert [p["id"] for p in uniques] == ["a", "c"]

    def test_liste_vide(self):
        uniques, nb_doublons = processor.dedupliquer_par_texte([])
        assert uniques == []
        assert nb_doublons == 0


class TestFiltrerCandidats:
    def test_separe_correctement_candidats_et_rejetes(self, posts_bruts_exemple):
        candidats, rejetes = processor.filtrer_candidats(posts_bruts_exemple)

        ids_candidats = {c["id"] for c in candidats}
        ids_rejetes = {r["id"] for r in rejetes}

        # p1 = annonce de vente valide, p5 = doublon exact de p1 -> doit disparaître
        assert ids_candidats == {"p1"}
        # p2 = recherche d'achat pure, p3 = spam, p4 = hors-sujet
        assert {"p2", "p3", "p4"}.issubset(ids_rejetes)

    def test_les_rejetes_ont_un_motif(self, posts_bruts_exemple):
        _, rejetes = processor.filtrer_candidats(posts_bruts_exemple)
        assert all("motif_rejet" in r for r in rejetes)

    def test_candidats_ont_le_texte_nettoye(self, posts_bruts_exemple):
        candidats, _ = processor.filtrer_candidats(posts_bruts_exemple)
        assert all("texte_nettoye" in c and c["texte_nettoye"] for c in candidats)

    def test_liste_vide(self):
        candidats, rejetes = processor.filtrer_candidats([])
        assert candidats == []
        assert rejetes == []
