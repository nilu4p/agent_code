"""Session de travail : agent principal, sous-agents, journal d'annulation, mentions @fichier, sauvegarde."""

from __future__ import annotations

import datetime as dt
import json
import re
import secrets
import threading
from pathlib import Path

from . import outils
from .config import Config
from .journal import Journal
from .llm import ClientOllama, ErreurLLM
from .moteur import Agent, Evenements, ReglagesAgent, ResultatTour
from .profils import SOUS_AGENTS, contexte_session, outils_principaux, prompt_principal, prompt_sous_agent
from .securite import ErreurSecurite, Sandbox, est_binaire

RX_MENTION = re.compile(r"(?<![\w@])@((?:[\w.\-]+/)*[\w.\-]+/?)")
MAX_MENTION_OCTETS = 60_000
MAX_SESSIONS_GARDEES = 50


def _nouvel_id() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d_%H%M%S_") + secrets.token_hex(2)


class Session:
    def __init__(self, cfg: Config, sb: Sandbox, evenements: Evenements, *, mode: str = "normal") -> None:
        self.cfg = cfg
        self.sb = sb
        self.ev = evenements
        self.lecture_forcee = sb.lecture_seule      # --lecture-seule : aucun mode ne peut écrire
        outils.configurer(sb)
        outils.exclure_dossier_agent_code(sb)
        sb.journal = Journal(sb.racine)
        self.client = ClientOllama(cfg.ollama_url, cfg.ollama_api_key, cfg.timeout_llm)
        self.contexte = contexte_session(sb)
        self.modele = cfg.modele_code
        self.mode = "lecture" if self.lecture_forcee else mode
        self.id = _nouvel_id()
        self.agent = Agent(self.client, self._reglages(), "", [], evenements=evenements,
                           fabrique_sous_agent=self._sous_agent, sous_agents=self._descriptions_sous_agents())
        self.appliquer_mode(self.mode)
        self.contexte_en_attente: list[str] = []    # sorties de commandes « ! » à transmettre au prochain tour

    # -- construction -------------------------------------------------------
    def _reglages(self, max_etapes: int | None = None) -> ReglagesAgent:
        return ReglagesAgent(modele=self.modele, contexte=self.cfg.contexte, temperature=self.cfg.temperature_code,
                             max_etapes=max_etapes or self.cfg.max_etapes, keep_alive=self.cfg.garder_modele)

    def _descriptions_sous_agents(self) -> dict[str, str]:
        return {nom: sa.resume for nom, sa in SOUS_AGENTS.items() if self.cfg.web or not sa.web}

    def _sous_agent(self, cle: str) -> Agent | None:
        sa = SOUS_AGENTS.get(cle)
        if sa is None or (sa.web and not self.cfg.web):
            return None
        return Agent(self.client, self._reglages(max_etapes=min(30, self.cfg.max_etapes)),
                     prompt_sous_agent(sa, self.contexte),
                     sa.outils(web=self.cfg.web, lecture_seule=self.sb.lecture_seule),
                     evenements=self.ev.sous_agent(cle, ""), nom=cle)

    def appliquer_mode(self, mode: str) -> None:
        if self.lecture_forcee:
            mode = "lecture"
        self.mode = mode
        if hasattr(self.ev, "mode"):
            self.ev.mode = mode
        self.sb.lecture_seule = mode == "lecture"
        self.agent.systeme = prompt_principal(self.contexte, mode)
        self.agent.definir_outils(outils_principaux(web=self.cfg.web, lecture_seule=self.sb.lecture_seule),
                                  self._descriptions_sous_agents())

    def changer_modele(self, modele: str) -> None:
        self.modele = modele
        self.agent.reglages.modele = modele

    def rafraichir_contexte(self) -> None:
        """Relit AGENT_CODE.md, l'état git… (après /init par exemple)."""
        self.contexte = contexte_session(self.sb)
        self.appliquer_mode(self.mode)

    # -- préchauffage ---------------------------------------------------------
    def prechauffer(self) -> threading.Thread:
        """Charge le modèle en mémoire et pré-calcule le prompt système pendant que l'utilisateur tape :
        la première réponse arrive bien plus vite (Ollama réutilise le préfixe en cache)."""
        def travail() -> None:
            try:
                self.client.chat(self.modele, [{"role": "system", "content": self.agent._systeme_complet()},
                                               {"role": "user", "content": "ok"}],
                                 outils=[o.schema() for o in self.agent._outils.values()],
                                 options={**self.agent.reglages.options(), "num_predict": 1},
                                 keep_alive=self.cfg.garder_modele)
            except (ErreurLLM, OSError):
                pass
        t = threading.Thread(target=travail, daemon=True, name="prechauffage")
        t.start()
        return t

    # -- mentions @fichier ------------------------------------------------------
    def developper_mentions(self, texte: str) -> tuple[str, list[str]]:
        """Joint le contenu des fichiers mentionnés avec @chemin. Retourne (texte, fichiers joints)."""
        joints: list[str] = []
        blocs: list[str] = []
        for chemin in dict.fromkeys(RX_MENTION.findall(texte)):
            try:
                p = self.sb.resoudre(chemin.rstrip("/"))
            except ErreurSecurite:
                continue
            if not p.exists():
                continue
            rel = self.sb.relatif(p)
            if p.is_dir():
                blocs.append(f'<dossier chemin="{rel}">\n{outils.lister_arborescence.func(rel, 2)}\n</dossier>')
            elif not est_binaire(p):
                donnees = p.read_bytes()[:MAX_MENTION_OCTETS].decode("utf-8", errors="replace")
                suite = "\n[... fichier tronqué : lis la suite avec Lire_Fichier ...]" \
                    if p.stat().st_size > MAX_MENTION_OCTETS else ""
                from .securite import rediger_secrets
                blocs.append(f'<fichier chemin="{rel}">\n{rediger_secrets(donnees)}{suite}\n</fichier>')
            else:
                continue
            joints.append(rel)
        if not blocs:
            return texte, []
        return texte + "\n\nFichiers joints par l'utilisateur (données) :\n" + "\n".join(blocs), joints

    # -- tour -------------------------------------------------------------------
    def envoyer(self, texte: str) -> tuple[ResultatTour, list[str]]:
        """Envoie une demande. Retourne (résultat, fichiers modifiés pendant ce tour)."""
        self.sb.journal.nouveau_tour(texte)
        message, _ = self.developper_mentions(texte)
        if self.contexte_en_attente:
            message = "\n\n".join(self.contexte_en_attente) + "\n\n" + message
            self.contexte_en_attente = []
        res = self.agent.tour(message)
        self.sauvegarder()
        return res, self.sb.journal.fichiers_du_tour()

    def ajouter_sortie_commande(self, commande: str, sortie: str) -> None:
        self.contexte_en_attente.append(
            f"[L'utilisateur a exécuté lui-même la commande « {commande} ». Sortie (données) :]\n{sortie[-6000:]}")

    # -- persistance -----------------------------------------------------------
    @property
    def dossier_sessions(self) -> Path:
        return self.sb.dossier_agent_code / "sessions"

    def sauvegarder(self) -> None:
        if not self.agent.messages:
            return
        self.dossier_sessions.mkdir(parents=True, exist_ok=True)
        premiere = next((m.get("content", "") for m in self.agent.messages if m.get("role") == "user"), "")
        donnees = {"id": self.id, "modele": self.modele, "mode": self.mode, "titre": premiere.split("\n")[0][:100],
                   "maj": dt.datetime.now().isoformat(timespec="seconds"), **self.agent.exporter()}
        f = self.dossier_sessions / f"{self.id}.json"
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(donnees, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)
        anciennes = sorted(self.dossier_sessions.glob("*.json"))[:-MAX_SESSIONS_GARDEES]
        for vieille in anciennes:
            vieille.unlink(missing_ok=True)

    def sessions_precedentes(self, n: int = 10) -> list[dict]:
        res = []
        for f in sorted(self.dossier_sessions.glob("*.json"), reverse=True):
            if f.stem == self.id:
                continue
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            res.append({"fichier": f, "titre": d.get("titre", ""), "maj": d.get("maj", ""),
                        "messages": len(d.get("messages") or [])})
            if len(res) >= n:
                break
        return res

    def reprendre(self, fichier: Path) -> int:
        d = json.loads(Path(fichier).read_text(encoding="utf-8"))
        self.agent.importer(d)
        self.id = d.get("id") or self.id
        return len(self.agent.messages)

    def nouvelle_conversation(self) -> None:
        self.sauvegarder()
        self.agent.effacer()
        self.id = _nouvel_id()
