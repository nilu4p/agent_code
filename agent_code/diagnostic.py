"""Diagnostic de l'installation (stdlib uniquement : fonctionne même si une dépendance manque).

    agent_code --diagnostic [dossier]
"""

from __future__ import annotations

import importlib.metadata as md
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import Config, dossier_config_utilisateur

@dataclass(frozen=True)
class Constat:
    niveau: str      # "ok" | "alerte" | "erreur"
    sujet: str
    message: str

    @property
    def icone(self) -> str:
        return {"ok": "✅", "alerte": "⚠️ ", "erreur": "❌"}[self.niveau]


def _requete_ollama(cfg: Config, chemin: str, corps: dict | None = None, timeout: float = 5) -> dict:
    donnees = json.dumps(corps).encode() if corps is not None else None
    req = urllib.request.Request(cfg.ollama_url + chemin, data=donnees, method="POST" if donnees else "GET",
                                 headers={"Content-Type": "application/json", **cfg.entetes_ollama()})
    # ProxyHandler({}) : ignore http_proxy & co (comme trust_env=False) — le code ne part pas vers un proxy.
    ouvreur = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with ouvreur.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace") or "{}")


def _present(modele: str, dispo: set[str]) -> bool:
    return modele in dispo or f"{modele}:latest" in dispo


def _ram_disponible_go() -> float | None:
    try:
        for ligne in Path("/proc/meminfo").read_text().splitlines():
            if ligne.startswith("MemAvailable:"):
                return int(ligne.split()[1]) / 1e6
    except OSError:
        pass
    return None


def memoire_kv_go(infos: dict, contexte: int) -> float | None:
    """Mémoire du cache KV (f16) pour `contexte` tokens, d'après les métadonnées du modèle."""
    mi = infos.get("model_info") or {}
    arch = mi.get("general.architecture", "")
    try:
        couches = int(mi[f"{arch}.block_count"])
        tetes_kv = int(mi.get(f"{arch}.attention.head_count_kv") or mi[f"{arch}.attention.head_count"])
        dim = int(mi.get(f"{arch}.attention.key_length") or
                  int(mi[f"{arch}.embedding_length"]) // int(mi[f"{arch}.attention.head_count"]))
    except (KeyError, ValueError, ZeroDivisionError):
        return None
    return 2 * couches * tetes_kv * dim * 2 * contexte / 1e9


def etat_ollama(cfg: Config) -> list[Constat]:
    constats: list[Constat] = []
    try:
        version = _requete_ollama(cfg, "/api/version").get("version", "?")
        tags = _requete_ollama(cfg, "/api/tags")
    except urllib.error.HTTPError as e:
        indice = " (clé AGENT_CODE_OLLAMA_API_KEY ?)" if e.code in {401, 403} else ""
        return [Constat("erreur", "ollama", f"{cfg.ollama_url} répond HTTP {e.code}{indice}")]
    except Exception as e:  # noqa: BLE001
        return [Constat("erreur", "ollama",
                        f"injoignable sur {cfg.ollama_url} ({type(e).__name__}). Lance-le : ollama serve")]
    constats.append(Constat("ok", "ollama", f"v{version} sur {cfg.ollama_url}"))
    if not cfg.ollama_local and not cfg.ollama_de_confiance:
        constats.append(Constat("alerte", "confidentialité",
                                f"Ollama n'est pas local : ton code sera envoyé à {cfg.ollama_url} "
                                "(AGENT_CODE_OLLAMA_DE_CONFIANCE=true si cette machine est à toi)"))
    modeles = {m.get("name", ""): m for m in tags.get("models", [])}
    roles = [("modèle code", cfg.modele_code)]
    if cfg.modele_reflexion != cfg.modele_code:
        roles.append(("modèle réflexion", cfg.modele_reflexion))
    for role, modele in roles:
        info_tag = modeles.get(modele) or modeles.get(f"{modele}:latest")
        if info_tag is None:
            constats.append(Constat("erreur", role, f"{modele} absent → ollama pull {modele} "
                                                    "(ou lance agent_code : il propose de le télécharger)"))
            continue
        try:
            infos = _requete_ollama(cfg, "/api/show", {"model": modele}, timeout=15)
        except Exception:  # noqa: BLE001
            infos = {}
        capacites = infos.get("capabilities")
        if capacites is not None and "tools" not in capacites:
            constats.append(Constat("erreur", role, f"{modele} ne gère pas les appels d'outils : choisis un "
                                                    "modèle avec la capacité « tools » (ex : qwen3-coder:30b)"))
            continue
        taille = (info_tag.get("size") or 0) / 1e9
        kv = memoire_kv_go(infos, cfg.contexte)
        besoin = taille + (kv or 0)
        detail = f"{modele} ({taille:.1f} Go + contexte {cfg.contexte} tokens ≈ {kv:.1f} Go)" if kv \
            else f"{modele} ({taille:.1f} Go)"
        constats.append(Constat("ok", role, detail))
        ram = _ram_disponible_go()
        try:
            charges = {m.get("name") for m in _requete_ollama(cfg, "/api/ps").get("models", [])}
        except Exception:  # noqa: BLE001
            charges = set()
        if ram is not None and cfg.ollama_local and modele not in charges and besoin > ram + taille * 0.1:
            constats.append(Constat("alerte", "mémoire",
                                    f"≈ {besoin:.1f} Go nécessaires, {ram:.1f} Go disponibles maintenant : ferme des "
                                    "applications ou réduis AGENT_CODE_CONTEXTE (ex : 16384)"))
    if len(roles) > 1:
        constats.append(Constat("alerte", "modèles", "deux modèles différents : Ollama devra les alterner en "
                                                     "mémoire (lent). Un seul modèle via AGENT_CODE_MODELE est conseillé."))
    return constats


def problemes_ollama(cfg: Config) -> tuple[list[str], list[str]]:
    """(erreurs bloquantes, alertes) pour le démarrage d'une mission."""
    constats = etat_ollama(cfg)
    erreurs = [f"{c.sujet} : {c.message}" for c in constats if c.niveau == "erreur"]
    alertes = [f"{c.sujet} : {c.message}" for c in constats if c.niveau == "alerte"]
    return erreurs, alertes


def _version_paquet(nom: str) -> str | None:
    try:
        return md.version(nom)
    except md.PackageNotFoundError:
        return None


def diagnostic_complet(cfg: Config, dossier: str | None = None) -> list[Constat]:
    c: list[Constat] = []
    v = sys.version_info
    c.append(Constat("ok" if v >= (3, 11) else "erreur", "python", f"{v.major}.{v.minor}.{v.micro} ({sys.executable})"))
    c.append(Constat("ok", "configuration", cfg.fichier_config) if cfg.fichier_config else
             Constat("alerte", "configuration",
                     f"aucun fichier trouvé, valeurs par défaut (agent_code --init-config → "
                     f"{dossier_config_utilisateur() / 'agent_code.env'})"))

    git = shutil.which("git")
    if git:
        sortie = subprocess.run([git, "--version"], capture_output=True, text=True).stdout.strip()
        c.append(Constat("ok", "git", sortie))
    else:
        c.append(Constat("erreur", "git", "absent : pas de checkpoint ni d'annulation possible (apt install git)"))

    for paquet, requis, role in [("rich", True, "affichage"), ("prompt_toolkit", False, "saisie confortable"),
                                 ("httpx", False, "lecture de pages web"), ("ddgs", False, "recherche web"),
                                 ("ruff", False, "analyse statique"), ("pyyaml", False, "validation YAML"),
                                 ("pytest", False, "tests Python")]:
        ver = _version_paquet(paquet)
        if ver:
            c.append(Constat("ok", paquet, f"{ver} ({role})"))
        else:
            c.append(Constat("erreur" if requis else "alerte", paquet,
                             f"non installé ({role}) → pip install {paquet}"))

    c.extend(etat_ollama(cfg))

    if dossier:
        p = Path(dossier).expanduser()
        if not p.is_dir():
            c.append(Constat("erreur", "projet", f"{p} n'est pas un dossier"))
        else:
            p = p.resolve()
            libre = shutil.disk_usage(p).free / 1e9
            c.append(Constat("ok" if libre > 2 else "alerte", "disque", f"{libre:.1f} Go libres sur {p}"))
            if not (p / ".git").exists():
                c.append(Constat("alerte", "projet", f"{p} n'est pas un dépôt git (il sera proposé de l'initialiser)"))
            else:
                c.append(Constat("ok", "projet", f"{p} (dépôt git)"))

    if cfg.autoriser_tests or cfg.commandes_autorisees or cfg.autoriser_pages_web:
        auto = []
        if cfg.autoriser_tests:
            auto.append("tests")
        if cfg.autoriser_pages_web:
            auto.append("pages web")
        auto += [" ".join(x) for x in cfg.commandes_autorisees]
        c.append(Constat("alerte", "approbations auto", ", ".join(auto) + " (exécutés sans validation)"))
    return c


def afficher(constats: list[Constat]) -> int:
    print("\n🩺 DIAGNOSTIC D'AGENT_CODE")
    largeur = max(len(x.sujet) for x in constats)
    for x in constats:
        print(f"  {x.icone} {x.sujet:<{largeur}}  {x.message}")
    erreurs = sum(x.niveau == "erreur" for x in constats)
    alertes = sum(x.niveau == "alerte" for x in constats)
    print(f"\n{'❌' if erreurs else '✅'} {erreurs} erreur(s), {alertes} alerte(s).")
    return 1 if erreurs else 0
