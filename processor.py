"""Étape A (filtrage regex local, gratuit) + Étape B (structuration via API Claude).

Sépare volontairement les deux étapes en fonctions indépendantes et testables :
`filtrer_candidats` ne fait aucun appel réseau (100% testable hors-ligne),
`structurer_lot` est la seule partie qui appelle l'API Claude.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from openpyxl import Workbook

from openai import AsyncOpenAI
from openai import APIConnectionError, APIStatusError, RateLimitError
from pydantic import BaseModel, ValidationError, field_validator

import config

logger = logging.getLogger("ouaga_foncier_etl.processor")

_RE_URL = re.compile(r"https?://\S+")
_RE_ESPACES = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# Étape A : nettoyage + filtrage regex (aucun coût API)
# --------------------------------------------------------------------------- #


def nettoyer_texte(texte: str | None) -> str:
    """Normalise le texte brut : retire les URLs, compresse les espaces."""
    if not texte:
        return ""
    texte = _RE_URL.sub("", texte)
    texte = _RE_ESPACES.sub(" ", texte)
    return texte.strip()


def dedupliquer_par_texte(posts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Supprime les doublons stricts (texte nettoyé identique).

    Utile car un même post republié/partagé dans plusieurs groupes suivis
    produit souvent un texte identique - inutile de payer 2x l'API pour ça.
    """
    vus: set[str] = set()
    uniques: list[dict[str, Any]] = []
    for p in posts:
        cle = p.get("texte_nettoye", "")
        if cle and cle in vus:
            continue
        if cle:
            vus.add(cle)
        uniques.append(p)
    return uniques, len(posts) - len(uniques)


def filtrer_candidats(posts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Étape A complète : nettoyage -> filtrage regex -> dédoublonnage.

    Returns:
        (candidats à envoyer au LLM, rejetés niveau 1 avec motif de rejet).
    """
    candidats: list[dict[str, Any]] = []
    rejetes: list[dict[str, Any]] = []

    for post in posts:
        texte_nettoye = nettoyer_texte(post.get("texte"))
        enrichi = {**post, "texte_nettoye": texte_nettoye}
        if not texte_nettoye:
            rejetes.append({**enrichi, "motif_rejet": "texte_vide"})
        elif config.est_candidat_foncier(texte_nettoye):
            candidats.append(enrichi)
        else:
            rejetes.append({**enrichi, "motif_rejet": "regex_niveau1"})

    candidats_dedup, nb_doublons = dedupliquer_par_texte(candidats)
    if nb_doublons:
        logger.info("Étape A : %d doublon(s) de texte supprimé(s).", nb_doublons)

    logger.info(
        "Étape A : %d posts -> %d candidats (%.1f%%), %d rejetés.",
        len(posts), len(candidats_dedup),
        100 * len(candidats_dedup) / max(len(posts), 1),
        len(rejetes) + nb_doublons,
    )
    return candidats_dedup, rejetes


# --------------------------------------------------------------------------- #
# Étape B : structuration via API OpenAI (async, Structured Outputs, schéma forcé)
# --------------------------------------------------------------------------- #


class AnnonceStructuree(BaseModel):
    """Schéma de sortie validé (voir aussi `config.SCHEMA_ANNONCE_JSON_SCHEMA` côté prompt).

    Utiliser Pydantic ici - plutôt qu'un simple `dict` non validé - permet de
    détecter immédiatement si le LLM dévie du contrat (type incohérent,
    enum invalide) au lieu de laisser une donnée corrompue silencieusement
    polluer le CSV final.
    """

    est_une_annonce_valide: bool
    type_bien: str
    quartier_zone: str | None = None
    superficie_m2: int | None = None
    prix_fcfa: int | None = None
    statut_document: str | None = None
    contacts_whatsapp: list[str] = []
    mots_cles_pertinents: list[str] = []
    resume_court: str = ""

    @field_validator("type_bien")
    @classmethod
    def _valider_type_bien(cls, v: str) -> str:
        if v not in config.TYPES_BIEN_VALIDES:
            logger.warning("type_bien inattendu du LLM : %r (conservé tel quel)", v)
        return v

    @field_validator("superficie_m2", "prix_fcfa")
    @classmethod
    def _valider_positif(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            logger.warning("Valeur numérique négative du LLM ignorée (mise à null) : %s", v)
            return None
        return v


def _construire_client(api_key: str | None = None) -> AsyncOpenAI:
    # .strip() : même piège que DATABASE_URL (voir config.py) - un secret CI
    # collé avec un retour à la ligne final casserait l'en-tête HTTP
    # Authorization envoyé par le client OpenAI.
    cle = (api_key or os.environ.get(config.ENV_OPENAI_KEY, "")).strip()
    if not cle:
        raise ValueError(f"Variable d'environnement {config.ENV_OPENAI_KEY} absente.")
    return AsyncOpenAI(api_key=cle)


class QuotaEpuiseError(Exception):
    """Levée quand OpenAI répond insufficient_quota (crédit épuisé) - erreur
    permanente qu'il est inutile de retenter, contrairement à un vrai
    rate-limit (RateLimitError générique, transitoire).
    """


async def structurer_annonce(
    client: AsyncOpenAI,
    texte: str,
    semaphore: asyncio.Semaphore,
    max_retries: int = config.LLM_MAX_RETRIES,
) -> dict[str, Any] | None:
    """Appelle l'API OpenAI pour structurer un post, avec retry/backoff exponentiel.

    Retourne None (plutôt que de lever) en cas d'échec définitif, pour que le
    traitement du lot entier ne soit pas interrompu par un seul post en erreur -
    l'appelant compte les échecs et les journalise (cf. `structurer_lot`).

    INCERTITUDE ASSUMÉE : cet appel (Structured Outputs, `response_format`
    json_schema strict) n'a pas pu être testé contre l'API OpenAI réelle -
    aucun accès réseau sortant vers api.openai.com depuis mon environnement
    (confirmé par un échec de connexion direct). La forme de l'appel est
    basée sur le contrat documenté du SDK `openai` (introspection du
    signature de `AsyncCompletions.create`, qui confirme `response_format`,
    `max_tokens`, `temperature` comme paramètres valides) - à valider par un
    run réel avec `--group-limit 1` avant tout usage à volume.
    """
    async with semaphore:
        for tentative in range(1, max_retries + 1):
            try:
                reponse = await client.chat.completions.create(
                    model=config.OPENAI_MODEL,
                    max_tokens=config.OPENAI_MAX_TOKENS,
                    temperature=config.OPENAI_TEMPERATURE,
                    response_format={
                        "type": "json_schema",
                        "json_schema": config.SCHEMA_ANNONCE_JSON_SCHEMA,
                    },
                    messages=[
                        {"role": "system", "content": config.PROMPT_SYSTEME_LLM},
                        {"role": "user", "content": texte},
                    ],
                )
                message = reponse.choices[0].message

                if message.refusal:
                    logger.error(
                        "Le modèle a refusé de structurer ce post : %s", message.refusal
                    )
                    return None
                if not message.content:
                    logger.error("Réponse LLM vide pour le texte : %.80s...", texte)
                    return None

                donnees = json.loads(message.content)
                annonce = AnnonceStructuree.model_validate(donnees)
                return annonce.model_dump()

            except RateLimitError as exc:
                # BUG CORRIGÉ le 2026-08-07 : OpenAI renvoie RateLimitError
                # (HTTP 429) pour DEUX cas très différents - un vrai
                # dépassement de débit (transitoire, le retry/backoff
                # ci-dessous est la bonne réponse) ET un quota/crédit épuisé
                # (`insufficient_quota`, permanent - retenter ne réussira
                # JAMAIS). Sans cette distinction, un compte à crédit épuisé
                # provoquait 3 tentatives ratées PAR POST, sur potentiellement
                # des centaines de posts, avant d'abandonner - confirmé en
                # conditions réelles le 2026-08-07 (run à 0/317 réussi,
                # 20 minutes perdues en retries voués à l'échec).
                code_erreur = getattr(exc, "code", None) or (
                    exc.body.get("code") if isinstance(getattr(exc, "body", None), dict) else None
                )
                if code_erreur == "insufficient_quota":
                    logger.critical(
                        "Quota/crédit OpenAI épuisé (insufficient_quota) - "
                        "arrêt immédiat, inutile de retenter les posts restants. "
                        "Rechargez le compte sur platform.openai.com/settings/organization/billing."
                    )
                    raise QuotaEpuiseError(
                        "Quota OpenAI épuisé - crédit à recharger."
                    ) from exc

                attente = config.LLM_BACKOFF_BASE_S * (2 ** (tentative - 1)) + random.uniform(0, 1)
                logger.warning(
                    "Rate limit API OpenAI (tentative %d/%d) - attente %.1fs",
                    tentative, max_retries, attente,
                )
                await asyncio.sleep(attente)
            except (APIConnectionError, APIStatusError) as exc:
                attente = config.LLM_BACKOFF_BASE_S * (2 ** (tentative - 1))
                logger.warning(
                    "Erreur API OpenAI (%s, tentative %d/%d) - attente %.1fs",
                    exc, tentative, max_retries, attente,
                )
                await asyncio.sleep(attente)
            except (json.JSONDecodeError, ValidationError) as exc:
                logger.error("Sortie LLM invalide (JSON ou schéma non respecté) : %s", exc)
                return None  # inutile de retenter : le modèle a mal répondu, pas un pb réseau

        logger.error("Échec définitif après %d tentatives pour un post.", max_retries)
        return None


async def structurer_lot(
    candidats: list[dict[str, Any]],
    api_key: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Structure tous les candidats en parallèle (borné par `LLM_MAX_CONCURRENCE`).

    Returns:
        (annonces valides et structurées, posts en échec ou jugés invalides par le LLM)
    """
    if not candidats:
        return [], []

    client = _construire_client(api_key)
    semaphore = asyncio.Semaphore(config.LLM_MAX_CONCURRENCE)

    async def _traiter(post: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        resultat = await structurer_annonce(client, post["texte_nettoye"], semaphore)
        return post, resultat

    try:
        resultats = await asyncio.gather(*(_traiter(p) for p in candidats))
    except QuotaEpuiseError:
        # Arrêt immédiat du lot entier : inutile de laisser les candidats
        # restants échouer un par un (le message détaillé a déjà été
        # journalisé dans structurer_annonce au moment de la détection).
        logger.critical(
            "Étape B interrompue : quota OpenAI épuisé, %d candidat(s) non traités.",
            len(candidats),
        )
        raise

    valides: list[dict[str, Any]] = []
    non_valides: list[dict[str, Any]] = []

    for post, structure in resultats:
        if structure is None:
            non_valides.append({**post, "motif_rejet": "echec_api_ou_validation"})
        elif not structure["est_une_annonce_valide"]:
            non_valides.append({**post, "motif_rejet": "llm_juge_invalide", **structure})
        else:
            structure["quartier_zone"] = config.normaliser_quartier(structure.get("quartier_zone"))
            structure["statut_document"] = config.normaliser_statut_document(structure.get("statut_document"))
            valides.append({**post, **structure})

    logger.info(
        "Étape B : %d candidats -> %d annonces valides, %d rejetées/échouées.",
        len(candidats), len(valides), len(non_valides),
    )
    return valides, non_valides
