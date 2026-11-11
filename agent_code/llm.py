"""Client Ollama natif (API /api/chat), stdlib uniquement.

Pourquoi pas l'API compatible OpenAI ? L'API native permet de régler num_ctx (fenêtre de contexte)
à chaque requête — plus besoin de variantes de modèles — et renvoie les appels d'outils structurés.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field


class ErreurLLM(Exception):
    """Erreur de communication avec Ollama (serveur absent, modèle manquant, réponse invalide)."""


@dataclass
class AppelOutil:
    nom: str
    arguments: dict
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def en_message(self) -> dict:
        return {"function": {"name": self.nom, "arguments": self.arguments}}


@dataclass
class ReponseLLM:
    texte: str
    appels: list[AppelOutil] = field(default_factory=list)
    tokens_prompt: int = 0
    tokens_sortie: int = 0
    duree_s: float = 0.0
    duree_generation_s: float = 0.0   # temps de génération seul (hors lecture du prompt)
    appels_textuels: bool = False   # appels retrouvés dans le texte (le modèle n'a pas utilisé le format natif)


# ---------------------------------------------------------------------------
# Repli : appels d'outils écrits en texte par le modèle
# ---------------------------------------------------------------------------

_RX_QWEN_XML = re.compile(r"<function=([\w\-.]+)>(.*?)</function>", re.S)
_RX_QWEN_PARAM = re.compile(r"<parameter=([\w\-.]+)>\n?(.*?)\n?</parameter>", re.S)
_RX_TOOL_CALL_JSON = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
# Appel écrit en JSON dans un bloc de code markdown, ou sur une ligne seule : {"name": "...", "arguments": {...}}
_RX_BLOC_JSON = re.compile(r"```[a-zA-Z]*\s*(\{.*?\})\s*```|^[ \t]*(\{[ \t]*\"name\"[^\n]*\})[ \t]*$", re.S | re.M)
MARQUEURS_APPEL = ("<tool_call>", "<function=")


def _appel_json(brut: str, noms: set[str]) -> AppelOutil | None:
    try:
        d = json.loads(brut, strict=False)   # strict=False : retours à la ligne bruts tolérés dans les chaînes
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    nom = d.get("name") or (d.get("function") or {}).get("name")
    args = d.get("arguments") or d.get("parameters") or (d.get("function") or {}).get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    return AppelOutil(nom, args) if nom in noms and isinstance(args, dict) else None


def _cle(a: AppelOutil) -> str:
    return a.nom + json.dumps(a.arguments, sort_keys=True, ensure_ascii=False)


def extraire_appels_xml(texte: str, noms: set[str]) -> tuple[list[AppelOutil], str]:
    """Appels au format Qwen XML (<function=nom><parameter=x>…</parameter></function>) écrits dans le texte."""
    appels = [AppelOutil(nom, {k: v for k, v in _RX_QWEN_PARAM.findall(corps)})
              for nom, corps in _RX_QWEN_XML.findall(texte) if nom in noms]
    if not appels:
        return [], texte
    debut = min((texte.find(m) for m in MARQUEURS_APPEL if m in texte), default=len(texte))
    return appels, texte[:debut].rstrip()


def extraire_appels_texte(texte: str, noms: set[str]) -> tuple[list[AppelOutil], str]:
    """Retrouve des appels d'outils écrits dans le texte au lieu du format natif : Qwen XML
    (<function=…>), JSON entre <tool_call>, ou JSON {"name": …} dans un bloc de code.
    Retourne (appels, texte nettoyé)."""
    appels, nettoye = extraire_appels_xml(texte, noms)
    if appels:
        return appels, nettoye
    appels = [a for a in (_appel_json(b, noms) for b in _RX_TOOL_CALL_JSON.findall(texte)) if a]
    if appels:
        debut = min((texte.find(m) for m in MARQUEURS_APPEL if m in texte), default=len(texte))
        return appels, texte[:debut].rstrip()
    debut_bloc = None
    for m in _RX_BLOC_JSON.finditer(texte):
        a = _appel_json(m.group(1) or m.group(2), noms)
        if a:
            appels.append(a)
            debut_bloc = m.start() if debut_bloc is None else debut_bloc
    if not appels:
        return [], texte
    # Le modèle a souvent écrit tout un plan autour : seul le premier appel est fiable (les suivants
    # dépendent de son résultat).
    return appels[:1], texte[:debut_bloc].rstrip()


# ---------------------------------------------------------------------------
# Gabarit qwen3-coder (mode « brut »)
# ---------------------------------------------------------------------------
# Pourquoi ? Avec Ollama 0.40, le rendu/analyse des appels d'outils de qwen3-coder est fragile : en streaming
# le premier appel fuit dans le texte, et l'historique n'est pas toujours rendu dans le format d'entraînement,
# si bien que le modèle dérive vers des appels écrits en JSON. En construisant nous-mêmes le prompt au format
# officiel et en analysant nous-mêmes la réponse, on contrôle tout — y compris le pré-remplissage de la
# réponse (« <tool_call> ») pour relancer un modèle qui bavarde au lieu d'agir.

SYSTEME_QWEN = "You are Qwen, a helpful AI assistant that can interact with a computer to solve tasks."
DEBUT_APPEL = "<tool_call>\n<function="
ARRETS_QWEN = ["<|im_end|>", "<|endoftext|>", "</tool_call>"]


def _rendre_outils_qwen(outils: list[dict]) -> str:
    s = "\n\n# Tools\n\nYou have access to the following functions:\n\n<tools>"
    for o in outils:
        f = o["function"]
        s += f"\n<function>\n<name>{f['name']}</name>\n<description>{f.get('description', '').strip()}</description>\n<parameters>"
        params = f.get("parameters") or {}
        for nom, prop in (params.get("properties") or {}).items():
            s += f"\n<parameter>\n<name>{nom}</name>\n<type>{prop.get('type', 'string')}</type>"
            if prop.get("description"):
                s += f"\n<description>{prop['description']}</description>"
            if prop.get("enum"):
                s += f"\n<enum>{json.dumps(prop['enum'], ensure_ascii=False)}</enum>"
            s += "\n</parameter>"
        if params.get("required"):
            s += f"\n<required>{json.dumps(params['required'])}</required>"
        s += "\n</parameters>\n</function>"
    return s + (
        "\n</tools>\n\nIf you choose to call a function ONLY reply in the following format with NO suffix:\n\n"
        "<tool_call>\n<function=example_function_name>\n<parameter=example_parameter_1>\nvalue_1\n</parameter>\n"
        "<parameter=example_parameter_2>\nThis is the value for the second parameter\nthat can span\nmultiple lines\n"
        "</parameter>\n</function>\n</tool_call>\n\n<IMPORTANT>\nReminder:\n"
        "- Function calls MUST follow the specified format: an inner <function=...></function> block must be nested "
        "within <tool_call></tool_call> XML tags\n"
        "- Required parameters MUST be specified\n"
        "- You may provide optional reasoning for your function call in natural language BEFORE the function call, "
        "but NOT after\n"
        "- If there is no function call available, answer the question like normal with your current knowledge and "
        "do not tell the user about function calls\n</IMPORTANT>")


def rendre_qwen3coder(messages: list[dict], outils: list[dict] | None, prefixe: str = "") -> str:
    """Prompt au format d'entraînement de qwen3-coder (ChatML + appels d'outils XML)."""
    systeme, reste = "", messages
    if messages and messages[0].get("role") == "system":
        systeme, reste = messages[0].get("content") or "", messages[1:]
    # Le préambule d'entraînement reste en tête : le modèle appelle bien mieux ses outils ainsi.
    tete = SYSTEME_QWEN + ("\n\n" + systeme if systeme else "")
    morceaux = [f"<|im_start|>system\n{tete}{_rendre_outils_qwen(outils) if outils else ''}<|im_end|>\n"]
    i = 0
    while i < len(reste):
        m = reste[i]
        role = m.get("role")
        if role == "tool":
            bloc = "<|im_start|>user"
            while i < len(reste) and reste[i].get("role") == "tool":
                bloc += f"\n<tool_response>\n{reste[i].get('content') or ''}\n</tool_response>"
                i += 1
            morceaux.append(bloc + "<|im_end|>\n")
            continue
        if role == "assistant":
            c = m.get("content") or ""
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                c += ("\n\n" if c else "") + f"<tool_call>\n<function={fn.get('name', '')}>\n"
                for k, v in (fn.get("arguments") or {}).items():
                    v = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                    c += f"<parameter={k}>\n{v}\n</parameter>\n"
                c += "</function>\n</tool_call>"
            morceaux.append(f"<|im_start|>assistant\n{c}<|im_end|>\n")
        else:
            morceaux.append(f"<|im_start|>{role or 'user'}\n{m.get('content') or ''}<|im_end|>\n")
        i += 1
    morceaux.append("<|im_start|>assistant\n" + prefixe)
    return "".join(morceaux)


def utilise_gabarit_qwen3coder(modele: str) -> bool:
    return "qwen3-coder" in modele.lower() and "next" not in modele.lower()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class ClientOllama:
    def __init__(self, url: str, api_key: str = "", timeout: float = 600) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        # ProxyHandler({}) : on ignore http_proxy & co — le code ne doit pas transiter par un proxy.
        self._ouvreur = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.mode_brut: bool | None = None   # None = automatique (gabarit qwen3-coder si le modèle l'est)

    def _chat_brut(self, modele: str, messages: list[dict], *, outils, options, sur_texte, keep_alive,
                   prefixe: str) -> ReponseLLM:
        opts = dict(options or {})
        opts["stop"] = list(dict.fromkeys([*(opts.get("stop") or []), *ARRETS_QWEN]))
        corps = {"model": modele, "prompt": rendre_qwen3coder(messages, outils, prefixe), "raw": True,
                 "stream": True, "keep_alive": keep_alive, "options": opts}
        morceaux = [prefixe]
        rep = ReponseLLM("")
        with self._requete("/api/generate", corps) as r:
            for ligne in r:
                if not ligne.strip():
                    continue
                try:
                    d = json.loads(ligne)
                except json.JSONDecodeError:
                    continue
                if d.get("error"):
                    raise ErreurLLM(str(d["error"]))
                morceau = d.get("response") or ""
                if morceau:
                    morceaux.append(morceau)
                    if sur_texte:
                        sur_texte(morceau)
                if d.get("done"):
                    rep.tokens_prompt = int(d.get("prompt_eval_count") or 0)
                    rep.tokens_sortie = int(d.get("eval_count") or 0)
                    rep.duree_s = (d.get("total_duration") or 0) / 1e9
                    rep.duree_generation_s = (d.get("eval_duration") or 0) / 1e9
        texte = "".join(morceaux)
        if outils and "<function=" in texte:
            if "</function>" not in texte:     # arrêt sur </tool_call> : on referme proprement
                texte = texte.rstrip() + "\n</function>"
            noms = {o["function"]["name"] for o in outils}
            appels, propre = extraire_appels_xml(texte, noms)
            if appels:
                rep.appels, rep.texte = appels, propre
                return rep
        rep.texte = texte
        if outils and not rep.appels:
            appels, propre = extraire_appels_texte(texte, {o["function"]["name"] for o in outils})
            if appels:
                rep.appels, rep.texte, rep.appels_textuels = appels, propre, True
        return rep

    def _requete(self, chemin: str, corps: dict | None = None, timeout: float | None = None):
        entetes = {"Content-Type": "application/json"}
        if self.api_key:
            entetes["Authorization"] = f"Bearer {self.api_key}"
        donnees = json.dumps(corps, ensure_ascii=False).encode() if corps is not None else None
        req = urllib.request.Request(self.url + chemin, data=donnees, headers=entetes,
                                     method="POST" if donnees is not None else "GET")
        try:
            return self._ouvreur.open(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode("utf-8", errors="replace")).get("error", "")
            except Exception:  # noqa: BLE001
                pass
            if e.code == 404 and "not found" in detail.lower():
                raise ErreurLLM(f"Modèle introuvable sur le serveur Ollama : {detail}. "
                                "Télécharge-le avec : ollama pull <modèle>") from None
            if e.code in {401, 403}:
                raise ErreurLLM(f"Accès refusé par {self.url} (HTTP {e.code}) : vérifie AGENT_CODE_OLLAMA_API_KEY.") from None
            raise ErreurLLM(f"Ollama a répondu HTTP {e.code} : {detail or e.reason}") from None
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            raison = getattr(e, "reason", e)
            raise ErreurLLM(f"Ollama injoignable sur {self.url} ({raison}). Lance-le : ollama serve") from None

    def _json(self, chemin: str, corps: dict | None = None, timeout: float = 10) -> dict:
        with self._requete(chemin, corps, timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace") or "{}")

    # -- infos ------------------------------------------------------------
    def version(self) -> str:
        return self._json("/api/version").get("version", "?")

    def modeles(self) -> list[dict]:
        return self._json("/api/tags").get("models", [])

    def details(self, modele: str) -> dict:
        return self._json("/api/show", {"model": modele})

    def present(self, modele: str) -> bool:
        noms = {m.get("name", "") for m in self.modeles()}
        return modele in noms or f"{modele}:latest" in noms

    def telecharger(self, modele: str, progression: Callable[[str, int, int], None] | None = None) -> None:
        """ollama pull, avec progression(statut, fait, total)."""
        with self._requete("/api/pull", {"model": modele, "stream": True}, timeout=3600) as r:
            for ligne in r:
                if not ligne.strip():
                    continue
                d = json.loads(ligne)
                if d.get("error"):
                    raise ErreurLLM(d["error"])
                if progression:
                    progression(d.get("status", ""), int(d.get("completed") or 0), int(d.get("total") or 0))

    # -- chat -------------------------------------------------------------
    def chat(self, modele: str, messages: list[dict], *, outils: list[dict] | None = None,
             options: dict | None = None, sur_texte: Callable[[str], None] | None = None,
             keep_alive: str = "30m", prefixe: str = "") -> ReponseLLM:
        """Envoie la conversation et lit la réponse en streaming. sur_texte(morceau) est appelé au fil de l'eau.
        `prefixe` : début imposé de la réponse (mode brut uniquement, ex : DEBUT_APPEL pour forcer un appel)."""
        if self.mode_brut is True or (self.mode_brut is None and utilise_gabarit_qwen3coder(modele)):
            return self._chat_brut(modele, messages, outils=outils, options=options, sur_texte=sur_texte,
                                   keep_alive=keep_alive, prefixe=prefixe)
        corps: dict = {"model": modele, "messages": messages, "stream": True, "keep_alive": keep_alive,
                       "options": options or {}}
        if outils:
            corps["tools"] = outils
        morceaux: list[str] = []
        rep = ReponseLLM("")
        with self._requete("/api/chat", corps) as r:
            for ligne in r:
                if not ligne.strip():
                    continue
                try:
                    d = json.loads(ligne)
                except json.JSONDecodeError:
                    continue
                if d.get("error"):
                    raise ErreurLLM(str(d["error"]))
                msg = d.get("message") or {}
                contenu = msg.get("content") or ""
                if contenu:
                    morceaux.append(contenu)
                    if sur_texte:
                        sur_texte(contenu)
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {"_brut": args}
                    rep.appels.append(AppelOutil(fn.get("name", ""), args if isinstance(args, dict) else {}))
                if d.get("done"):
                    rep.tokens_prompt = int(d.get("prompt_eval_count") or 0)
                    rep.tokens_sortie = int(d.get("eval_count") or 0)
                    rep.duree_s = (d.get("total_duration") or 0) / 1e9
        rep.texte = "".join(morceaux)
        if outils:
            noms = {o["function"]["name"] for o in outils}
            # Ollama (parseur qwen3-coder, en streaming) laisse parfois fuiter le PREMIER appel dans le texte
            # (<function=…> sans <tool_call>) alors que les suivants arrivent structurés : on fusionne, dans l'ordre.
            fuites, nettoye = extraire_appels_xml(rep.texte, noms)
            if fuites:
                vus = {_cle(a) for a in fuites}
                rep.appels = fuites + [a for a in rep.appels if _cle(a) not in vus]
                rep.texte, rep.appels_textuels = nettoye, True
            elif not rep.appels:
                appels, nettoye = extraire_appels_texte(rep.texte, noms)
                if appels:
                    rep.appels, rep.texte, rep.appels_textuels = appels, nettoye, True
        return rep
