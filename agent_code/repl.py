"""Session interactive « façon Claude Code » : agent_code [dossier]."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from . import outils
from .config import Config
from .interface import DESCRIPTION_MODE, ICONES_MODE, MODES, RICH, Interface, escape
from .llm import ErreurLLM
from .securite import DOSSIERS_IGNORES, Sandbox, est_sensible, rediger_secrets
from .session import Session

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    PTK = True
except ImportError:  # pragma: no cover — repli sur input() + readline
    PTK = False

if RICH:
    from rich.panel import Panel
    from rich.table import Table

COMMANDES: dict[str, str] = {
    "/aide": "affiche cette aide",
    "/mode": "change de mode : normal · auto · lecture  (raccourci : ⇧Tab)",
    "/modele": "liste les modèles installés ou en choisit un : /modele qwen3-coder:30b",
    "/annuler": "annule les modifications de fichiers du dernier tour",
    "/diff": "montre tout ce que la session a modifié",
    "/commit": "propose un message de commit et committe (git)",
    "/init": "analyse le projet et écrit AGENT_CODE.md (mémoire du projet, relue à chaque session)",
    "/memo": "ajoute une note durable au projet : /memo on utilise pytest-asyncio",
    "/contexte": "occupation de la mémoire de travail du modèle",
    "/compacter": "résume la conversation pour libérer de la mémoire",
    "/effacer": "nouvelle conversation (l'ancienne reste dans /reprendre)",
    "/reprendre": "reprend une conversation précédente",
    "/missions": "liste les missions multi-agents (dev, revue, debug…)",
    "/mission": "lance une mission : /mission revue tout le projet",
    "/quitter": "quitte (ou Ctrl+D)",
}

PROMPT_INIT = """\
Analyse ce projet puis écris (ou mets à jour) le fichier AGENT_CODE.md à la racine. Il sera relu au début de chaque
session pour te donner le contexte : sois concis et utile (60 lignes max), en français, avec ces sections :
# <nom du projet>
## Objectif (2-3 phrases)
## Stack et dépendances clés
## Commandes (installer, lancer, tester, linter — vérifie qu'elles existent vraiment)
## Architecture (dossiers et fichiers clés, avec leur rôle)
## Conventions (style, nommage, tests, gestion d'erreurs)
## Pièges et points d'attention
N'invente rien : appuie-toi sur les fichiers (README, pyproject/package.json, Makefile, CI, code).
Si AGENT_CODE.md existe déjà, améliore-le sans perdre d'information utile."""


# ---------------------------------------------------------------------------
# Saisie
# ---------------------------------------------------------------------------

class _Completeur(Completer if PTK else object):
    """Complète les /commandes et les @chemins du projet."""

    def __init__(self, sb: Sandbox) -> None:
        self.sb = sb
        self._fichiers: list[str] | None = None
        self._date = 0.0

    def fichiers(self) -> list[str]:
        if self._fichiers is None or time.monotonic() - self._date > 30:
            res: list[str] = []
            for dossier, sous, noms in os.walk(self.sb.racine):
                sous[:] = sorted(d for d in sous if d not in DOSSIERS_IGNORES and not est_sensible(Path(d) / "x"))
                rel_d = os.path.relpath(dossier, self.sb.racine)
                if rel_d != ".":
                    res.append(rel_d + "/")
                for n in sorted(noms):
                    rel = n if rel_d == "." else f"{rel_d}/{n}"
                    if not est_sensible(rel):
                        res.append(rel)
                if len(res) > 20_000:
                    break
            self._fichiers, self._date = res, time.monotonic()
        return self._fichiers

    def get_completions(self, document, complete_event):
        texte = document.text_before_cursor
        if texte.startswith("/") and " " not in texte:
            for cmd, desc in COMMANDES.items():
                if cmd.startswith(texte):
                    yield Completion(cmd, start_position=-len(texte), display_meta=desc)
            return
        if texte.startswith(("/mode ",)):
            mot = texte.split(" ", 1)[1]
            for m in MODES:
                if m.startswith(mot):
                    yield Completion(m, start_position=-len(mot), display_meta=DESCRIPTION_MODE[m])
            return
        mot = texte.split()[-1] if texte.split() and not texte.endswith(" ") else ""
        if mot.startswith("@"):
            cherche = mot[1:].lower()
            n = 0
            for f in self.fichiers():
                if cherche in f.lower():
                    yield Completion("@" + f, start_position=-len(mot))
                    n += 1
                    if n >= 40:
                        break


class Saisie:
    def __init__(self, sb: Sandbox, session_ref: dict) -> None:
        self.session_ref = session_ref
        self.ptk = None
        if PTK:
            historique = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "agent_code"
            historique.mkdir(parents=True, exist_ok=True)
            kb = KeyBindings()

            @kb.add("s-tab")
            def _(event):
                s = self.session_ref.get("session")
                if s and not s.lecture_forcee:
                    s.appliquer_mode(MODES[(MODES.index(s.mode) + 1) % len(MODES)])
                    event.app.invalidate()

            @kb.add("escape", "enter")
            def _(event):
                event.current_buffer.insert_text("\n")

            self.ptk = PromptSession(history=FileHistory(str(historique / "historique")),
                                     completer=_Completeur(sb), complete_while_typing=True,
                                     key_bindings=kb, bottom_toolbar=self._barre, multiline=False)
        else:
            try:
                import readline  # noqa: F401 — édition de ligne et historique
            except ImportError:
                pass

    def _barre(self):
        s = self.session_ref.get("session")
        if not s:
            return ""
        taux = int(s.agent.taux_contexte() * 100)
        couleur = "ansigreen" if taux < 50 else "ansiyellow" if taux < 75 else "ansired"
        return HTML(f" {ICONES_MODE[s.mode]} <b>{s.mode}</b> <i>(⇧Tab)</i> · {escape_html(s.modele)} · "
                    f"mémoire <style fg='{couleur}'>{taux}%</style> · /aide · Échap+Entrée = nouvelle ligne")

    def lire(self) -> str:
        if self.ptk:
            return self.ptk.prompt(HTML("<ansicyan><b>›</b></ansicyan> "))
        return input("› ")


def escape_html(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Session interactive
# ---------------------------------------------------------------------------

class Repl:
    def __init__(self, cfg: Config, sb: Sandbox, *, mode: str, version: str) -> None:
        self.cfg = cfg
        self.sb = sb
        self.version = version
        self.ui = Interface(sb, mode=mode)
        self.ui.brancher(sb)
        self.console = self.ui.console
        self.session: Session | None = None
        self._ref: dict = {}

    # -- affichage ------------------------------------------------------------
    def afficher(self, texte: str, style: str = "") -> None:
        self.ui.ligne(texte, style)

    def banniere(self) -> None:
        git = ""
        if outils.est_depot(self.sb):
            ok, br = outils.git(self.sb, "branch", "--show-current")
            git = f" · 🌿 {br.strip()}" if ok and br.strip() else ""
        corps = (f"[bold]✻ agent_code[/] [dim]v{self.version} — assistant de code 100 % local[/]\n\n"
                 f"📂 {escape(str(self.sb.racine))}{git}\n"
                 f"🧠 {escape(self.cfg.modele_code)} · mémoire {self.cfg.contexte // 1024}k tokens\n\n"
                 "[dim]Décris ce que tu veux faire, en langage naturel.\n"
                 "@fichier joint un fichier · !commande lance une commande toi-même · /aide · ⇧Tab change de mode\n"
                 "Ctrl+C interrompt l'agent · Ctrl+D quitte[/]")
        if self.console:
            self.console.print(Panel(corps, border_style="cyan", expand=False))
        else:
            print(corps)

    def aide(self) -> None:
        if self.console:
            t = Table(show_header=False, box=None, padding=(0, 2))
            for cmd, desc in COMMANDES.items():
                t.add_row(f"[cyan]{cmd}[/]", desc)
            t.add_row("", "")
            t.add_row("[cyan]@chemin[/]", "joint un fichier ou un dossier à ta demande (Tab pour compléter)")
            t.add_row("[cyan]!commande[/]", "lance une commande toi-même ; sa sortie est transmise à l'agent")
            t.add_row("[cyan]⇧Tab[/]", "change de mode : " + " → ".join(MODES))
            t.add_row("[cyan]Échap+Entrée[/]", "nouvelle ligne dans ta demande")
            self.console.print(Panel(t, title="Aide", title_align="left", border_style="cyan", expand=False))
            self.afficher("[dim]Modes : " + " · ".join(f"{ICONES_MODE[m]} {m} = {DESCRIPTION_MODE[m]}"
                                                       for m in MODES) + "[/]")
        else:
            for cmd, desc in COMMANDES.items():
                print(f"{cmd:<12} {desc}")

    # -- préparation -----------------------------------------------------------
    def preparer(self) -> bool:
        """Vérifie Ollama et le modèle ; propose de le télécharger. Retourne False si impossible."""
        from .llm import ClientOllama
        client = ClientOllama(self.cfg.ollama_url, self.cfg.ollama_api_key, 30)
        try:
            client.version()
        except ErreurLLM as e:
            self.afficher(f"[red]❌ {escape(str(e))}[/]")
            self.afficher("[dim]Démarre Ollama (« ollama serve » ou « systemctl start ollama »), puis relance agent_code.[/]")
            return False
        modele = self.cfg.modele_code
        try:
            present = client.present(modele)
        except ErreurLLM as e:
            self.afficher(f"[red]❌ {escape(str(e))}[/]")
            return False
        if not present:
            self.afficher(f"[yellow]Le modèle {escape(modele)} n'est pas encore installé.[/]")
            rep = self.ui._demander("Le télécharger maintenant avec Ollama ? ([green]O[/]/n) › ").lower()
            if rep.startswith("n"):
                return False
            if not self._telecharger(client, modele):
                return False
        try:
            caps = client.details(modele).get("capabilities")
        except ErreurLLM:
            caps = None
        if caps is not None and "tools" not in caps:
            self.afficher(f"[red]❌ {escape(modele)} ne gère pas les appels d'outils : impossible de l'utiliser comme "
                          "agent. Choisis-en un autre (AGENT_CODE_MODELE=qwen3-coder:30b).[/]")
            return False
        if not self.cfg.ollama_local and not self.cfg.ollama_de_confiance:
            self.afficher(f"[yellow]⚠️  Ollama n'est pas local : ton code sera envoyé à {escape(self.cfg.ollama_url)}.[/]")
        return True

    def _telecharger(self, client, modele: str) -> bool:
        try:
            if RICH and self.ui.terminal:
                from rich.progress import BarColumn, DownloadColumn, Progress, TransferSpeedColumn
                with Progress("[progress.description]{task.description}", BarColumn(), DownloadColumn(),
                              TransferSpeedColumn(), console=self.console) as prog:
                    tache = prog.add_task(f"⬇️  {modele}", total=None)

                    def maj(statut: str, fait: int, total: int) -> None:
                        prog.update(tache, completed=fait, total=total or None,
                                    description=f"⬇️  {modele} · {statut[:30]}")
                    client.telecharger(modele, maj)
            else:
                client.telecharger(modele, lambda s, f, t: None)
        except (ErreurLLM, KeyboardInterrupt) as e:
            self.afficher(f"[red]❌ Téléchargement interrompu : {escape(str(e) or 'Ctrl+C')}[/]")
            return False
        self.afficher(f"[green]✅ {escape(modele)} installé.[/]")
        return True

    # -- boucle ---------------------------------------------------------------
    def lancer(self, *, reprendre: bool = False, demande_initiale: str | None = None) -> int:
        self.banniere()
        if not self.preparer():
            return 1
        self.session = Session(self.cfg, self.sb, self.ui, mode=self.ui.mode)
        self._ref["session"] = self.session
        if reprendre:
            precedentes = self.session.sessions_precedentes(1)
            if precedentes:
                n = self.session.reprendre(precedentes[0]["fichier"])
                self.afficher(f"[green]↻ Conversation reprise : « {escape(precedentes[0]['titre'])} » ({n} messages)[/]")
            else:
                self.afficher("[dim]Aucune conversation précédente à reprendre.[/]")
        if self.session.contexte.fichier_instructions:
            self.afficher(f"[dim]📘 Instructions du projet chargées depuis {self.session.contexte.fichier_instructions}[/]")
        else:
            self.afficher("[dim]💡 Astuce : /init crée AGENT_CODE.md, la mémoire du projet (stack, commandes, conventions).[/]")
        self.afficher(f"[dim]{ICONES_MODE[self.session.mode]} Mode {self.session.mode} : "
                      f"{DESCRIPTION_MODE[self.session.mode]}[/]")
        self.session.prechauffer()
        saisie = Saisie(self.sb, self._ref)

        if demande_initiale:
            self.traiter(demande_initiale)
        ctrl_c = 0.0
        while True:
            try:
                texte = saisie.lire()
            except KeyboardInterrupt:
                if time.monotonic() - ctrl_c < 2:
                    break
                ctrl_c = time.monotonic()
                self.afficher("[dim](Ctrl+C encore pour quitter, ou /quitter)[/]")
                continue
            except EOFError:
                break
            texte = texte.strip()
            if not texte:
                continue
            if texte in {"/quitter", "/q", "/exit", "/quit", "exit", "quit"}:
                break
            try:
                self.traiter(texte)
            except KeyboardInterrupt:
                self.afficher("[yellow]⏹️  Interrompu.[/]")
        self.session.sauvegarder()
        self.afficher("[dim]👋 À bientôt. Reprends cette conversation avec : agent_code -c[/]")
        return 0

    def traiter(self, texte: str) -> None:
        if texte.startswith("/"):
            cmd, _, arg = texte.partition(" ")
            fn = getattr(self, "cmd_" + cmd[1:].replace("-", "_"), None)
            if fn is None:
                proches = [c for c in COMMANDES if c.startswith(cmd[:3])]
                self.afficher(f"[red]Commande inconnue : {escape(cmd)}[/]" +
                              (f" [dim](tu voulais dire {', '.join(proches)} ?)[/]" if proches else " [dim]/aide[/]"))
                return
            fn(arg.strip())
            return
        if texte.startswith("!"):
            self.commande_utilisateur(texte[1:].strip())
            return
        self.demander_agent(texte)

    def demander_agent(self, texte: str) -> None:
        assert self.session is not None
        debut = time.monotonic()
        tokens_avant = self.session.agent.stats.tokens_sortie
        gen_avant = self.session.agent.stats.duree_generation_s
        _, joints = self.session.developper_mentions(texte)
        if joints:
            self.afficher(f"[dim]📎 joint : {escape(', '.join(joints))}[/]")
        res, modifies = self.session.envoyer(texte)
        duree = time.monotonic() - debut
        if res.erreur and not res.texte:
            self.afficher(f"[red]❌ {escape(res.erreur)}[/]")
            return
        if res.interrompu:
            self.afficher("[yellow]⏹️  Interrompu. Donne une nouvelle consigne, ou /annuler pour défaire les changements.[/]")
        tokens = self.session.agent.stats.tokens_sortie - tokens_avant
        gen = self.session.agent.stats.duree_generation_s - gen_avant
        vitesse = f" · {tokens / gen:.0f} tok/s" if tokens and gen > 0.5 else ""
        self.afficher(f"[dim]· {duree:.0f}s · {res.etapes} étape(s) · {tokens} tokens{vitesse} · "
                      f"mémoire {int(self.session.agent.taux_contexte() * 100)}%[/]")
        if modifies:
            self.afficher(f"[green]📝 {len(modifies)} fichier(s) modifié(s) : {escape(', '.join(modifies[:8]))}"
                          f"{'…' if len(modifies) > 8 else ''}[/] [dim]· /diff pour voir · /annuler pour revenir en arrière[/]")
        if res.erreur:
            self.afficher(f"[yellow]⚠️  {escape(res.erreur)}[/]")

    def commande_utilisateur(self, commande: str) -> None:
        """« !cmd » : commande lancée par l'utilisateur lui-même (pas par l'agent), dans le projet."""
        assert self.session is not None
        if not commande:
            self.afficher("[dim]Usage : !commande   (ex : !pytest -q)[/]")
            return
        try:
            r = subprocess.run(commande, shell=True, cwd=self.sb.racine, capture_output=True, text=True,
                               timeout=self.cfg.timeout_cmd * 5, errors="replace")
            sortie = (r.stdout + ("\n" + r.stderr if r.stderr else "")).rstrip()
            code = r.returncode
        except subprocess.TimeoutExpired:
            sortie, code = "(délai dépassé)", None
        if self.console:
            self.console.print(Panel(escape(sortie[-8000:] or "(aucune sortie)"),
                                     title=f"$ {escape(commande)} → code {code}", title_align="left",
                                     border_style="green" if code == 0 else "red"))
        else:
            print(sortie)
        self.session.ajouter_sortie_commande(commande, rediger_secrets(f"code retour {code}\n{sortie}"))
        self.afficher("[dim](sortie transmise à l'agent avec ta prochaine demande)[/]")

    # -- commandes / ----------------------------------------------------------
    def cmd_aide(self, _arg: str) -> None:
        self.aide()

    cmd_help = cmd_aide

    def cmd_mode(self, arg: str) -> None:
        assert self.session is not None
        if self.session.lecture_forcee:
            self.afficher("[yellow]Session lancée avec --lecture-seule : le mode lecture est imposé.[/]")
            return
        if not arg:
            arg = MODES[(MODES.index(self.session.mode) + 1) % len(MODES)]
        if arg not in MODES:
            self.afficher(f"[red]Mode inconnu. Choix : {', '.join(MODES)}[/]")
            return
        self.session.appliquer_mode(arg)
        self.afficher(f"{ICONES_MODE[arg]} Mode [bold]{arg}[/] : {DESCRIPTION_MODE[arg]}")

    def cmd_modele(self, arg: str) -> None:
        assert self.session is not None
        try:
            modeles = self.session.client.modeles()
        except ErreurLLM as e:
            self.afficher(f"[red]❌ {escape(str(e))}[/]")
            return
        if not arg:
            for m in sorted(modeles, key=lambda x: x.get("name", "")):
                nom = m.get("name", "")
                actuel = " [green]← actuel[/]" if nom in {self.session.modele, self.session.modele + ":latest"} else ""
                self.afficher(f"  {escape(nom):<40} {m.get('size', 0) / 1e9:5.1f} Go{actuel}")
            self.afficher("[dim]/modele <nom> pour changer (le modèle doit gérer les outils).[/]")
            return
        noms = {m.get("name", "") for m in modeles}
        if arg not in noms and f"{arg}:latest" not in noms:
            self.afficher(f"[red]{escape(arg)} n'est pas installé (ollama pull {escape(arg)}).[/]")
            return
        self.session.changer_modele(arg)
        self.afficher(f"🧠 Modèle : [bold]{escape(arg)}[/] (pour changer durablement : AGENT_CODE_MODELE dans la config)")

    def cmd_annuler(self, _arg: str) -> None:
        res = self.sb.journal.annuler_dernier() if self.sb.journal else None
        if not res:
            self.afficher("[dim]Rien à annuler (seules les modifications faites par l'agent sont suivies).[/]")
            return
        titre, fichiers = res
        self.afficher(f"[green]↩️  Annulé ({escape(titre[:60])}) : {escape(', '.join(fichiers))}[/]")
        assert self.session is not None
        self.session.contexte_en_attente.append(
            f"[L'utilisateur a annulé tes dernières modifications de fichiers : {', '.join(fichiers)}. "
            "Ces fichiers sont revenus à leur état précédent.]")

    def cmd_diff(self, _arg: str) -> None:
        diff = self.sb.journal.diff_session() if self.sb.journal else ""
        if not diff:
            self.afficher("[dim]Aucune modification faite par l'agent dans cette session.[/]")
            return
        if self.console:
            from rich.syntax import Syntax
            self.console.print(Syntax(rediger_secrets(diff), "diff", theme="ansi_dark", word_wrap=True))
        else:
            print(diff)

    def cmd_contexte(self, _arg: str) -> None:
        assert self.session is not None
        a = self.session.agent
        est = a.estimer_tokens()
        self.afficher(f"🧠 Mémoire de travail : ~{est} / {a.reglages.contexte} tokens ({int(a.taux_contexte() * 100)}%) · "
                      f"{len(a.messages)} messages · résumé {'oui' if a.resume else 'non'}")
        self.afficher(f"[dim]Session : {a.stats.appels_llm} appels au modèle · {a.stats.tokens_sortie} tokens générés · "
                      f"{a.stats.duree_llm_s / 60:.1f} min de calcul. Au-delà de 70 %, un résumé automatique est fait.[/]")

    def cmd_compacter(self, arg: str) -> None:
        assert self.session is not None
        avant = self.session.agent.taux_contexte()
        self.ui.debut_reflexion()
        ok = self.session.agent.compacter(arg)
        self.ui.fin_texte()
        if ok:
            self.afficher(f"[green]🗜️  Conversation résumée : mémoire {int(avant * 100)}% → "
                          f"{int(self.session.agent.taux_contexte() * 100)}%[/]")
        else:
            self.afficher("[dim]Rien à résumer pour l'instant.[/]")

    def cmd_effacer(self, _arg: str) -> None:
        assert self.session is not None
        self.session.nouvelle_conversation()
        self.afficher("[green]✨ Nouvelle conversation.[/] [dim](l'ancienne est dans /reprendre)[/]")

    cmd_clear = cmd_effacer

    def cmd_reprendre(self, arg: str) -> None:
        assert self.session is not None
        liste = self.session.sessions_precedentes(10)
        if not liste:
            self.afficher("[dim]Aucune conversation précédente.[/]")
            return
        if not arg:
            for i, s in enumerate(liste, 1):
                self.afficher(f"  [cyan]{i:>2}[/]. {escape(s['maj'][:16].replace('T', ' '))} · "
                              f"{escape(s['titre'][:70])} [dim]({s['messages']} messages)[/]")
            arg = self.ui._demander("Numéro de la conversation à reprendre (Entrée = annuler) › ")
        if not arg.isdigit() or not 1 <= int(arg) <= len(liste):
            return
        choix = liste[int(arg) - 1]
        self.session.sauvegarder()
        n = self.session.reprendre(choix["fichier"])
        self.afficher(f"[green]↻ Conversation reprise : « {escape(choix['titre'])} » ({n} messages)[/]")

    def cmd_init(self, _arg: str) -> None:
        assert self.session is not None
        if self.session.mode == "lecture":
            self.afficher("[yellow]/init écrit AGENT_CODE.md : passe d'abord en mode normal ou auto (⇧Tab).[/]")
            return
        self.demander_agent(PROMPT_INIT)
        self.session.rafraichir_contexte()
        if self.session.contexte.fichier_instructions:
            self.afficher(f"[green]📘 {self.session.contexte.fichier_instructions} chargé : il sera relu à chaque "
                          "session. Édite-le librement pour guider l'agent.[/]")

    def cmd_memo(self, arg: str) -> None:
        if not arg:
            self.afficher("[dim]Usage : /memo <note durable sur le projet>[/]")
            return
        self.afficher(outils.noter_memo.func(arg))

    def cmd_missions(self, _arg: str) -> None:
        from .missions import MISSIONS
        for cle, m in MISSIONS.items():
            verrou = " [dim]🔒 lecture seule[/]" if m.lecture_seule else ""
            self.afficher(f"  {m.emoji} [cyan]{cle:<9}[/] {escape(m.titre)}{verrou}\n      [dim]{escape(m.resume)}[/]")
        self.afficher("[dim]/mission <nom> <demande> — ex : /mission revue le module api/[/]")

    def cmd_mission(self, arg: str) -> None:
        from .missions import MISSIONS
        from .orchestrateur import lancer_mission
        cle, _, demande = arg.partition(" ")
        if cle not in MISSIONS:
            self.cmd_missions("")
            return
        mission = MISSIONS[cle]
        if not demande.strip():
            demande = self.ui._demander(f"{mission.emoji} {mission.invite} › ")
            if not demande.strip():
                return
        if mission.web_requis and not self.cfg.web:
            self.afficher("[yellow]Cette mission nécessite le web (AGENT_CODE_WEB=true).[/]")
            return
        try:
            res = lancer_mission(self.cfg, self.sb, mission, demande.strip(), interface=self.ui)
            self.afficher(f"[green]📄 Rapport : {escape(res.rapport)}[/]")
        except KeyboardInterrupt:
            self.afficher("[yellow]⏹️  Mission interrompue.[/]")
        except Exception as e:  # noqa: BLE001
            self.afficher(f"[red]❌ Mission échouée : {escape(str(e)[:500])}[/]")
        finally:
            assert self.session is not None
            self.session.appliquer_mode(self.session.mode)   # rétablit lecture seule / outils

    def cmd_commit(self, arg: str) -> None:
        assert self.session is not None
        if not outils.est_depot(self.sb):
            self.afficher("[yellow]Ce projet n'est pas un dépôt git (lance « !git init » si tu le souhaites).[/]")
            return
        ok, statut = outils.git(self.sb, "status", "--porcelain")
        if not ok or not statut.strip():
            self.afficher("[dim]Rien à committer.[/]")
            return
        self.afficher("[dim]" + escape(statut.rstrip()) + "[/]")
        message = arg
        if not message:
            _, diff = outils.git(self.sb, "diff", "HEAD", "--no-ext-diff", "--stat", "-p", "--", ".",
                                 *outils._EXCLUSIONS_GIT)
            _, nouveaux = outils.git(self.sb, "ls-files", "--others", "--exclude-standard")
            demande = ("Écris un message de commit git en français pour ces changements : une première ligne "
                       "à l'impératif de 72 caractères max, une ligne vide, puis 1 à 5 puces concises. "
                       "Réponds UNIQUEMENT avec le message, sans bloc de code.\n\n"
                       f"Nouveaux fichiers :\n{nouveaux[:2000]}\n\nDiff :\n{rediger_secrets(diff)[:14000]}")
            self.ui.debut_reflexion()
            try:
                rep = self.session.client.chat(self.session.modele, [{"role": "user", "content": demande}],
                                               options={**self.session.agent.reglages.options(), "num_predict": 300},
                                               keep_alive=self.cfg.garder_modele)
                message = rep.texte.strip().strip("`").strip()
            except ErreurLLM as e:
                self.afficher(f"[red]❌ {escape(str(e))}[/]")
                return
            finally:
                self.ui.fin_texte()
        if self.console:
            self.console.print(Panel(escape(message), title="Message de commit", title_align="left",
                                     border_style="green", expand=False))
        rep = self.ui._demander("[bold]Committer ?[/] [green]o[/]ui · [red]n[/]on · [cyan]e[/] = écrire un autre message › ")
        if rep.lower() == "e":
            message = self.ui._demander("Message › ")
            rep = "o" if message else "n"
        if rep.lower() not in {"", "o", "oui", "y"}:
            self.afficher("[dim]Commit annulé.[/]")
            return
        ok, err = outils.git(self.sb, "add", "-A", "--", ".", *outils._EXCLUSIONS_GIT)
        if not ok:
            self.afficher(f"[red]❌ git add : {escape(err[:300])}[/]")
            return
        # Commit fait au nom de l'utilisateur : ses hooks (pre-commit…) s'exécutent normalement.
        r = subprocess.run(["git", "commit", "-q", "-F", "-"], input=message, cwd=self.sb.racine,
                           capture_output=True, text=True)
        if r.returncode == 0:
            _, sha = outils.git(self.sb, "log", "--oneline", "-1")
            self.afficher(f"[green]✅ Commit : {escape(sha.strip())}[/]")
        else:
            self.afficher(f"[red]❌ git commit : {escape((r.stderr or r.stdout)[:600])}[/]")


def lancer_repl(cfg: Config, sb: Sandbox, *, mode: str, version: str, reprendre: bool = False,
                demande_initiale: str | None = None) -> int:
    return Repl(cfg, sb, mode=mode, version=version).lancer(reprendre=reprendre, demande_initiale=demande_initiale)


def executer_une_fois(cfg: Config, sb: Sandbox, demande: str, *, mode: str) -> tuple[int, str]:
    """agent_code -p "…" : une demande, une réponse, sans session interactive (scripts, CI)."""
    ui = Interface(sb, mode=mode)
    ui.brancher(sb)
    session = Session(cfg, sb, ui, mode=mode)
    res, modifies = session.envoyer(demande)
    if res.erreur and not res.texte:
        ui.ligne(f"[red]❌ {escape(res.erreur)}[/]")
        return 1, ""
    if modifies:
        ui.ligne(f"[green]📝 Fichiers modifiés : {escape(', '.join(modifies))}[/]")
    return (130 if res.interrompu else 0), res.texte

