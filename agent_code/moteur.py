"""Moteur agentique : boucle « le modèle appelle des outils jusqu'à pouvoir répondre ».

- Appels d'outils natifs Ollama (avec repli si le modèle les écrit en texte).
- Sous-agents (outil Deleguer) avec leur propre contexte : l'agent principal ne reçoit que leur conclusion.
- Plan de travail visible (outil Mettre_A_Jour_Plan).
- Gestion de la fenêtre de contexte : résultats d'outils anciens raccourcis, puis résumé automatique.
- Ctrl+C interrompt proprement le tour en cours (l'historique reste cohérent).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .llm import DEBUT_APPEL, MARQUEURS_APPEL, ClientOllama, ErreurLLM, ReponseLLM, utilise_gabarit_qwen3coder
from .outils import Outil

CARACTERES_PAR_TOKEN = 3.2       # estimation prudente (code + français)
SEUIL_ALLEGEMENT = 0.70          # au-delà de 70 % de la fenêtre : on fait de la place
GARDER_MESSAGES_RECENTS = 8
TAILLE_RESULTAT_ALLEGE = 400
MAX_RAPPORT_SOUS_AGENT = 6000
MAX_RELANCES = 2
RELANCE = ("[Message automatique] Tu n'as appelé aucun outil. Si la tâche n'est pas terminée, fais MAINTENANT "
           "la prochaine action par un appel d'outil (pas de description en texte). Si elle est terminée, "
           "donne ton résumé final.")
_RX_ANNONCE = re.compile(
    r"(je vais|je vais maintenant|allons-y|commençons|je commence|voici (?:le|mon) plan|plan de travail|"
    r"étape 1|ensuite,? je|je relancerai|let me|i will|i'll|next,? i)", re.I)


_RX_DEMANDE_ACTION = re.compile(
    r"\b(corrige|corriger|répare|modifie|modifier|ajoute|ajouter|implémente|implémenter|crée|créer|écris|écrire|"
    r"refactor\w*|renomme|supprime|remplace|mets? à jour|fix|change|adapte|optimise|migre)", re.I)
_RX_BLOC_CODE = re.compile(r"```(?!\s*(?:sh|bash|shell|console|text|txt)\b)[a-zA-Z0-9_+-]*\n.{40,}?```", re.S)
_RX_RENVOI_UTILISATEUR = re.compile(
    r"(relancez|lancez|exécutez|vous pouvez (?:lancer|exécuter|relancer)|tu peux (?:lancer|relancer|exécuter))"
    r"[^.\n]{0,40}(tests?|commande|python|pytest|npm)", re.I)

RELANCE_APPLIQUER = ("[Message automatique] Tu as montré du code au lieu de modifier les fichiers. Ne montre pas le "
                     "code : lis le fichier concerné si besoin (Lire_Fichier), puis applique toi-même la modification "
                     "avec Modifier_Fichier, et vérifie.")
RELANCE_VERIFIER = ("[Message automatique] Ne demande pas à l'utilisateur de lancer la vérification : lance-la "
                    "toi-même avec tes outils (Lancer_Tests ou Executer_Commande), puis donne le résultat.")


def raison_relance(texte: str, noms_outils: list[str], *, demande: str = "", a_ecrit: bool = False,
                   a_verifie: bool = False) -> str | None:
    """Message de relance si le modèle s'est arrêté sans agir alors qu'il le devait (défaut fréquent des
    modèles locaux), sinon None."""
    t = (texte or "").strip()
    if not t or any(nom in t for nom in noms_outils if nom != "Deleguer"):
        return RELANCE
    peut_ecrire = "Modifier_Fichier" in noms_outils
    if peut_ecrire and not a_ecrit and _RX_DEMANDE_ACTION.search(demande) and _RX_BLOC_CODE.search(t):
        return RELANCE_APPLIQUER
    if (peut_ecrire or "Executer_Commande" in noms_outils) and not a_verifie and _RX_RENVOI_UTILISATEUR.search(t):
        return RELANCE_VERIFIER
    if _RX_ANNONCE.search(t[-400:]) or t.endswith(":"):
        return RELANCE
    return None


def semble_inacheve(texte: str, noms_outils: list[str]) -> bool:
    return raison_relance(texte, noms_outils) is not None


# ---------------------------------------------------------------------------
# Événements (affichage) — l'interface surcharge ce qui l'intéresse
# ---------------------------------------------------------------------------

class Evenements:
    def debut_reflexion(self) -> None: ...
    def texte(self, morceau: str) -> None: ...
    def fin_texte(self) -> None: ...
    def outil_debut(self, nom: str, args: dict) -> None: ...
    def outil_fin(self, nom: str, args: dict, resultat: str, duree: float) -> None: ...
    def plan(self, items: list[tuple[str, str]]) -> None: ...
    def info(self, message: str) -> None: ...

    def sous_agent(self, nom: str, tache: str) -> Evenements:
        return self

    def fin_sous_agent(self, nom: str, resultat: str, duree: float) -> None: ...
    def relance(self, raison: str, texte: str) -> None: ...


# ---------------------------------------------------------------------------
# Filtre d'affichage : masque les appels d'outils écrits en texte (<tool_call>…)
# ---------------------------------------------------------------------------

class FiltreAppels:
    def __init__(self, sortie: Callable[[str], None]) -> None:
        self.sortie = sortie
        self.tampon = ""
        self.bloque = False
        self.emis = False

    def _emettre(self, t: str) -> None:
        if t:
            self.emis = True
            self.sortie(t)

    def ajouter(self, morceau: str) -> None:
        if self.bloque:
            return
        self.tampon += morceau
        positions = [self.tampon.find(m) for m in MARQUEURS_APPEL if m in self.tampon]
        if positions:
            self._emettre(self.tampon[: min(positions)])
            self.tampon, self.bloque = "", True
            return
        dernier = self.tampon.rfind("<")
        if dernier >= 0 and any(m.startswith(self.tampon[dernier:]) for m in MARQUEURS_APPEL):
            self._emettre(self.tampon[:dernier])
            self.tampon = self.tampon[dernier:]
        else:
            self._emettre(self.tampon)
            self.tampon = ""

    def terminer(self) -> None:
        if not self.bloque:
            self._emettre(self.tampon)
        self.tampon = ""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

@dataclass
class ResultatTour:
    texte: str
    etapes: int = 0
    interrompu: bool = False
    erreur: str = ""


@dataclass
class ReglagesAgent:
    modele: str
    contexte: int = 32768
    temperature: float = 0.3
    max_etapes: int = 50
    keep_alive: str = "30m"
    num_predict: int = 8192

    def options(self) -> dict:
        # Réglages recommandés par Qwen pour qwen3-coder (top_p/top_k/repeat_penalty), température plus basse
        # pour la fiabilité des appels d'outils.
        return {"num_ctx": self.contexte, "temperature": self.temperature, "top_p": 0.8, "top_k": 20,
                "repeat_penalty": 1.05, "num_predict": self.num_predict}


@dataclass
class Statistiques:
    appels_llm: int = 0
    tokens_sortie: int = 0
    tokens_prompt: int = 0
    duree_llm_s: float = 0.0
    duree_generation_s: float = 0.0

    def ajouter(self, rep: ReponseLLM) -> None:
        self.appels_llm += 1
        self.tokens_sortie += rep.tokens_sortie
        self.tokens_prompt += rep.tokens_prompt
        self.duree_llm_s += rep.duree_s
        self.duree_generation_s += rep.duree_generation_s


class Agent:
    def __init__(self, client: ClientOllama, reglages: ReglagesAgent, systeme: str, outils: list[Outil], *,
                 evenements: Evenements | None = None, nom: str = "principal",
                 fabrique_sous_agent: Callable[[str], Agent | None] | None = None,
                 sous_agents: dict[str, str] | None = None) -> None:
        self.client = client
        self.reglages = reglages
        self.systeme = systeme
        self.nom = nom
        self.ev = evenements or Evenements()
        self.messages: list[dict] = []
        self.resume = ""                      # résumé des échanges compactés
        self.plan: list[tuple[str, str]] = []
        self.stats = Statistiques()
        self._fabrique = fabrique_sous_agent
        self._outils: dict[str, Outil] = {}
        self.definir_outils(outils, sous_agents or {})

    # -- outils -----------------------------------------------------------
    def definir_outils(self, outils: list[Outil], sous_agents: dict[str, str] | None = None) -> None:
        self._outils = {o.name: o for o in outils}
        self._outils["Mettre_A_Jour_Plan"] = self._outil_plan()
        if self._fabrique and sous_agents:
            self._outils["Deleguer"] = self._outil_deleguer(sous_agents)

    @property
    def noms_outils(self) -> list[str]:
        return list(self._outils)

    def _outil_plan(self) -> Outil:
        def mettre_a_jour_plan(plan: str) -> str:
            """Publie ou met à jour ton plan de travail (visible par l'utilisateur), une étape par ligne,
            préfixée par son état : « [x] fait », « [~] en cours », « [ ] à faire ».
            Utilise-le pour toute tâche de plus de 2 étapes, et mets-le à jour au fil de l'avancement."""
            items = []
            for ligne in str(plan).splitlines():
                ligne = ligne.strip().lstrip("-*0123456789.) ").strip()
                if not ligne:
                    continue
                etat = "a_faire"
                for prefixe, e in (("[x]", "fait"), ("[X]", "fait"), ("[~]", "en_cours"), ("[>]", "en_cours"),
                                   ("[ ]", "a_faire")):
                    if ligne.startswith(prefixe):
                        etat, ligne = e, ligne[len(prefixe):].strip()
                        break
                items.append((etat, ligne[:200]))
            if not items:
                return "❌ Plan vide : une étape par ligne, ex : « [ ] lire le module x »."
            self.plan = items[:30]
            self.ev.plan(self.plan)
            restantes = sum(e != "fait" for e, _ in self.plan)
            return f"✅ Plan mis à jour ({restantes} étape(s) restante(s))."
        return Outil.depuis_fonction("Mettre_A_Jour_Plan", mettre_a_jour_plan)

    def _outil_deleguer(self, sous_agents: dict[str, str]) -> Outil:
        liste = "\n".join(f"- {nom} : {desc}" for nom, desc in sous_agents.items())

        def deleguer(type_agent: str, tache: str) -> str:
            """Confie une tâche autonome à un sous-agent qui travaille dans son propre contexte et te renvoie
            seulement sa conclusion (économise ta mémoire de travail). Décris la tâche précisément : il ne
            voit pas ta conversation."""
            t = str(type_agent).strip().lower()
            if t not in sous_agents:
                return f"❌ type_agent inconnu : {type_agent!r}. Disponibles : {', '.join(sous_agents)}."
            if not str(tache).strip():
                return "❌ tache vide."
            agent = self._fabrique(t)
            if agent is None:
                return f"❌ Sous-agent {t} indisponible (web désactivé ?)."
            debut = time.monotonic()
            res = agent.tour(str(tache))
            self.stats.appels_llm += agent.stats.appels_llm
            self.stats.tokens_sortie += agent.stats.tokens_sortie
            self.stats.tokens_prompt += agent.stats.tokens_prompt
            self.stats.duree_llm_s += agent.stats.duree_llm_s
            agent.ev.fin_sous_agent(t, res.texte, time.monotonic() - debut)
            if res.interrompu:
                raise KeyboardInterrupt
            texte = res.texte.strip() or "(le sous-agent n'a rien rendu)"
            if len(texte) > MAX_RAPPORT_SOUS_AGENT:
                texte = texte[:MAX_RAPPORT_SOUS_AGENT] + "\n[... rapport tronqué ...]"
            return f"Rapport du sous-agent {t} ({res.etapes} étape(s)) :\n{texte}"

        outil = Outil.depuis_fonction("Deleguer", deleguer)
        outil.description += f"\nSous-agents disponibles (type_agent) :\n{liste}"
        outil.parametres["properties"]["type_agent"]["enum"] = list(sous_agents)
        return outil

    # -- contexte ---------------------------------------------------------
    def _systeme_complet(self) -> str:
        if not self.resume:
            return self.systeme
        return (self.systeme + "\n\n# RÉSUMÉ DES ÉCHANGES PRÉCÉDENTS (compactés pour libérer de la mémoire)\n"
                + self.resume)

    def estimer_tokens(self) -> int:
        car = len(self._systeme_complet())
        car += sum(len(m.get("content") or "") + (len(json.dumps(m["tool_calls"], ensure_ascii=False))
                                                  if m.get("tool_calls") else 0) for m in self.messages)
        car += len(json.dumps([o.schema() for o in self._outils.values()], ensure_ascii=False))
        return int(car / CARACTERES_PAR_TOKEN)

    def taux_contexte(self) -> float:
        return self.estimer_tokens() / max(1, self.reglages.contexte)

    def _alleger(self, garder: int = GARDER_MESSAGES_RECENTS, taille: int = TAILLE_RESULTAT_ALLEGE) -> int:
        """Raccourcit les anciens résultats d'outils. Retourne le nombre de messages raccourcis."""
        n = 0
        for m in self.messages[:-garder] if garder else self.messages:
            if m.get("role") == "tool" and len(m.get("content") or "") > taille:
                m["content"] = m["content"][:taille] + "\n[... résultat ancien raccourci pour libérer de la mémoire ; " \
                                                       "relance l'outil si besoin ...]"
                n += 1
        return n

    def compacter(self, consigne: str = "") -> bool:
        """Résume les anciens échanges dans le prompt système. Coupe à une frontière « user »."""
        coupures = [i for i, m in enumerate(self.messages) if m.get("role") == "user" and i > 0]
        coupures = [i for i in coupures if i <= len(self.messages) - 2] or coupures
        if not coupures:
            return False
        coupe = coupures[-1] if len(self.messages) - coupures[-1] <= GARDER_MESSAGES_RECENTS else coupures[-1]
        anciens, recents = self.messages[:coupe], self.messages[coupe:]
        transcription = []
        for m in anciens:
            role = m.get("role")
            contenu = (m.get("content") or "")[:1500]
            if m.get("tool_calls"):
                contenu += " " + ", ".join(f"[appel {tc['function']['name']}]" for tc in m["tool_calls"])
            transcription.append(f"{role.upper()} : {contenu}")
        demande = ("Résume cette conversation entre un utilisateur et un assistant de code, pour que l'assistant "
                   "puisse continuer sans elle : objectifs de l'utilisateur, décisions prises, fichiers lus ou "
                   "modifiés (chemins), état d'avancement, points en suspens. 25 lignes max, en français."
                   + (f"\nConsigne supplémentaire : {consigne}" if consigne else "")
                   + "\n\n" + (f"Résumé antérieur :\n{self.resume}\n\n" if self.resume else "")
                   + "\n".join(transcription)[-60_000:])
        try:
            rep = self.client.chat(self.reglages.modele, [{"role": "user", "content": demande}],
                                   options={**self.reglages.options(), "num_predict": 1500},
                                   keep_alive=self.reglages.keep_alive)
        except ErreurLLM as e:
            self.ev.info(f"⚠️  Résumé impossible ({e}) : anciens résultats simplement raccourcis.")
            return False
        self.stats.ajouter(rep)
        self.resume = rep.texte.strip()[:6000]
        self.messages = recents
        return True

    def _faire_de_la_place(self) -> None:
        if self.taux_contexte() < SEUIL_ALLEGEMENT:
            return
        if self._alleger():
            if self.taux_contexte() < SEUIL_ALLEGEMENT:
                return
        self.ev.info("🗜️  Mémoire de travail presque pleine : résumé des échanges précédents…")
        if not self.compacter():
            self._alleger(garder=2, taille=200)

    # -- boucle principale --------------------------------------------------
    def _peut_preremplir(self) -> bool:
        brut = getattr(self.client, "mode_brut", False)
        return brut is True or (brut is None and utilise_gabarit_qwen3coder(self.reglages.modele))

    def _appeler_llm(self, avec_outils: bool = True, forcer_appel: bool = False) -> ReponseLLM:
        self.ev.debut_reflexion()
        filtre = FiltreAppels(self.ev.texte)
        extra = {"prefixe": DEBUT_APPEL} if forcer_appel and avec_outils and self._peut_preremplir() else {}
        try:
            rep = self.client.chat(
                self.reglages.modele, [{"role": "system", "content": self._systeme_complet()}, *self.messages],
                outils=[o.schema() for o in self._outils.values()] if avec_outils else None,
                options=self.reglages.options(), sur_texte=filtre.ajouter, keep_alive=self.reglages.keep_alive,
                **extra)
        finally:
            filtre.terminer()
            self.ev.fin_texte()
        self.stats.ajouter(rep)
        return rep

    def _executer(self, nom: str, args: dict) -> str:
        outil = self._outils.get(nom)
        if outil is None:
            return f"❌ Outil inconnu : {nom}. Outils disponibles : {', '.join(self._outils)}."
        try:
            return str(outil.executer(args))
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001 — l'agent doit voir l'erreur, pas planter
            return f"❌ Erreur {type(e).__name__} dans {nom} : {str(e)[:500]}"

    def tour(self, demande: str) -> ResultatTour:
        """Traite une demande de l'utilisateur jusqu'à la réponse finale."""
        self.messages.append({"role": "user", "content": demande})
        dernieres_signatures: list[str] = []
        etape = 0
        relances = 0
        forcer = False
        a_ecrit = a_verifie = False
        try:
            while etape < self.reglages.max_etapes:
                etape += 1
                self._faire_de_la_place()
                rep = self._appeler_llm(forcer_appel=forcer)
                forcer = False
                message: dict = {"role": "assistant", "content": rep.texte}
                if rep.appels:
                    message["tool_calls"] = [a.en_message() for a in rep.appels]
                self.messages.append(message)
                if not rep.appels:
                    relance = raison_relance(rep.texte, self.noms_outils, demande=demande, a_ecrit=a_ecrit,
                                             a_verifie=a_verifie) if relances < MAX_RELANCES else None
                    if relance:
                        self.ev.relance(relance, rep.texte)
                        relances += 1
                        self.messages.append({"role": "user", "content": relance})
                        # On commence nous-mêmes sa réponse par « <tool_call> » (mode brut) : il doit agir.
                        forcer = True
                        continue
                    return ResultatTour(rep.texte, etape)
                for i, appel in enumerate(rep.appels):
                    signature = appel.nom + json.dumps(appel.arguments, sort_keys=True, ensure_ascii=False)
                    self.ev.outil_debut(appel.nom, appel.arguments)
                    debut = time.monotonic()
                    try:
                        if dernieres_signatures[-2:] == [signature, signature]:
                            resultat = ("⚠️ Tu as déjà fait exactement cet appel deux fois de suite : le résultat ne "
                                        "changera pas. Utilise ce que tu sais déjà, change d'approche, ou réponds.")
                        else:
                            resultat = self._executer(appel.nom, appel.arguments)
                    except KeyboardInterrupt:
                        self.ev.outil_fin(appel.nom, appel.arguments, "⏹️ interrompu", time.monotonic() - debut)
                        self.messages.append({"role": "tool", "tool_name": appel.nom,
                                              "content": "⛔ Interrompu par l'utilisateur."})
                        for reste in rep.appels[i + 1:]:
                            self.messages.append({"role": "tool", "tool_name": reste.nom,
                                                  "content": "⛔ Non exécuté (interruption)."})
                        raise
                    dernieres_signatures.append(signature)
                    if appel.nom in {"Ecrire_Fichier", "Modifier_Fichier"} and resultat.startswith("✅"):
                        a_ecrit = True
                    if appel.nom in {"Lancer_Tests", "Executer_Commande", "Verifier_Syntaxe", "Analyser_Qualite"}:
                        a_verifie = True
                    self.ev.outil_fin(appel.nom, appel.arguments, resultat, time.monotonic() - debut)
                    self.messages.append({"role": "tool", "tool_name": appel.nom, "content": resultat})
            # Limite d'étapes : on demande un point d'étape sans outils.
            self.messages.append({"role": "user", "content": (
                "Limite d'étapes atteinte pour cette demande. Sans appeler d'outil, résume ce qui a été fait, "
                "ce qui reste à faire, et ce dont tu as besoin pour continuer.")})
            rep = self._appeler_llm(avec_outils=False)
            self.messages.append({"role": "assistant", "content": rep.texte})
            return ResultatTour(rep.texte, etape, erreur="limite d'étapes atteinte")
        except KeyboardInterrupt:
            if self.messages and self.messages[-1].get("role") == "user":
                self.messages.append({"role": "assistant", "content": "[réponse interrompue par l'utilisateur]"})
            self.messages.append({"role": "user", "content": "[J'ai interrompu ta tâche. Attends ma prochaine consigne.]"})
            self.messages.append({"role": "assistant", "content": "D'accord, j'attends ta consigne."})
            return ResultatTour("", etape, interrompu=True)
        except ErreurLLM as e:
            if self.messages and self.messages[-1].get("role") == "user":
                self.messages.pop()   # la demande pourra être renvoyée telle quelle
            return ResultatTour("", etape, erreur=str(e))

    # -- persistance ------------------------------------------------------
    def exporter(self) -> dict:
        return {"messages": self.messages, "resume": self.resume, "plan": self.plan}

    def importer(self, donnees: dict) -> None:
        self.messages = list(donnees.get("messages") or [])
        self.resume = str(donnees.get("resume") or "")
        self.plan = [tuple(x) for x in donnees.get("plan") or []]

    def effacer(self) -> None:
        self.messages, self.resume, self.plan = [], "", []


@dataclass
class Fabrique:
    """Construit l'agent principal et ses sous-agents avec la même configuration."""
    client: ClientOllama
    reglages: ReglagesAgent
    evenements: Evenements = field(default_factory=Evenements)
