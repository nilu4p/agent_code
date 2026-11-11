"""Tests des outils des agents (bibliothèque standard uniquement)."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_code import outils  # noqa: E402
from agent_code.securite import Sandbox  # noqa: E402


def appel(outil, *a, **k):
    return outil.func(*a, **k)


class TestOutils(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.racine = Path(self.tmp.name) / "projet"
        self.racine.mkdir()
        (self.racine / "app.py").write_text("def f(x):\n    return x\n")
        self.sb = Sandbox(self.racine, interactif=False)
        outils.configurer(self.sb)

    def tearDown(self):
        self.tmp.cleanup()

    def test_modifier_preserve_crlf(self):
        f = self.racine / "win.txt"
        f.write_bytes(b"ligne1\r\nligne2\r\nligne3\r\n")
        res = appel(outils.modifier_fichier, "win.txt", "ligne2\nligne3", "DEUX\ntrois")
        self.assertIn("✅", res)
        self.assertEqual(f.read_bytes(), b"ligne1\r\nDEUX\r\ntrois\r\n")

    def test_modifier_preserve_permissions(self):
        f = self.racine / "script.sh"
        f.write_text("echo a\n")
        f.chmod(0o755)
        appel(outils.modifier_fichier, "script.sh", "echo a", "echo b")
        self.assertEqual(f.stat().st_mode & 0o777, 0o755)

    def test_refus_ecriture_secret_masque(self):
        res = appel(outils.ecrire_fichier, "conf.py", 'PASSWORD = "***MASQUÉ***"\n')
        self.assertIn("⛔", res)
        self.assertFalse((self.racine / "conf.py").exists())

    def test_lecture_secret_masquee(self):
        (self.racine / "conf.py").write_text('password = "hunter2_tres_secret"\n')
        self.assertNotIn("hunter2_tres_secret", appel(outils.lire_fichier, "conf.py"))

    def test_lecture_plage_hors_limites(self):
        res = appel(outils.lire_fichier, "app.py", 50, 10)
        self.assertIn("lignes 2-2", res)

    def test_recherche_ignore_symlinks_et_sensibles(self):
        dehors = Path(self.tmp.name) / "dehors"
        dehors.mkdir()
        (dehors / "x.py").write_text("MOT_UNIQUE_DEHORS\n")
        os.symlink(dehors, self.racine / "lien")
        (self.racine / ".env").write_text("MOT_UNIQUE_DEHORS\n")
        self.assertEqual(appel(outils.rechercher_code, "MOT_UNIQUE_DEHORS"), "Aucun résultat.")

    def test_arborescence_ne_suit_pas_les_liens(self):
        os.symlink(self.racine, self.racine / "boucle")  # lien cyclique
        res = appel(outils.lister_arborescence, ".", 5)
        self.assertIn("lien symbolique", res)

    def test_lancer_tests_refuse_options(self):
        self.assertIn("⛔", appel(outils.lancer_tests, "--rootdir=/"))

    def test_url_privees_refusees(self):
        for url in ["http://127.0.0.1/", "http://[::ffff:127.0.0.1]/", "file:///etc/passwd",
                    "http://user:pw@example.com/", "http://169.254.169.254/latest/meta-data"]:
            self.assertIn("⛔", appel(outils.lire_page_web, url), url)

    @unittest.skipUnless(subprocess.run(["git", "--version"], capture_output=True).returncode == 0, "git absent")
    def test_checkpoint_exclut_secrets_et_depot_vide(self):
        (self.racine / ".env").write_text("A=1\n")
        res = outils.faire_checkpoint(self.sb, "test")
        self.assertIn("✅", res)
        ok, suivis = outils.git(self.sb, "ls-files")
        self.assertTrue(ok)
        self.assertIn("app.py", suivis)
        self.assertNotIn(".env", suivis)
        # Second checkpoint sans changement : pas d'erreur
        self.assertIn("Rien à sauvegarder", outils.faire_checkpoint(self.sb, "re"))

    def test_lecture_seule_retire_les_outils_d_ecriture(self):
        noms = {o.name for o in outils.boite("developpeur", web=False, lecture_seule=True)}
        self.assertNotIn("Ecrire_Fichier", noms)
        self.assertNotIn("Modifier_Fichier", noms)


if __name__ == "__main__":
    unittest.main()


class TestCommandeTests(unittest.TestCase):
    def test_repli_unittest(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            r = Path(d) / "p"
            (r / "tests").mkdir(parents=True)
            sb = Sandbox(r, interactif=False)
            with mock.patch.object(outils, "_pytest_disponible", return_value=False):
                self.assertEqual(outils._commande_tests(sb, "")[-5:], ["unittest", "discover", "-v", "-s", "tests"])
                self.assertIn("tests", outils._commande_tests(sb, "tests/"))
                self.assertEqual(outils._commande_tests(sb, "tests/test_a.py::T")[-1], "tests.test_a")
            with mock.patch.object(outils, "_pytest_disponible", return_value=True):
                self.assertIn("pytest", outils._commande_tests(sb, ""))
