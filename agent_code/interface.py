"""Affichage terminal (rich) et validations humaines : diffs colorés, commandes, plan, sous-agents."""

from __future__ import annotations

import difflib
import sys
import time

from .moteur import Evenements
from .securite import Sandbox, rediger_secrets

try:
    from rich.console import Console
    from rich.live import Live
    from rich.markup import escape
    from rich.panel import Panel
    from rich.spinner import Spinner
    from rich.text import Text
    RICH = True
except ImportError:  # pragma: no cover — affichage minimal sans rich
    RICH = False

    def escape(t: str) -> str:  # type: ignore[no-redef]
        return t

MODES = ("normal", "auto", "lecture")
ICONES_MODE = {"normal": "🛡️ ", "auto": "⚡", "lecture": "📖"}
DESCRIPTION_MODE = {
    "normal": "tu valides chaque modification et chaque commande",
    "auto": "modifications appliquées sans validation (/annuler pour revenir en arrière)",
    "lecture": "lecture seule : l'agent explore et propose un plan",
}
MAX_LIGNES_DIFF = 120


def _console() -> Console | None:
    return Console(highlight=False, soft_wrap=False) if RICH else None


class _Attente:
    """Spinner avec chronomètre (re-rendu à chaque rafraîchissement du Live)."""

    def __init__(self, message: str) -> None:
        self.debut = time.monotonic()
        self.message = message
        self.spinner = Spinner("dots", style="cyan")

    def __rich__(self):
        s = int(time.monotonic() - self.debut)
        self.spinner.update(text=Text(f" {self.message} {s}s · Ctrl+C pour interrompre", style="dim"))
        return self.spinner


def resume_arguments(nom: str, args: dict) -> str:
    a = {k: v for k, v in (args or {}).items() if v not in ("", None)}
    if nom == "Lire_Fichier":
        plage = ""
        if a.get("ligne_debut") or a.get("ligne_fin"):
            plage = f" l.{a.get('ligne_debut', 1)}-{a.get('ligne_fin') or 'fin'}"
        return f"{a.get('chemin', '?')}{plage}"
    if nom == "Rechercher_Code":
        return f"« {a.get('motif', '')} »" + (f" dans *{a['extension']}" if a.get("extension") else "") \
            + (f" ({a['chemin']})" if a.get("chemin") not in (None, ".") else "")
    if nom == "Deleguer":
        return f"{a.get('type_agent', '?')} : {str(a.get('tache', ''))[:90]}"
    if nom in {"Ecrire_Fichier", "Modifier_Fichier"}:
        return str(a.get("chemin", "?"))
    for v in a.values():
        if isinstance(v, str):
            return v[:100] + ("…" if len(v) > 100 else "")
    return ", ".join(f"{k}={v}" for k, v in a.items())[:100]


def resume_resultat(resultat: str) -> tuple[str, bool]:
    lignes = [x for x in (resultat or "").splitlines() if x.strip()]
    if not lignes:
        return "(vide)", False
    erreur = lignes[0].lstrip().startswith(("❌", "⛔"))
    premiere = lignes[0].strip()
    premiere = premiere[:160] + ("…" if len(premiere) > 160 else "")
    if len(lignes) > 1 and not erreur:
        premiere += f"  (+{len(lignes) - 1} lignes)"
    if erreur and len(lignes) > 1:
        premiere += " " + " ".join(x.strip() for x in lignes[1:3])[:200]
    return premiere, erreur


def stat_diff(avant: str | None, apres: str) -> tuple[int, int]:
    plus = moins = 0
    for ligne in difflib.unified_diff((avant or "").splitlines(), apres.splitlines(), lineterm="", n=0):
        if ligne.startswith("+") and not ligne.startswith("+++"):
            plus += 1
        elif ligne.startswith("-") and not ligne.startswith("---"):
            moins += 1
    return plus, moins


class Interface(Evenements):
    def __init__(self, sb: Sandbox | None = None, *, mode: str = "normal", console=None, prefixe: str = "",
                 afficher_texte: bool = True, parent: Interface | None = None) -> None:
        self.sb = sb
        self.mode = mode
        self.console = console if console is not None else _console()
        self.prefixe = prefixe
        self.afficher_texte = afficher_texte
        self.parent = parent
        self._live = None
        self._en_ligne = False      # du texte streamé sans retour à la ligne final
        self._texte_emis = False

    # -- primitives -------------------------------------------------------
    @property
    def terminal(self) -> bool:
        return bool(self.console and self.console.is_terminal)

    def ligne(self, texte: str, style: str = "") -> None:
        self._arreter_attente()
        self._finir_ligne()
        if self.console:
            self.console.print(Text(self.prefixe) + Text.from_markup(texte, style=style) if self.prefixe
                               else Text.from_markup(texte, style=style))
        else:
            print(self.prefixe + texte)

    def _finir_ligne(self) -> None:
        if self._en_ligne:
            (self.console.file if self.console else sys.stdout).write("\n")
            self._en_ligne = False

    def _arreter_attente(self) -> None:
        racine = self._racine()
        if racine._live is not None:
            racine._live.stop()
            racine._live = None

    def _racine(self) -> Interface:
        return self.parent._racine() if self.parent else self

    # -- événements du moteur -----------------------------------------------
    def debut_reflexion(self) -> None:
        self._texte_emis = False
        racine = self._racine()
        if not self.terminal or racine._live is not None:
            return
        msg = "Réflexion…" if not self.prefixe else "Sous-agent au travail…"
        racine._live = Live(_Attente(msg), console=self.console, refresh_per_second=8, transient=True)
        racine._live.start()

    def texte(self, morceau: str) -> None:
        if not self.afficher_texte:
            return
        self._arreter_attente()
        sortie = self.console.file if self.console else sys.stdout
        if not self._texte_emis:
            if self.console:
                self.console.print()
            self._texte_emis = True
        sortie.write(morceau)
        sortie.flush()
        self._en_ligne = not morceau.endswith("\n")

    def fin_texte(self) -> None:
        self._arreter_attente()
        self._finir_ligne()

    def outil_debut(self, nom: str, args: dict) -> None:
        if nom == "Mettre_A_Jour_Plan":
            return
        icone = "◆" if nom == "Deleguer" else "●"
        style = "bold magenta" if nom == "Deleguer" else "bold"
        self.ligne(f"[{style}]{icone} {escape(nom)}[/]([cyan]{escape(resume_arguments(nom, args))}[/])")

    def outil_fin(self, nom: str, args: dict, resultat: str, duree: float) -> None:
        if nom == "Mettre_A_Jour_Plan":
            return
        resume, erreur = resume_resultat(rediger_secrets(resultat))
        temps = f" [dim]· {duree:.0f}s[/]" if duree >= 2 else ""
        self.ligne(f"  [dim]⎿[/] [{'red' if erreur else 'dim'}]{escape(resume)}[/]{temps}")

    def plan(self, items: list[tuple[str, str]]) -> None:
        symboles = {"fait": "[green]☑[/]", "en_cours": "[yellow]◐[/]", "a_faire": "[dim]☐[/]"}
        lignes = [f"{symboles.get(e, '☐')} " + (f"[dim strike]{escape(t)}[/]" if e == "fait" else
                                                 f"[bold]{escape(t)}[/]" if e == "en_cours" else escape(t))
                  for e, t in items]
        if self.console and not self.prefixe:
            self._arreter_attente()
            self._finir_ligne()
            self.console.print(Panel("\n".join(lignes), title="📋 Plan", title_align="left", border_style="blue",
                                     expand=False))
        else:
            for x in lignes:
                self.ligne(x)

    def info(self, message: str) -> None:
        self.ligne(f"[yellow]{escape(message)}[/]")

    def sous_agent(self, nom: str, tache: str) -> Interface:
        return Interface(self.sb, mode=self.mode, console=self.console, prefixe=self.prefixe + "  │ ",
                         afficher_texte=False, parent=self)

    def fin_sous_agent(self, nom: str, resultat: str, duree: float) -> None:
        pass

    # -- validations ----------------------------------------------------------
    def _demander(self, invite: str) -> str:
        self._arreter_attente()
        self._finir_ligne()
        try:
            if self.console:
                return self.console.input(invite).strip()
            return input(invite).strip()
        except EOFError:
            return "n"

    def valider_ecriture(self, rel: str, avant: str | None, apres: str) -> bool | str:
        racine = self._racine()
        plus, moins = stat_diff(avant, apres)
        titre = f"{'✏️  Modifier' if avant is not None else '📄 Créer'} {rel}  [green]+{plus}[/] [red]−{moins}[/]"
        if racine.mode == "auto":
            self.ligne(f"  [dim]⎿[/] {titre} [dim](mode auto)[/]")
            return True
        if self.sb is not None and not self.sb.interactif:
            self.ligne(f"  [dim]⎿[/] {titre} [red](refusé : session non interactive, utilise --mode auto)[/]")
            return ("session non interactive sans validation possible : décris les changements à faire "
                    "au lieu de les appliquer (l'utilisateur peut relancer avec --mode auto)")
        self.afficher_diff(rel, avant, apres, titre)
        rep = self._demander("[bold]Appliquer ?[/] [green]o[/]ui · [red]n[/]on · "
                             "[cyan]t[/]oujours (passe en mode auto) · ou tape une consigne › ")
        return self._interpreter(rep, sur_toujours=lambda: self._passer_auto())

    def _passer_auto(self) -> None:
        self._racine().mode = "auto"
        self.ligne("[cyan]⚡ Mode auto : les prochaines modifications seront appliquées sans validation "
                   "(⇧Tab ou /mode pour revenir).[/]")

    def demander(self, titre: str, details: str, genre: str, argv: list[str] | None) -> bool | str:
        prefixe_cmd = ""
        if genre == "commande" and argv:
            prefixe_cmd = " ".join(argv[:2] if len(argv) > 1 and not argv[1].startswith("-") else argv[:1])
        icone = {"commande": "⚙️ ", "tests": "🧪", "web": "🌐"}.get(genre, "⚠️ ")
        corps = escape(rediger_secrets(details))
        if self.console:
            self._arreter_attente()
            self._finir_ligne()
            self.console.print(Panel(corps, title=f"{icone} {escape(titre)}", title_align="left",
                                     border_style="yellow", expand=False))
        else:
            print(f"{icone} {titre}\n{details}")
        toujours = {"commande": f"toujours autoriser « {prefixe_cmd} » (session)",
                    "tests": "toujours lancer les tests (session)",
                    "web": "toujours autoriser les pages web (session)"}.get(genre, "")
        invite = "[bold]Autoriser ?[/] [green]o[/]ui · [red]n[/]on · " + \
                 (f"[cyan]t[/] = {escape(toujours)} · " if toujours else "") + "ou tape une consigne › "

        def toujours_fn() -> None:
            if self.sb is None:
                return
            if genre == "commande" and prefixe_cmd:
                self.sb.commandes_autorisees = (*self.sb.commandes_autorisees, tuple(prefixe_cmd.split()))
            elif genre == "tests":
                self.sb.autoriser_tests = True
            elif genre == "web":
                self.sb.autoriser_pages_web = True
        return self._interpreter(self._demander(invite), sur_toujours=toujours_fn if toujours else None)

    @staticmethod
    def _interpreter(rep: str, sur_toujours=None) -> bool | str:
        r = rep.strip()
        if r.lower() in {"", "o", "oui", "y", "yes"}:
            return True
        if r.lower() in {"t", "toujours", "a", "always"} and sur_toujours:
            sur_toujours()
            return True
        if r.lower() in {"n", "non", "no"}:
            return False
        return r   # consigne libre : refus + instruction transmise à l'agent

    def afficher_diff(self, rel: str, avant: str | None, apres: str, titre: str | None = None) -> None:
        lignes = list(difflib.unified_diff((avant or "").splitlines(), apres.splitlines(), lineterm="", n=3))[2:]
        if avant is None:
            lignes = [f"+{x}" for x in apres.splitlines()]
        rendu = Text() if RICH else None
        affichees = lignes[:MAX_LIGNES_DIFF]
        for x in affichees:
            x = rediger_secrets(x)
            if rendu is None:
                continue
            if x.startswith("@@"):
                rendu.append(x + "\n", style="cyan")
            elif x.startswith("+"):
                rendu.append(x + "\n", style="green")
            elif x.startswith("-"):
                rendu.append(x + "\n", style="red")
            else:
                rendu.append(x + "\n", style="dim")
        if len(lignes) > MAX_LIGNES_DIFF:
            reste = f"[... {len(lignes) - MAX_LIGNES_DIFF} lignes de plus ...]"
            if rendu is not None:
                rendu.append(reste, style="dim italic")
        self._arreter_attente()
        self._finir_ligne()
        if self.console:
            self.console.print(Panel(rendu or Text("(aucun changement)"), title=titre or rel, title_align="left",
                                     border_style="blue"))
        else:
            print(titre or rel)
            print("\n".join(affichees))

    # -- branchement sur la sandbox ---------------------------------------------
    def brancher(self, sb: Sandbox) -> None:
        self.sb = sb
        sb.demandeur = self.demander
        sb.valideur_ecriture = self.valider_ecriture
