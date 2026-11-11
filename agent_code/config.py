"""Configuration centralisée (variables d'environnement / fichier .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def parser_dotenv(texte: str) -> dict[str, str]:
    """Parse un .env simple : CLE=valeur, guillemets, commentaires de ligne et de fin de ligne."""
    res: dict[str, str] = {}
    for ligne in texte.splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#") or "=" not in ligne:
            continue
        if ligne.startswith("export "):
            ligne = ligne[len("export "):]
        cle, val = ligne.split("=", 1)
        cle, val = cle.strip(), val.strip()
        if val[:1] in {"'", '"'} and val[0] in val[1:]:
            val = val[1:val.index(val[0], 1)]            # valeur entre guillemets : # autorisé dedans
        else:
            # « 20   # commentaire » → « 20 » (un # collé à la valeur est conservé)
            for i, c in enumerate(val):
                if c == "#" and (i == 0 or val[i - 1] in " \t"):
                    val = val[:i]
                    break
            val = val.strip()
        if cle:
            res[cle] = val
    return res


def dossier_config_utilisateur() -> Path:
    """~/.config/agent_code (ou $XDG_CONFIG_HOME/agent_code)."""
    base = os.environ.get("XDG_CONFIG_HOME", "").strip() or str(Path.home() / ".config")
    return Path(base) / "agent_code"


def fichiers_config_candidats() -> list[Path]:
    """Ordre de recherche du fichier de configuration (le premier trouvé gagne).

    1. $AGENT_CODE_CONFIG (chemin explicite)
    2. .env à côté du code (installation « git clone »)
    3. ~/.config/agent_code/agent_code.env (installation pipx / uv tool, serveur)
    Le .env du PROJET CIBLE n'est jamais lu : il appartient au projet, pas à agent_code.
    """
    explicite = os.environ.get("AGENT_CODE_CONFIG", "").strip()
    if explicite:   # chemin explicite : lui seul, jamais de repli silencieux sur un autre fichier
        return [Path(explicite).expanduser()]
    candidats = []
    candidats.append(Path(__file__).resolve().parent.parent / ".env")
    candidats.append(dossier_config_utilisateur() / "agent_code.env")
    return candidats


def _charger_dotenv() -> Path | None:
    """Charge le premier fichier de config trouvé. L'environnement reste prioritaire."""
    for fichier in fichiers_config_candidats():
        if fichier.is_file():
            for cle, val in parser_dotenv(fichier.read_text(encoding="utf-8")).items():
                os.environ.setdefault(cle, val)
            return fichier
    return None


def modele_config() -> str:
    """Contenu du fichier de configuration par défaut (commenté)."""
    return (Path(__file__).resolve().parent / "defaut.env").read_text(encoding="utf-8")


def _liste(nom: str) -> tuple[tuple[str, ...], ...]:
    """« pytest, npm run build » → (("pytest",), ("npm", "run", "build"))."""
    import shlex
    res = []
    for morceau in os.environ.get(nom, "").split(","):
        try:
            argv = tuple(shlex.split(morceau))
        except ValueError:
            print(f"⚠️  {nom} : entrée ignorée ({morceau.strip()!r}).")
            continue
        if argv:
            res.append(argv)
    return tuple(res)


def _bool(nom: str, defaut: bool) -> bool:
    val = os.environ.get(nom, "").strip().lower()
    if not val:
        return defaut
    return val in {"1", "true", "oui", "yes", "on", "vrai"}


def _int(nom: str, defaut: int, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        v = int(os.environ.get(nom, defaut))
    except ValueError:
        print(f"⚠️  {nom} invalide, valeur par défaut utilisée ({defaut}).")
        return defaut
    v = max(minimum, v)
    return min(v, maximum) if maximum is not None else v


def _float(nom: str, defaut: float, minimum: float = 0.0, maximum: float = 2.0) -> float:
    try:
        v = float(os.environ.get(nom, defaut))
    except ValueError:
        print(f"⚠️  {nom} invalide, valeur par défaut utilisée ({defaut}).")
        return defaut
    return min(max(v, minimum), maximum)


MODELE_PAR_DEFAUT = "qwen3-coder:30b"
MODES = ("normal", "auto", "lecture")


@dataclass(frozen=True)
class Config:
    ollama_url: str
    modele_reflexion: str      # revue, cadrage, veille (missions)
    modele_code: str           # agent principal, écriture / correction de code
    temperature_code: float
    temperature_reflexion: float
    contexte: int              # fenêtre de contexte demandée à Ollama (num_ctx), en tokens
    max_etapes: int            # appels d'outils max par demande (anti-boucle)
    garder_modele: str         # durée de maintien du modèle en mémoire après usage (keep_alive Ollama)
    timeout_cmd: int
    timeout_llm: int           # secondes max pour une réponse du LLM
    max_sortie: int            # taille max (caractères) renvoyée par un outil
    web: bool
    auto_approuver_sures: bool
    verbose: bool
    mode: str = "normal"       # mode de la session interactive : normal | auto | lecture
    # --- exploitation (poste ou serveur) ---
    ollama_api_key: str = ""          # Ollama derrière un reverse proxy avec authentification Bearer
    ollama_de_confiance: bool = False  # Ollama distant mais maîtrisé (docker compose, LAN) : pas d'alerte
    timeout_mission: int = 0          # secondes max pour une mission entière (0 = illimité)
    autoriser_tests: bool = False     # Lancer_Tests sans validation (indispensable sans surveillance)
    commandes_autorisees: tuple[tuple[str, ...], ...] = ()  # préfixes de commandes acceptés sans validation
    autoriser_pages_web: bool = False  # Lire_Page_Web sans validation
    fichier_config: str = ""          # fichier de config effectivement chargé (diagnostic)

    @property
    def ollama_local(self) -> bool:
        hote = (urlparse(self.ollama_url).hostname or "").lower()
        return hote in {"localhost", "127.0.0.1", "::1"} or hote.endswith(".localhost")

    def entetes_ollama(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.ollama_api_key}"} if self.ollama_api_key else {}


def charger_config() -> Config:
    fichier = _charger_dotenv()

    url = os.environ.get("AGENT_CODE_OLLAMA_URL", "http://localhost:11434").strip().rstrip("/")
    if urlparse(url).scheme not in {"http", "https"} or not urlparse(url).hostname:
        print(f"⚠️  AGENT_CODE_OLLAMA_URL invalide ({url!r}) : http://localhost:11434 utilisé.")
        url = "http://localhost:11434"
    cle_api = os.environ.get("AGENT_CODE_OLLAMA_API_KEY", "").strip()
    modele = os.environ.get("AGENT_CODE_MODELE", "").strip() or MODELE_PAR_DEFAUT
    mode = os.environ.get("AGENT_CODE_MODE", "normal").strip().lower()
    if mode not in MODES:
        print(f"⚠️  AGENT_CODE_MODE invalide ({mode!r}) : « normal » utilisé.")
        mode = "normal"
    return Config(
        ollama_url=url,
        modele_reflexion=os.environ.get("AGENT_CODE_MODELE_REFLEXION", "").strip() or modele,
        modele_code=os.environ.get("AGENT_CODE_MODELE_CODE", "").strip() or modele,
        temperature_code=_float("AGENT_CODE_TEMPERATURE_CODE", 0.3),
        temperature_reflexion=_float("AGENT_CODE_TEMPERATURE_REFLEXION", 0.5),
        contexte=_int("AGENT_CODE_CONTEXTE", 32768, 4096, 262_144),
        max_etapes=_int("AGENT_CODE_MAX_ETAPES", 50, 5, 300),
        garder_modele=os.environ.get("AGENT_CODE_GARDER_MODELE", "30m").strip() or "30m",
        timeout_cmd=_int("AGENT_CODE_TIMEOUT_CMD", 120, 5),
        timeout_llm=_int("AGENT_CODE_TIMEOUT_LLM", 900, 30),
        max_sortie=_int("AGENT_CODE_MAX_SORTIE", 12_000, 2_000),
        web=_bool("AGENT_CODE_WEB", True),
        auto_approuver_sures=_bool("AGENT_CODE_AUTO_APPROUVER_SURES", True),
        verbose=_bool("AGENT_CODE_VERBOSE", True),
        mode=mode,
        ollama_api_key=cle_api,
        ollama_de_confiance=_bool("AGENT_CODE_OLLAMA_DE_CONFIANCE", False),
        timeout_mission=_int("AGENT_CODE_TIMEOUT_MISSION", 0, 0),
        autoriser_tests=_bool("AGENT_CODE_AUTORISER_TESTS", False),
        commandes_autorisees=_liste("AGENT_CODE_COMMANDES_AUTORISEES"),
        autoriser_pages_web=_bool("AGENT_CODE_AUTORISER_PAGES_WEB", False),
        fichier_config=str(fichier or ""),
    )
