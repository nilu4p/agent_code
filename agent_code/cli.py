"""✻ agent_code — assistant de code agentique 100 % local et sécurisé (Ollama).

Exemples :
  agent_code                                                  # session interactive dans le dossier courant
  agent_code ~/projets/mon_api                                # … dans un autre projet
  agent_code -c                                               # reprend la dernière conversation
  agent_code -p "explique le module auth"                     # une question, une réponse (scripts)
  agent_code . -m revue -t "tout le projet"                   # mission multi-agents (audit en lecture seule)
  agent_code . -m dev -T demande.md --branche --json          # sans surveillance (cron, CI, serveur)
  agent_code --diagnostic .                                   # vérifie l'installation
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

VERSION = "1.0"

# Codes de sortie (documentés dans le README)
OK, ECHEC, USAGE, OCCUPE, TIMEOUT, INTERROMPU = 0, 1, 2, 3, 124, 130


def saisir(invite: str) -> str:
    try:
        return input(invite).strip()
    except EOFError:
        return ""


def parser_arguments(missions: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="agent_code", description="Assistant de code agentique 100 %% local et sécurisé.",
                                epilog="Sans option : session interactive (/aide pour l'aide). Codes de sortie : "
                                       "0 ok, 1 échec, 2 usage/config, 3 projet occupé, 124 délai dépassé, 130 interrompu.")
    p.add_argument("dossier", nargs="?", default=".", help="Projet cible (bac à sable). Défaut : dossier courant.")
    p.add_argument("-p", "--prompt", metavar="DEMANDE",
                   help="Une seule demande à l'agent, réponse sur la sortie standard, puis quitte.")
    p.add_argument("-c", "--continuer", action="store_true", help="Reprend la dernière conversation du projet.")
    p.add_argument("--mode", choices=["normal", "auto", "lecture"],
                   help="normal : tu valides chaque modification · auto : modifications sans validation · "
                        "lecture : aucune modification (défaut : AGENT_CODE_MODE ou normal).")
    p.add_argument("--modele", help="Modèle Ollama à utiliser pour cette session (défaut : AGENT_CODE_MODELE).")
    p.add_argument("-m", "--mission", choices=missions, help="Mission multi-agents à lancer.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("-t", "--tache", help="Description de la demande.")
    g.add_argument("-T", "--tache-fichier", metavar="FICHIER",
                   help="Lit la demande dans un fichier (« - » = entrée standard). Pratique pour un traceback.")
    p.add_argument("--lecture-seule", action="store_true", help="Interdit toute écriture et exécution de code.")
    p.add_argument("--branche", action="store_true",
                   help="Isole les modifications dans une branche git agent_code/<mission>-<date> (ta branche ne bouge pas).")
    p.add_argument("--sans-web", action="store_true", help="Désactive la recherche web.")
    p.add_argument("--silencieux", action="store_true", help="Missions : masque le texte des agents (seuls leurs actions et le rapport s'affichent).")
    p.add_argument("--confirmer-tout", action="store_true",
                   help="Demande validation même pour les commandes d'inspection (ls, git status...).")
    p.add_argument("--autoriser-tests", action="store_true",
                   help="Lance les tests sans validation (exécute le code du projet).")
    p.add_argument("--autoriser-cmd", action="append", default=[], metavar="CMD",
                   help="Préfixe de commande accepté sans validation (répétable), ex : --autoriser-cmd 'npm run lint'.")
    p.add_argument("--timeout-mission", type=int, metavar="SECONDES",
                   help="Durée max de la mission (défaut : AGENT_CODE_TIMEOUT_MISSION, 0 = illimité).")
    p.add_argument("--json", action="store_true",
                   help="Écrit un résumé JSON (statut, rapport, fichiers, branche) en dernière ligne de la sortie.")
    p.add_argument("--liste", action="store_true", help="Affiche les missions disponibles et quitte.")
    p.add_argument("--diagnostic", action="store_true", help="Vérifie l'installation (Ollama, modèles, contexte, git...).")
    p.add_argument("--init-config", action="store_true",
                   help="Crée ~/.config/agent_code/agent_code.env à partir du modèle et quitte.")
    p.add_argument("--version", action="version", version=f"agent_code {VERSION}")
    return p.parse_args()


def afficher_missions(missions: dict) -> list[str]:
    cles = list(missions)
    print("\n📋 MISSIONS DISPONIBLES")
    for i, cle in enumerate(cles, 1):
        m = missions[cle]
        verrou = " 🔒lecture seule" if m.lecture_seule else ""
        print(f"  {i:>2}. {m.emoji} {m.titre:<32} [{cle}]{verrou}\n      {m.resume}")
    return cles


def choisir_mission(missions: dict) -> str | None:
    cles = afficher_missions(missions)
    while True:
        choix = saisir("\nNuméro ou nom de mission (q pour quitter) : ").lower()
        if choix in {"q", "quit", "quitter", "exit", ""}:
            return None
        if choix.isdigit() and 1 <= int(choix) <= len(cles):
            return cles[int(choix) - 1]
        if choix in missions:
            return choix
        print("❓ Choix invalide.")


def init_config() -> int:
    from .config import dossier_config_utilisateur, modele_config
    cible = dossier_config_utilisateur() / "agent_code.env"
    if cible.exists():
        print(f"ℹ️  {cible} existe déjà : rien n'a été modifié.")
        return OK
    cible.parent.mkdir(parents=True, exist_ok=True)
    cible.write_text(modele_config(), encoding="utf-8")
    cible.chmod(0o600)   # peut contenir AGENT_CODE_OLLAMA_API_KEY
    print(f"✅ Configuration créée : {cible}\n   Adapte-la puis lance : agent_code --diagnostic")
    return OK


def lire_demande(args: argparse.Namespace) -> str | None:
    if args.tache_fichier is None:
        return args.tache
    if args.tache_fichier == "-":
        return sys.stdin.read()
    return Path(args.tache_fichier).expanduser().read_text(encoding="utf-8", errors="replace")


def main() -> int:
    if sys.version_info < (3, 11):  # tomllib, Path.is_relative_to…
        print("❌ Python 3.11 ou plus récent est requis (3.12 recommandé).")
        return USAGE

    from .config import charger_config
    from .missions import MISSIONS
    from .securite import ErreurSecurite, ProjetOccupe, Sandbox

    args = parser_arguments(sorted(MISSIONS))
    if args.liste:
        afficher_missions(MISSIONS)
        return OK
    if args.init_config:
        return init_config()

    # Surcharges de configuration par la ligne de commande
    if args.sans_web:
        os.environ["AGENT_CODE_WEB"] = "false"
    if args.modele:
        os.environ["AGENT_CODE_MODELE"] = args.modele
        os.environ["AGENT_CODE_MODELE_CODE"] = args.modele
        os.environ["AGENT_CODE_MODELE_REFLEXION"] = args.modele
    if args.mode:
        os.environ["AGENT_CODE_MODE"] = args.mode
    if args.silencieux:
        os.environ["AGENT_CODE_VERBOSE"] = "false"
    if args.timeout_mission is not None:
        os.environ["AGENT_CODE_TIMEOUT_MISSION"] = str(max(0, args.timeout_mission))
    if args.autoriser_tests:
        os.environ["AGENT_CODE_AUTORISER_TESTS"] = "true"
    if args.autoriser_cmd:
        existantes = os.environ.get("AGENT_CODE_COMMANDES_AUTORISEES", "")
        os.environ["AGENT_CODE_COMMANDES_AUTORISEES"] = ",".join(filter(None, [existantes, *args.autoriser_cmd]))
    cfg = charger_config()

    if args.diagnostic:
        from .diagnostic import afficher, diagnostic_complet
        return afficher(diagnostic_complet(cfg, args.dossier))

    try:
        demande_initiale = lire_demande(args)
    except OSError as e:
        print(f"❌ Impossible de lire la demande : {e}")
        return USAGE

    try:
        sb = Sandbox(
            args.dossier,
            lecture_seule=args.lecture_seule,
            auto_approuver_sures=cfg.auto_approuver_sures and not args.confirmer_tout,
            timeout_cmd=cfg.timeout_cmd,
            max_sortie=cfg.max_sortie,
            # La demande lue sur stdin consomme l'entrée standard : plus de validation possible.
            interactif=False if args.tache_fichier == "-" else None,
            autoriser_tests=cfg.autoriser_tests,
            commandes_autorisees=cfg.commandes_autorisees,
            autoriser_pages_web=cfg.autoriser_pages_web,
        )
    except (ErreurSecurite, FileNotFoundError, NotADirectoryError, PermissionError) as e:
        print(f"❌ {e}")
        return USAGE

    os.chdir(sb.racine)
    mode = "lecture" if args.lecture_seule else cfg.mode

    # --- Session interactive (par défaut) ou demande unique -----------------------
    if not args.mission:
        if args.prompt or (args.tache_fichier and not args.tache):
            demande = args.prompt or demande_initiale or ""
            return _une_fois(cfg, sb, demande, mode, args.json)
        if not sb.interactif:
            print("❌ Pas de terminal : utilise -p \"demande\" (une réponse) ou -m <mission> -t \"demande\".")
            return USAGE
        from .repl import lancer_repl
        return lancer_repl(cfg, sb, mode=mode, version=VERSION, reprendre=args.continuer,
                           demande_initiale=args.tache)

    if not sb.interactif and not demande_initiale:
        print("❌ Mode non interactif (pas de terminal) : précise la demande avec -t ou -T.")
        return USAGE

    from .orchestrateur import lancer_mission, verifier_ollama

    print(f"\n✻ AGENT_CODE v{VERSION} — mission {args.mission}")
    print(f"🔒 Bac à sable : {sb.racine}")
    print(f"🧠 Modèle      : {cfg.modele_code}" + (f" (réflexion : {cfg.modele_reflexion})"
                                                    if cfg.modele_reflexion != cfg.modele_code else ""))
    print(f"⚙️  Options     : web={'oui' if cfg.web else 'non'} | lecture seule={'oui' if sb.lecture_seule else 'non'}"
          f" | branche={'oui' if args.branche else 'non'}"
          + (f" | délai max={cfg.timeout_mission}s" if cfg.timeout_mission else ""))
    if not sb.interactif:
        auto = (["tests"] if cfg.autoriser_tests else []) + [" ".join(c) for c in cfg.commandes_autorisees]
        print(f"🤖 Sans surveillance : validations refusées sauf {', '.join(auto) or 'commandes d’inspection'}.")

    problemes = verifier_ollama(cfg)
    if problemes:
        print("\n❌ " + "\n❌ ".join(problemes))
        if not sb.interactif or saisir("Continuer quand même ? (o/N) : ").lower() not in {"o", "oui"}:
            return ECHEC

    try:
        with sb.verrouiller():
            return _boucle_missions(args, cfg, sb, MISSIONS, demande_initiale, lancer_mission)
    except ProjetOccupe as e:
        print(f"\n{e}")
        if args.json:
            print(json.dumps({"statut": "occupe", "erreur": str(e)}, ensure_ascii=False))
        return OCCUPE


def _une_fois(cfg, sb, demande: str, mode: str, sortie_json: bool) -> int:
    from .diagnostic import problemes_ollama
    from .repl import executer_une_fois
    from .securite import ProjetOccupe
    demande = (demande or "").strip()
    if not demande:
        print("❌ Demande vide.")
        return USAGE
    erreurs, _ = problemes_ollama(cfg)
    if erreurs:
        print("❌ " + "\n❌ ".join(erreurs))
        return ECHEC
    try:
        with sb.verrouiller():
            code, texte = executer_une_fois(cfg, sb, demande[:20000], mode=mode)
    except ProjetOccupe as e:
        print(e)
        return OCCUPE
    if sortie_json:
        print(json.dumps({"statut": "ok" if code == 0 else "erreur", "reponse": texte,
                          "fichiers_modifies": sb.journal.fichiers_du_tour() if sb.journal else []},
                         ensure_ascii=False))
    return code


def _boucle_missions(args, cfg, sb, missions, demande_initiale, lancer_mission) -> int:
    from .orchestrateur import DelaiMissionDepasse, ResultatMission

    premiere = True
    code_sortie = OK
    while True:
        cle = args.mission if (premiere and args.mission) else (choisir_mission(missions) if sb.interactif else None)
        if cle is None:
            break
        mission = missions[cle]
        if mission.web_requis and not cfg.web:
            print("⚠️  Cette mission nécessite le web (retire --sans-web).")
            premiere = False
            if not sb.interactif:
                code_sortie = USAGE
                break
            continue
        demande = demande_initiale if (premiere and demande_initiale) else saisir(f"\n{mission.emoji} {mission.invite}\n> ")
        premiere = False
        demande = (demande or "").strip()[:8000]
        if not demande:
            print("ℹ️  Demande vide, mission annulée.")
            continue
        res: ResultatMission | None = None
        try:
            res = lancer_mission(cfg, sb, mission, demande, branche=args.branche)
            code_sortie = OK
        except DelaiMissionDepasse as e:
            print(f"\n⏱️  {e}")
            sb.audit("MISSION_TIMEOUT", mission=cle)
            res, code_sortie = getattr(e, "resultat_mission", None), TIMEOUT
        except KeyboardInterrupt as e:
            print("\n⏹️  Mission interrompue.")
            sb.audit("MISSION_INTERROMPUE", mission=cle)
            res, code_sortie = getattr(e, "resultat_mission", None), INTERROMPU
        except Exception as e:  # noqa: BLE001
            print(f"\n❌ La mission a échoué : {type(e).__name__} : {str(e)[:1000]}")
            print("   Détails dans .agent_code/audit.log et .agent_code/logs/.")
            sb.audit("MISSION_ERREUR", mission=cle, erreur=str(e)[:500])
            res, code_sortie = getattr(e, "resultat_mission", None), ECHEC
            if res is None:
                res = ResultatMission(mission=cle, statut="erreur", erreur=f"{type(e).__name__} : {str(e)[:500]}")
        if args.json and res is not None:
            print(json.dumps(res.en_dict(), ensure_ascii=False))
        if not sb.interactif or args.mission or code_sortie == INTERROMPU:
            break
        if saisir("\n🔁 Lancer une autre mission ? (o/N) : ").lower() not in {"o", "oui"}:
            break

    print(f"\n👋 Fin de session. Journal d'audit : {Path(sb.dossier_agent_code, 'audit.log')}")
    return code_sortie


def point_entree() -> None:
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n👋 Interrompu.")
        sys.exit(INTERROMPU)


if __name__ == "__main__":
    point_entree()
