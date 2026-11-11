"""Journal des modifications de fichiers faites par les agents, tour par tour.

Permet /annuler (revenir à l'état d'avant le dernier tour) et /diff (tout ce que la session a changé),
avec ou sans git. Seules les écritures faites par les outils (Ecrire_Fichier, Modifier_Fichier) sont
suivies : les effets des commandes shell (npm install, formateurs…) ne le sont pas.
"""

from __future__ import annotations

import difflib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

MAX_OCTETS_SUIVIS = 5_000_000   # au-delà, le fichier d'origine n'est pas mémorisé


@dataclass
class Tour:
    titre: str
    originaux: dict[Path, bytes | None] = field(default_factory=dict)   # None = fichier créé pendant le tour


class Journal:
    def __init__(self, racine: Path) -> None:
        self.racine = racine
        self.tours: list[Tour] = []

    # -- enregistrement ---------------------------------------------------
    def nouveau_tour(self, titre: str) -> None:
        self.tours.append(Tour(titre[:120]))

    def avant_ecriture(self, p: Path) -> None:
        if not self.tours:
            self.nouveau_tour("(hors tour)")
        tour = self.tours[-1]
        if p in tour.originaux:
            return
        if p.exists():
            try:
                tour.originaux[p] = p.read_bytes() if p.stat().st_size <= MAX_OCTETS_SUIVIS else b""
            except OSError:
                tour.originaux[p] = b""
        else:
            tour.originaux[p] = None

    # -- consultation -----------------------------------------------------
    def fichiers_du_tour(self, index: int = -1) -> list[str]:
        if not self.tours:
            return []
        return sorted(self._rel(p) for p in self.tours[index].originaux)

    def originaux_session(self) -> dict[Path, bytes | None]:
        """Pour chaque fichier touché, son contenu au tout début de la session."""
        res: dict[Path, bytes | None] = {}
        for tour in self.tours:
            for p, avant in tour.originaux.items():
                res.setdefault(p, avant)
        return res

    def diff_session(self, max_car: int = 60_000) -> str:
        morceaux = []
        for p, avant in sorted(self.originaux_session().items()):
            rel = self._rel(p)
            a = (avant or b"").decode("utf-8", errors="replace").splitlines(keepends=True)
            b = p.read_bytes().decode("utf-8", errors="replace").splitlines(keepends=True) if p.exists() else []
            d = "".join(difflib.unified_diff(a, b, f"a/{rel}" if avant is not None else "/dev/null",
                                             f"b/{rel}" if p.exists() else "/dev/null"))
            if d:
                morceaux.append(d if d.endswith("\n") else d + "\n")
        texte = "".join(morceaux)
        return texte if len(texte) <= max_car else texte[:max_car] + "\n[... diff tronqué ...]\n"

    # -- annulation -------------------------------------------------------
    def annuler_dernier(self) -> tuple[str, list[str]] | None:
        """Restaure les fichiers du dernier tour ayant modifié quelque chose. Retourne (titre, fichiers)."""
        while self.tours and not self.tours[-1].originaux:
            self.tours.pop()
        if not self.tours:
            return None
        tour = self.tours.pop()
        restaures = []
        for p, avant in tour.originaux.items():
            if avant is None:
                if p.exists():
                    p.unlink()
            elif avant == b"" and p.exists() and p.stat().st_size > MAX_OCTETS_SUIVIS:
                continue   # trop gros pour avoir été mémorisé : on n'y touche pas
            else:
                _ecrire_octets(p, avant)
            restaures.append(self._rel(p))
        return tour.titre, sorted(restaures)

    def _rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.racine))
        except ValueError:
            return str(p)


def _ecrire_octets(p: Path, donnees: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    mode = p.stat().st_mode & 0o777 if p.exists() else None
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(donnees)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
