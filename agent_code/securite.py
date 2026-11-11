"""Couche de sécurité d'agent_code (stdlib uniquement, testable sans dépendance).

Principes :
  * Bac à sable : tout chemin est résolu (symlinks compris) et doit rester
    sous la racine du workspace — y compris les arguments des commandes.
  * Moindre privilège : fichiers et dossiers sensibles illisibles, zones
    protégées non modifiables, mode lecture seule possible.
  * Pas de shell : les commandes sont découpées avec shlex et exécutées
    sans `shell=True` → aucune injection via `;`, `|`, `$()`, etc.
  * Politique de commandes : BLOQUÉE / AUTO (liste blanche) / CONFIRMER.
  * Environnement nettoyé : les variables contenant des secrets (et l'agent
    SSH) ne sont jamais transmises aux sous-processus.
  * Processus maîtrisés : groupe de processus dédié, tué entièrement au
    timeout (pas de petits-enfants orphelins qui bloquent agent_code).
  * Rédaction : les secrets détectés dans les sorties sont masqués avant
    d'être renvoyés au LLM ou écrits dans le journal.
  * Journal d'audit : chaque action sensible est tracée dans .agent_code/audit.log
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ErreurSecurite(Exception):
    """Levée quand une action viole la politique de sécurité."""


# ---------------------------------------------------------------------------
# Fichiers & dossiers
# ---------------------------------------------------------------------------

# Dossiers ignorés lors des parcours (perf + bruit).
DOSSIERS_IGNORES = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build",
    ".idea", ".vscode", ".next", ".nuxt", "target", ".gradle", ".agent_code",
    "site-packages", ".cache", "coverage", "htmlcov",
}

# Fichiers dont le contenu ne doit JAMAIS être envoyé au LLM.
MOTIFS_SENSIBLES = [
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks",
    "*.keystore", "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*", ".netrc",
    ".pgpass", ".npmrc", ".pypirc", "credentials", "credentials.*",
    "*secret*", "*.kdbx", ".htpasswd", "*.tfstate", "*.tfstate.*", "*.tfvars",
    ".git-credentials", "*.ovpn", "*.asc", "*.gpg",
]
# Exceptions : fichiers d'exemple légitimes.
MOTIFS_SENSIBLES_AUTORISES = [".env.example", ".env.sample", ".env.template", "*.example"]
# Dossiers dont tout le contenu est sensible (où qu'ils soient dans le projet).
DOSSIERS_SENSIBLES = {".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".gcloud", ".password-store",
                      "secrets", ".secrets"}

# Chemins que les agents ne peuvent jamais modifier (n'importe quel niveau).
# .vscode/.idea : tâches exécutées à l'ouverture du dossier ; .envrc : exécuté par direnv.
ZONES_PROTEGEES_ECRITURE = {".git", ".agent_code", ".venv", "venv", "node_modules", ".vscode", ".idea", ".envrc"}

EXTENSIONS_BINAIRES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".tar", ".7z", ".rar", ".so", ".dll", ".exe", ".bin", ".pyc", ".o", ".a",
    ".class", ".jar", ".woff", ".woff2", ".ttf", ".mp3", ".mp4", ".sqlite", ".db",
    ".bz2", ".xz", ".whl", ".parquet", ".pkl", ".pt", ".onnx", ".gguf", ".safetensors",
}


def _nom_sensible(nom: str) -> bool:
    nom = nom.lower()
    if any(fnmatch.fnmatch(nom, m) for m in MOTIFS_SENSIBLES_AUTORISES):
        return False
    return any(fnmatch.fnmatch(nom, m) for m in MOTIFS_SENSIBLES)


def est_sensible(chemin: Path | str) -> bool:
    """Vrai si le fichier, ou l'un de ses dossiers parents, est sensible."""
    p = Path(chemin)
    if _nom_sensible(p.name):
        return True
    return any(part.lower() in DOSSIERS_SENSIBLES for part in p.parts[:-1])


def est_binaire(chemin: Path) -> bool:
    if chemin.suffix.lower() in EXTENSIONS_BINAIRES:
        return True
    try:
        with open(chemin, "rb") as f:
            return b"\0" in f.read(4096)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Rédaction des secrets
# ---------------------------------------------------------------------------

MARQUEUR_MASQUE = "***MASQUÉ***"

_MOTIFS_SECRETS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                      # AWS
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),            # GitHub
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),             # GitLab
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),                # OpenAI / Anthropic…
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"),         # Slack
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),                # Google
    re.compile(r"\bhf_[A-Za-z0-9]{30,}\b"),                   # Hugging Face
    re.compile(r"\b(?:sk|rk|pk)_live_[A-Za-z0-9]{20,}\b"),    # Stripe
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),  # JWT
]
# clé=valeur  (password=..., api_key: "...", TOKEN=...)
_MOTIF_CLE_VALEUR = re.compile(
    r"(?i)\b([\w\-]*(?:password|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key)[\w\-]*)"
    r"(\s*[:=]\s*)(['\"]?)([^\s'\"]{6,})(\3)"
)
# URL avec identifiants : scheme://user:pass@host
_MOTIF_URL = re.compile(r"(?i)\b([a-z][a-z0-9+\-.]*://[^\s:/@]+:)([^\s@/]{3,})(@)")


def rediger_secrets(texte: str) -> str:
    """Masque les secrets connus dans un texte."""
    if not texte:
        return texte
    for motif in _MOTIFS_SECRETS:
        texte = motif.sub("***SECRET-MASQUÉ***", texte)
    texte = _MOTIF_CLE_VALEUR.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{MARQUEUR_MASQUE}{m.group(5)}", texte)
    texte = _MOTIF_URL.sub(lambda m: f"{m.group(1)}{MARQUEUR_MASQUE}{m.group(3)}", texte)
    return texte


def contient_masque(texte: str) -> bool:
    """Vrai si le texte contient un secret masqué (recopié depuis une sortie d'outil)."""
    return MARQUEUR_MASQUE in texte or "***SECRET-MASQUÉ***" in texte


def tronquer(texte: str, max_car: int) -> str:
    """Tronque une sortie trop longue en gardant le début et la fin."""
    if len(texte) <= max_car:
        return texte
    moitie = max_car // 2
    coupe = len(texte) - max_car
    return f"{texte[:moitie]}\n\n[... {coupe} caractères tronqués ...]\n\n{texte[-moitie:]}"


_VARS_RETIREES = {
    "SSH_AUTH_SOCK", "SSH_AGENT_PID", "GPG_AGENT_INFO", "KUBECONFIG", "AWS_PROFILE",
    "DOCKER_CONFIG", "DOCKER_HOST", "GIT_ASKPASS", "SSH_ASKPASS", "DBUS_SESSION_BUS_ADDRESS",
}


def env_nettoye() -> dict[str, str]:
    """Copie de l'environnement sans variables contenant des secrets ni accès aux agents d'identité."""
    motif = re.compile(r"(?i)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|SESSION|PRIVATE)")
    garder = {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "USER", "SHELL", "TMPDIR",
              "VIRTUAL_ENV", "PYTHONPATH", "NODE_PATH", "JAVA_HOME", "GOPATH"}
    env = {k: v for k, v in os.environ.items()
           if k in garder or (k not in _VARS_RETIREES and not motif.search(k))}
    env["GIT_TERMINAL_PROMPT"] = "0"   # jamais de prompt git bloquant
    env["GIT_PAGER"] = "cat"
    env["PAGER"] = "cat"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["CI"] = env.get("CI", "1")     # outils non interactifs (npm, jest…)
    return env


# ---------------------------------------------------------------------------
# Politique de commandes
# ---------------------------------------------------------------------------

class Verdict(Enum):
    BLOQUEE = "bloquée"
    AUTO = "auto"
    CONFIRMER = "confirmer"


# Exécutables interdits quoi qu'il arrive (dont les « lanceurs » qui permettraient
# de contourner cette liste : timeout sudo …, nice bash …).
EXECUTABLES_INTERDITS = {
    "sudo", "su", "doas", "pkexec", "dd", "mkfs", "fdisk", "parted", "mount",
    "umount", "shutdown", "reboot", "halt", "poweroff", "init", "systemctl",
    "chown", "chgrp", "passwd", "useradd", "userdel", "usermod", "crontab",
    "iptables", "nft", "ufw", "nc", "ncat", "netcat", "socat", "telnet",
    "ssh", "scp", "sftp", "rsync", "ftp", "eval", "exec", "sh", "bash", "zsh",
    "fish", "dash", "ksh", "csh", "tcsh", "xargs", "env", "nohup", "setsid",
    "screen", "tmux", "at", "batch", "kill", "killall", "pkill",
    # lanceurs / wrappers
    "timeout", "nice", "ionice", "stdbuf", "time", "command", "builtin", "chroot",
    "unshare", "nsenter", "runuser", "flock", "watch", "script", "busybox", "sg",
    "newgrp", "strace", "ltrace", "gdb", "faketime", "firejail",
    # disques / système
    "shred", "wipefs", "mkswap", "swapon", "swapoff", "modprobe", "insmod", "rmmod",
}
# Variantes / préfixes (mkfs.ext4, etc.)
PREFIXES_INTERDITS = ("mkfs.",)

# Commandes (préfixes de tokens) sans effet de bord notable → auto-approuvables.
COMMANDES_SURES = [
    ["ls"], ["pwd"], ["cat"], ["head"], ["tail"], ["wc"], ["tree"], ["file"],
    ["which"], ["grep"], ["rg"], ["find"], ["diff"], ["echo"], ["stat"], ["du"],
    ["git", "status"], ["git", "diff"], ["git", "log"], ["git", "show"],
    ["git", "branch"], ["git", "blame"], ["git", "ls-files"],
    ["python", "--version"], ["python3", "--version"], ["node", "--version"],
    ["npm", "--version"], ["ruff", "check"], ["ruff", "format", "--check"],
    ["mypy"], ["eslint"], ["tsc", "--noEmit"], ["cargo", "check"], ["go", "vet"],
]
# Arguments qui rendent une commande « sûre » dangereuse (écriture, exécution…).
ARGS_DANGEREUX = {
    "-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls",
    "--output", "-o", "--fix", "--unsafe-fixes", "--ext-diff", "--textconv",
    "--pre", "--pre-glob",  # rg --pre exécute un programme
}
PREFIXES_ARGS_DANGEREUX = ("--output=", "--pre=", "--fix")
# `git branch` n'est sûr que pour lister.
ARGS_GIT_BRANCH_MUTANTS = {
    "-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-f", "--force",
    "-u", "--set-upstream-to", "--unset-upstream", "--edit-description", "-t", "--track",
}
SOUS_COMMANDES_GIT_INTERDITES = {
    "push", "remote", "config", "credential", "filter-branch", "filter-repo",
    "clean", "gc", "prune", "reflog", "update-ref", "worktree", "stash", "replace",
    "daemon", "send-email", "instaweb", "fsck", "hook",
}
OPTIONS_CURL_ENVOI = ("-d", "--data", "-F", "--form", "-T", "--upload-file", "-X", "--request",
                      "--json", "--post-data", "--post-file", "--body-data", "--body-file", "--method")

CARACTERES_SHELL = set(";|&><`$\n\r\\")

# Exclusions ajoutées automatiquement à grep/rg récursifs : pas de fuite de .env & co.
_EXCLUSIONS_RECHERCHE = [m for m in MOTIFS_SENSIBLES if "/" not in m]


@dataclass
class AnalyseCommande:
    verdict: Verdict
    raison: str
    argv: list[str] = field(default_factory=list)
    # Vrai si la commande ne peut JAMAIS être approuvée par une liste blanche
    # (code inline, arguments qui écrivent ou exécutent) : validation humaine obligatoire.
    sensible: bool = False


def _verifier_argument(a: str, racine: Path) -> str | None:
    """Retourne une raison de blocage si l'argument désigne un chemin interdit."""
    valeurs = [a]
    if a.startswith("-") and "=" in a:          # --file=/etc/passwd, --git-dir=..
        valeurs = [a.split("=", 1)[1]]
    elif a.startswith("-"):
        return None
    for v in valeurs:
        if not v:
            continue
        # « HEAD:chemin » (git show) → on vérifie aussi la partie chemin
        candidats = [v] + ([v.split(":", 1)[1]] if ":" in v and "://" not in v else [])
        for c in candidats:
            if not c:
                continue
            if est_sensible(c):
                return f"accès à un fichier sensible interdit : {a}"
            brut = Path(os.path.expanduser(c))
            cible = brut if brut.is_absolute() else racine / brut
            try:
                # resolve() suit les liens symboliques : « cat lien » vers /etc est bloqué.
                resolu = cible.resolve()
            except (OSError, RuntimeError):
                continue
            if not resolu.is_relative_to(racine):
                return f"argument hors du bac à sable : {a}"
            if est_sensible(resolu.relative_to(racine)):
                return f"accès à un fichier sensible interdit : {a}"
    return None


def _durcir_recherche(exe: str, argv: list[str]) -> list[str]:
    """Ajoute à grep/rg récursifs des exclusions pour ne jamais afficher les fichiers sensibles."""
    if exe == "grep" and any(re.fullmatch(r"-[a-zA-Z]*[rR][a-zA-Z]*|--recursive|--dereference-recursive", a)
                             for a in argv[1:]):
        extra = [f"--exclude={m}" for m in _EXCLUSIONS_RECHERCHE]
        extra += [f"--exclude-dir={d}" for d in sorted(DOSSIERS_SENSIBLES | {".git", ".agent_code", "node_modules", ".venv"})]
        return [argv[0], *extra, *argv[1:]]
    if exe == "rg":
        extra: list[str] = []
        for m in _EXCLUSIONS_RECHERCHE + [f"{d}/**" for d in sorted(DOSSIERS_SENSIBLES)] + [".agent_code/**"]:
            extra += ["-g", f"!{m}"]
        return [argv[0], *extra, *argv[1:]]
    return argv


def analyser_commande(commande: str, racine: Path) -> AnalyseCommande:
    """Classe une commande selon la politique de sécurité."""
    commande = commande.strip()
    if not commande:
        return AnalyseCommande(Verdict.BLOQUEE, "commande vide")

    meta = CARACTERES_SHELL & set(commande)
    if meta:
        return AnalyseCommande(
            Verdict.BLOQUEE,
            f"caractères shell interdits {sorted(meta)} : pas de pipes, redirections, "
            "substitutions ni chaînage. Lance une seule commande simple.",
        )
    try:
        argv = shlex.split(commande)
    except ValueError as e:
        return AnalyseCommande(Verdict.BLOQUEE, f"commande mal formée : {e}")
    if not argv:
        return AnalyseCommande(Verdict.BLOQUEE, "commande vide")

    exe = os.path.basename(argv[0])
    args = argv[1:]
    if exe in EXECUTABLES_INTERDITS or exe.startswith(PREFIXES_INTERDITS):
        return AnalyseCommande(Verdict.BLOQUEE, f"exécutable interdit : {exe}")
    if "/" in argv[0] or argv[0].startswith("."):
        # ./script.sh, /usr/bin/x, sous/dossier/outil : on vérifie que c'est dans le bac à sable
        raison = _verifier_argument(argv[0], racine)
        if raison:
            return AnalyseCommande(Verdict.BLOQUEE, raison)

    # rm récursif/forcé, chmod permissif
    if exe == "rm" and any(a.startswith("-") and ("r" in a.lower() or "f" in a) for a in args):
        return AnalyseCommande(Verdict.BLOQUEE, "rm récursif/forcé interdit (utilise git pour annuler)")
    if exe == "chmod" and any(
        (re.fullmatch(r"[0-7]{3,4}", a) and int(a[-1]) & 2)          # écriture pour "others"
        or (re.fullmatch(r"[0-7]{4}", a) and int(a[0]) & 6)          # setuid / setgid
        or any(re.fullmatch(r"[ugoa]*[+=][rwxXst]*", m)
               and ("s" in m or ("w" in m and (re.match(r"[ugo]*[oa]", m) or m[0] in "+=")))
               for m in a.split(","))
        for a in args
    ):
        return AnalyseCommande(Verdict.BLOQUEE, "permissions trop larges interdites")
    if exe == "git":
        # git -c x=y / --exec-path / -C : permettent d'exécuter du code ou de sortir du projet
        globales = {"-c", "-C", "--exec-path", "--config-env", "--git-dir", "--work-tree", "--namespace"}
        if any(a in globales or a.startswith(tuple(f"{g}=" for g in globales if g.startswith("--")))
               for a in args):
            return AnalyseCommande(Verdict.BLOQUEE, "options globales git (-c, -C, --git-dir…) interdites")
        sous = next((a for a in args if not a.startswith("-")), "")
        if sous in SOUS_COMMANDES_GIT_INTERDITES:
            return AnalyseCommande(Verdict.BLOQUEE, f"git {sous} interdit aux agents (à faire toi-même)")
        if sous == "reset" and "--hard" in args:
            return AnalyseCommande(Verdict.BLOQUEE, "git reset --hard interdit (l'annulation est gérée par agent_code)")
    if exe in {"curl", "wget"} and any(
        a == o or (o.startswith("--") and a.split("=", 1)[0].startswith(o))   # --data-binary, --data-raw…
        or (len(o) == 2 and a.startswith(o) and not a.startswith("--"))          # -dvaleur
        for a in args for o in OPTIONS_CURL_ENVOI
    ):
        return AnalyseCommande(Verdict.BLOQUEE, "envoi de données réseau interdit (risque d'exfiltration)")

    # Arguments pointant hors du workspace ou vers des fichiers sensibles
    for a in args:
        raison = _verifier_argument(a, racine)
        if raison:
            return AnalyseCommande(Verdict.BLOQUEE, raison)

    argv = _durcir_recherche(exe, argv)

    if exe in {"python", "python3", "node", "perl", "ruby", "php", "deno", "bun"} and any(
            a in {"-c", "-e", "--eval", "-p", "--print"} for a in args):
        return AnalyseCommande(Verdict.CONFIRMER, "exécution de code inline : vérifie le contenu", argv,
                               sensible=True)

    dangereux = any(a in ARGS_DANGEREUX or a.startswith(PREFIXES_ARGS_DANGEREUX) for a in args)
    if exe == "git" and args[:1] == ["branch"] and any(
            a in ARGS_GIT_BRANCH_MUTANTS or a.startswith(("--set-upstream-to=", "--track=")) for a in args[1:]):
        dangereux = True
    if exe == "git" and args[:1] == ["branch"] and any(not a.startswith("-") for a in args[1:]) \
            and not any(a in {"-l", "--list", "-a", "-r", "--contains", "--merged", "--no-merged", "--points-at"}
                        for a in args[1:]):
        dangereux = True  # « git branch nom » crée une branche
    if not dangereux:
        for prefixe in COMMANDES_SURES:
            # correspondance exacte de argv[0] : un « ./ls » écrit par un agent n'est pas « ls »
            if argv[: len(prefixe)] == prefixe:
                return AnalyseCommande(Verdict.AUTO, "commande en lecture seule", argv)

    return AnalyseCommande(Verdict.CONFIRMER, "commande avec effets de bord possibles", argv, sensible=dangereux)


def correspond_liste_blanche(argv: list[str], liste: tuple[tuple[str, ...], ...] | list) -> bool:
    """Vrai si argv commence exactement par l'un des préfixes autorisés (comparaison token par token).
    « pytest » autorise « pytest -q tests/ » ; « ./pytest » ou « pytest-evil » ne correspondent pas."""
    return any(len(p) > 0 and tuple(argv[: len(p)]) == tuple(p) for p in liste)


# ---------------------------------------------------------------------------
# Exécution de processus
# ---------------------------------------------------------------------------

MAX_OCTETS_CAPTURE = 2_000_000  # par flux : au-delà, seuls le début et la fin sont gardés


def _lire_borne(f) -> str:
    """Lit un fichier temporaire de sortie en gardant au plus MAX_OCTETS_CAPTURE octets."""
    f.seek(0, os.SEEK_END)
    taille = f.tell()
    f.seek(0)
    if taille <= MAX_OCTETS_CAPTURE:
        return f.read().decode("utf-8", errors="replace")
    moitie = MAX_OCTETS_CAPTURE // 2
    debut = f.read(moitie)
    f.seek(taille - moitie)
    fin = f.read()
    return (debut.decode("utf-8", errors="replace")
            + f"\n[... {taille - 2 * moitie} octets ignorés ...]\n"
            + fin.decode("utf-8", errors="replace"))


def _tuer_groupe(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # pragma: no cover
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


def executer_processus(argv: list[str], cwd: Path, timeout: int) -> tuple[int | None, str, str]:
    """Exécute argv sans shell. Retourne (code retour | None si timeout, stdout, stderr).

    * Sorties redirigées vers des fichiers temporaires : un petit-enfant qui garde
      le tuyau ouvert ne peut pas bloquer agent_code.
    * Nouveau groupe de processus, entièrement tué en cas de timeout.
    """
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            argv, cwd=cwd, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
            env=env_nettoye(), start_new_session=(os.name == "posix"),
        )
        try:
            code: int | None = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _tuer_groupe(proc)
            proc.wait()
            code = None
        except BaseException:  # Ctrl+C : on ne laisse rien tourner derrière nous
            _tuer_groupe(proc)
            proc.wait()
            raise
        else:
            # Le parent a fini : on nettoie d'éventuels démons restés dans le groupe.
            _tuer_groupe(proc)
        return code, _lire_borne(out), _lire_borne(err)


# ---------------------------------------------------------------------------
# Bac à sable
# ---------------------------------------------------------------------------

TAILLE_MAX_AUDIT = 5_000_000  # rotation du journal d'audit au-delà (octets)


class Sandbox:
    """Encapsule toutes les règles d'accès au workspace."""

    def __init__(
        self,
        racine: str | Path,
        *,
        lecture_seule: bool = False,
        auto_approuver_sures: bool = True,
        timeout_cmd: int = 120,
        max_sortie: int = 12_000,
        interactif: bool | None = None,
        autoriser_tests: bool = False,
        commandes_autorisees: tuple[tuple[str, ...], ...] = (),
        autoriser_pages_web: bool = False,
    ) -> None:
        self.racine = Path(racine).expanduser().resolve(strict=True)
        if not self.racine.is_dir():
            raise ErreurSecurite(f"{self.racine} n'est pas un dossier")
        if self.racine == Path(self.racine.anchor) or self.racine == Path.home().resolve():
            raise ErreurSecurite("Refus : le bac à sable ne peut pas être / ou ton $HOME entier.")
        if est_sensible(self.racine / "x"):
            raise ErreurSecurite(f"Refus : {self.racine} est un dossier sensible (.ssh, .aws…).")
        self.lecture_seule = lecture_seule
        self.auto_approuver_sures = auto_approuver_sures
        self.timeout_cmd = timeout_cmd
        self.max_sortie = max_sortie
        self.interactif = sys.stdin.isatty() if interactif is None else interactif
        self.autoriser_tests = autoriser_tests
        self.commandes_autorisees = tuple(tuple(c) for c in commandes_autorisees)
        self.autoriser_pages_web = autoriser_pages_web
        # Crochets fournis par l'interface (session interactive) — None = comportement par défaut.
        #   demandeur(titre, details, genre, argv) -> bool | str   (str = refus + consigne pour l'agent)
        #   valideur_ecriture(chemin_relatif, avant | None, apres) -> bool | str
        self.demandeur: Callable[..., bool | str] | None = None
        self.valideur_ecriture: Callable[[str, str | None, str], bool | str] | None = None
        self.journal = None            # agent_code.journal.Journal : permet /annuler
        self._consigne = ""
        self.dossier_agent_code = self.racine / ".agent_code"
        self.dossier_agent_code.mkdir(exist_ok=True)
        self._verrou = threading.RLock()       # sérialise les prompts humains
        self._verrou_audit = threading.Lock()

    # -- chemins ----------------------------------------------------------
    def resoudre(self, chemin: str | Path, *, ecriture: bool = False) -> Path:
        """Résout un chemin demandé par un agent et vérifie qu'il est autorisé."""
        brut = str(chemin).strip().strip("'\"") or "."
        if "\0" in brut:
            raise ErreurSecurite("⛔ Chemin invalide (octet nul).")
        p = Path(os.path.expanduser(brut))
        if not p.is_absolute():
            p = self.racine / p
        # resolve() suit les symlinks → bloque les évasions par lien symbolique
        p = p.resolve()
        if not p.is_relative_to(self.racine):
            self.audit("BLOQUE_TRAVERSAL", chemin=str(chemin))
            raise ErreurSecurite(f"⛔ Chemin hors du bac à sable bloqué : {chemin}")
        rel = p.relative_to(self.racine)
        if est_sensible(rel):
            self.audit("BLOQUE_SENSIBLE", chemin=str(rel))
            raise ErreurSecurite(f"⛔ Fichier sensible ({rel}) : accès interdit aux agents.")
        if ecriture:
            if self.lecture_seule:
                raise ErreurSecurite("⛔ Mode lecture seule : aucune écriture autorisée.")
            zone = next((part for part in rel.parts if part in ZONES_PROTEGEES_ECRITURE), None)
            if zone:
                raise ErreurSecurite(f"⛔ Zone protégée en écriture : {zone}")
            if p.is_dir():
                raise ErreurSecurite(f"⛔ {rel} est un dossier, pas un fichier.")
        return p

    def relatif(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.racine)) or "."
        except ValueError:
            return str(p)

    # -- validation humaine ------------------------------------------------
    def approuver(self, titre: str, details: str, *, pre_autorise: bool, genre: str = "autre",
                  argv: list[str] | None = None) -> bool:
        """Validation humaine, sauf si l'action est pré-autorisée par la configuration."""
        if pre_autorise:
            self.audit("AUTO_APPROUVE", titre=titre, details=details[:300])
            return True
        return self.demander(titre, details, genre=genre, argv=argv)

    def consigne_refus(self) -> str:
        """Consigne tapée par l'utilisateur lors du dernier refus (à transmettre à l'agent)."""
        c, self._consigne = self._consigne, ""
        return f"\nConsigne de l'utilisateur : {c}" if c else ""

    def preparer_ecriture(self, p: Path, avant: str | None, apres: str) -> None:
        """Appelé par les outils juste avant d'écrire : validation (diff) puis journalisation."""
        if self.valideur_ecriture is not None:
            rel = self.relatif(p)
            with self._verrou:
                rep = self.valideur_ecriture(rel, avant, apres)
            if rep is not True:
                self.audit("ECRITURE_REFUSEE", chemin=rel)
                consigne = f"\nConsigne de l'utilisateur : {rep}" if isinstance(rep, str) and rep else ""
                raise ErreurSecurite(f"⛔ Modification de {rel} refusée par l'utilisateur.{consigne}")
        if self.journal is not None:
            self.journal.avant_ecriture(p)

    def demander(self, titre: str, details: str, *, genre: str = "autre", argv: list[str] | None = None) -> bool:
        if not self.interactif:
            self.audit("REFUS_NON_INTERACTIF", titre=titre, details=details[:300])
            return False
        if self.demandeur is not None:
            with self._verrou:
                rep = self.demandeur(titre, details, genre, argv)
            ok = rep is True
            self._consigne = rep if isinstance(rep, str) else ""
            self.audit("VALIDATION", titre=titre, accepte=ok, details=details[:300])
            return ok
        with self._verrou:
            print("\n" + "=" * 60)
            print(f"⚠️  VALIDATION REQUISE : {titre}")
            print(details)
            print("=" * 60)
            try:
                rep = input("Autoriser ? (o/N) : ").strip().lower()
            except EOFError:
                rep = ""
        ok = rep in {"o", "oui", "y", "yes"}
        self.audit("VALIDATION", titre=titre, accepte=ok, details=details[:300])
        return ok

    # -- exécution ---------------------------------------------------------
    def executer(self, commande: str, *, timeout: int | None = None, forcer_confirmation: bool = False) -> str:
        analyse = analyser_commande(commande, self.racine)
        self.audit("COMMANDE", commande=commande, verdict=analyse.verdict.value, raison=analyse.raison)

        if analyse.verdict is Verdict.BLOQUEE:
            return f"⛔ Commande bloquée par la politique de sécurité : {analyse.raison}"
        if self.lecture_seule and analyse.verdict is not Verdict.AUTO:
            return "⛔ Mode lecture seule : seules les commandes d'inspection sont permises."

        auto = analyse.verdict is Verdict.AUTO and self.auto_approuver_sures and not forcer_confirmation
        liste_blanche = (analyse.verdict is Verdict.CONFIRMER and not analyse.sensible and not forcer_confirmation
                         and correspond_liste_blanche(analyse.argv, self.commandes_autorisees))
        if not auto and not self.approuver("exécution d'une commande",
                                           f"📂 {self.racine}\n👉 {commande}\nℹ️  {analyse.raison}",
                                           pre_autorise=liste_blanche, genre="commande", argv=analyse.argv):
            return "⛔ Action refusée par l'utilisateur (ou session non interactive)." + self.consigne_refus()
        return self.lancer(analyse.argv, timeout=timeout)

    def lancer(self, argv: list[str], *, timeout: int | None = None) -> str:
        """Lance un argv déjà validé (sans shell), sortie nettoyée et tronquée."""
        delai = timeout or self.timeout_cmd
        try:
            code, stdout, stderr = executer_processus(argv, self.racine, delai)
        except FileNotFoundError:
            return f"❌ Exécutable introuvable : {argv[0]}"
        except PermissionError:
            return f"❌ Permission refusée pour exécuter : {argv[0]}"
        entete = f"CODE RETOUR: {code}" if code is not None else f"⚠️ Timeout : commande arrêtée après {delai}s.\nCODE RETOUR: TIMEOUT"
        sortie = f"{entete}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}".strip()
        return tronquer(rediger_secrets(sortie), self.max_sortie)

    def lancer_brut(self, argv: list[str], *, timeout: int | None = None) -> tuple[bool, str]:
        """Pour le code d'agent_code (git déterministe) : (succès, stdout non tronqué)."""
        try:
            code, stdout, stderr = executer_processus(argv, self.racine, timeout or self.timeout_cmd)
        except OSError as e:
            return False, str(e)
        return code == 0, stdout if code == 0 else (stderr or stdout)

    # -- verrou -------------------------------------------------------------
    def verrouiller(self) -> "VerrouProjet":
        """Empêche deux missions simultanées sur le même projet (cron + humain, deux sessions…)."""
        return VerrouProjet(self.dossier_agent_code / "verrou")

    # -- audit -------------------------------------------------------------
    def audit(self, evenement: str, **details) -> None:
        ligne = {"ts": _dt.datetime.now().isoformat(timespec="seconds"), "evt": evenement, **details}
        texte = rediger_secrets(json.dumps(ligne, ensure_ascii=False, default=str))
        fichier = self.dossier_agent_code / "audit.log"
        try:
            with self._verrou_audit:
                if fichier.exists() and fichier.stat().st_size > TAILLE_MAX_AUDIT:
                    os.replace(fichier, fichier.with_suffix(".log.1"))
                with open(fichier, "a", encoding="utf-8") as f:
                    f.write(texte + "\n")
        except OSError:
            pass


class ProjetOccupe(ErreurSecurite):
    """Une autre mission tourne déjà sur ce projet."""


class VerrouProjet:
    """Verrou exclusif (flock) libéré automatiquement, même si le processus est tué."""

    def __init__(self, fichier: Path) -> None:
        self.fichier = fichier
        self._fd: int | None = None

    def __enter__(self) -> "VerrouProjet":
        self._fd = os.open(self.fichier, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            import fcntl
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:  # pragma: no cover — Windows : pas de verrou
            pass
        except BlockingIOError:
            try:
                autre = os.pread(self._fd, 200, 0).decode(errors="replace").strip()
            except OSError:
                autre = ""
            os.close(self._fd)
            self._fd = None
            raise ProjetOccupe("⛔ Une autre mission tourne déjà sur ce projet"
                               + (f" ({autre})" if autre else "") + ". Réessaie plus tard.") from None
        os.ftruncate(self._fd, 0)
        os.pwrite(self._fd, f"pid {os.getpid()} depuis {_dt.datetime.now():%Y-%m-%d %H:%M:%S}".encode(), 0)
        return self

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            try:
                os.ftruncate(self._fd, 0)
            except OSError:
                pass
            os.close(self._fd)   # fermer le descripteur libère le flock
            self._fd = None
