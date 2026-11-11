"""Tests du parseur .env (stdlib uniquement)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_code.config import parser_dotenv  # noqa: E402


class TestDotenv(unittest.TestCase):
    def test_commentaires_fin_de_ligne(self):
        env = parser_dotenv("AGENT_CODE_MAX_ITER=20            # anti-boucle\n"
                            "AGENT_CODE_AUTO_APPROUVER_SURES=true   # ls, git status\n"
                            "# commentaire\n\nexport A=1\n")
        self.assertEqual(env["AGENT_CODE_MAX_ITER"], "20")
        self.assertEqual(env["AGENT_CODE_AUTO_APPROUVER_SURES"], "true")
        self.assertEqual(env["A"], "1")

    def test_guillemets_et_dieses(self):
        env = parser_dotenv('A="valeur # avec dièse"  # commentaire\nB=abc#def\nC=\'x y\'\nD=\n')
        self.assertEqual(env["A"], "valeur # avec dièse")
        self.assertEqual(env["B"], "abc#def")
        self.assertEqual(env["C"], "x y")
        self.assertEqual(env["D"], "")


if __name__ == "__main__":
    unittest.main()
