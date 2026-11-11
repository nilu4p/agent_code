"""Tests du moteur agentique, du client Ollama, du journal d'annulation et de la session (sans Ollama)."""

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_code import outils  # noqa: E402
from agent_code.journal import Journal  # noqa: E402
from agent_code.llm import AppelOutil, ClientOllama, ErreurLLM, ReponseLLM, extraire_appels_texte  # noqa: E402
from agent_code.moteur import Agent, Evenements, FiltreAppels, ReglagesAgent  # noqa: E402
from agent_code.outils import Outil, tool  # noqa: E402
from agent_code.securite import Sandbox  # noqa: E402


class FauxClient:
    """Rejoue une liste de réponses ; mémorise les requêtes reçues."""

    def __init__(self, reponses):
        self.reponses = list(reponses)
        self.requetes = []

    def chat(self, modele, messages, *, outils=None, options=None, sur_texte=None, keep_alive="30m"):
        self.requetes.append({"messages": [dict(m) for m in messages], "outils": outils})
        rep = self.reponses.pop(0)
        if isinstance(rep, BaseException):
            raise rep
        if callable(rep):
            rep = rep(messages)
        if sur_texte and rep.texte:
            sur_texte(rep.texte)
        return rep


def appel(nom, **args):
    return ReponseLLM("", [AppelOutil(nom, args)])


def texte(t):
    return ReponseLLM(t)


class Projet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.racine = Path(self.tmp.name) / "projet"
        self.racine.mkdir()
        (self.racine / "app.py").write_text("def f(x):\n    return x\n")
        self.sb = Sandbox(self.racine, interactif=False)
        outils.configurer(self.sb)

    def tearDown(self):
        self.tmp.cleanup()

    def agent(self, reponses, outils_=None, **k):
        client = FauxClient(reponses)
        a = Agent(client, ReglagesAgent("faux", contexte=32768, max_etapes=k.pop("max_etapes", 10)), "système",
                  outils_ if outils_ is not None else [outils.lire_fichier, outils.modifier_fichier], **k)
        return a, client


class TestOutil(unittest.TestCase):
    def test_schema_depuis_signature(self):
        s = outils.lire_fichier.schema()["function"]
        self.assertEqual(s["name"], "Lire_Fichier")
        self.assertEqual(s["parameters"]["required"], ["chemin"])
        self.assertEqual(s["parameters"]["properties"]["ligne_debut"]["type"], "integer")

    def test_normaliser_alias_et_types(self):
        args, err = outils.lire_fichier.normaliser({"path": "a.py", "ligne_debut": "5"})
        self.assertIsNone(err)
        self.assertEqual(args, {"chemin": "a.py", "ligne_debut": 5})
        _, err = outils.lire_fichier.normaliser({})
        self.assertIn("manquant", err)

    def test_argument_inconnu_unique_associe_au_requis(self):
        args, err = outils.lire_fichier.normaliser({"emplacement": "a.py"})
        self.assertIsNone(err)
        self.assertEqual(args["chemin"], "a.py")

    def test_decorateur(self):
        @tool("Bonjour")
        def bonjour(nom: str, poli: bool = True) -> str:
            """Dit bonjour."""
            return ("Bonjour " if poli else "Salut ") + nom
        self.assertIsInstance(bonjour, Outil)
        self.assertEqual(bonjour.executer({"nom": "Léa", "poli": "false"}), "Salut Léa")


class TestReplisTexte(unittest.TestCase):
    def test_format_qwen_xml(self):
        t = ("Je lis le fichier.\n<tool_call>\n<function=Lire_Fichier>\n<parameter=chemin>\napp.py\n"
             "</parameter>\n</function>\n</tool_call>")
        appels, propre = extraire_appels_texte(t, {"Lire_Fichier"})
        self.assertEqual([(a.nom, a.arguments) for a in appels], [("Lire_Fichier", {"chemin": "app.py"})])
        self.assertEqual(propre, "Je lis le fichier.")

    def test_format_json(self):
        t = '<tool_call>{"name": "Lire_Fichier", "arguments": {"chemin": "a.py"}}</tool_call>'
        appels, _ = extraire_appels_texte(t, {"Lire_Fichier"})
        self.assertEqual(appels[0].arguments, {"chemin": "a.py"})

    def test_outil_inconnu_ignore(self):
        appels, propre = extraire_appels_texte("<function=rm_rf></function>", {"Lire_Fichier"})
        self.assertEqual(appels, [])

    def test_filtre_affichage(self):
        sortie = []
        f = FiltreAppels(sortie.append)
        for morceau in ["Voici ", "le plan <", "tool_", "call>\n<function=X>", " secret"]:
            f.ajouter(morceau)
        f.terminer()
        self.assertEqual("".join(sortie), "Voici le plan ")
        sortie2 = []
        f2 = FiltreAppels(sortie2.append)
        f2.ajouter("a < b et <c")
        f2.terminer()
        self.assertEqual("".join(sortie2), "a < b et <c")


class TestAgent(Projet):
    def test_boucle_outil_puis_reponse(self):
        a, client = self.agent([appel("Lire_Fichier", chemin="app.py"), texte("La fonction f renvoie x.")])
        res = a.tour("que fait app.py ?")
        self.assertEqual(res.texte, "La fonction f renvoie x.")
        self.assertEqual(res.etapes, 2)
        roles = [m["role"] for m in a.messages]
        self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])
        self.assertIn("return x", a.messages[2]["content"])
        self.assertEqual(client.requetes[0]["messages"][0], {"role": "system", "content": "système"})

    def test_erreur_d_outil_renvoyee_au_modele(self):
        a, _ = self.agent([appel("Lire_Fichier", chemin="../../etc/passwd"), appel("Inexistant"), texte("ok")])
        a.tour("x")
        self.assertIn("⛔", a.messages[2]["content"])
        self.assertIn("Outil inconnu", a.messages[4]["content"])

    def test_garde_fou_repetition(self):
        r = appel("Lire_Fichier", chemin="app.py")
        a, _ = self.agent([r, r, r, texte("fini")])
        a.tour("x")
        self.assertIn("déjà fait exactement cet appel", a.messages[6]["content"])

    def test_limite_d_etapes(self):
        a, client = self.agent([appel("Lire_Fichier", chemin="app.py", ligne_debut=i) for i in range(1, 4)]
                               + [texte("bilan")], max_etapes=3)
        res = a.tour("x")
        self.assertEqual(res.texte, "bilan")
        self.assertIn("limite", res.erreur)
        self.assertIsNone(client.requetes[-1]["outils"])   # le bilan est demandé sans outils

    def test_interruption_garde_un_historique_coherent(self):
        a, _ = self.agent([appel("Lire_Fichier", chemin="app.py"), KeyboardInterrupt()])
        res = a.tour("x")
        self.assertTrue(res.interrompu)
        roles = [m["role"] for m in a.messages]
        self.assertEqual(roles[:3], ["user", "assistant", "tool"])
        self.assertEqual(roles[-1], "assistant")
        # tours suivants possibles
        a.client.reponses = [texte("reprise")]
        self.assertEqual(a.tour("continue").texte, "reprise")

    def test_erreur_llm_retire_la_demande(self):
        a, _ = self.agent([ErreurLLM("Ollama injoignable")])
        res = a.tour("x")
        self.assertIn("injoignable", res.erreur)
        self.assertEqual(a.messages, [])

    def test_plan(self):
        recus = []

        class Ev(Evenements):
            def plan(self, items):
                recus.append(items)
        a, _ = self.agent([appel("Mettre_A_Jour_Plan", plan="[x] lire\n[~] modifier\n- [ ] tester"), texte("ok")],
                          evenements=Ev())
        a.tour("x")
        self.assertEqual(recus[0], [("fait", "lire"), ("en_cours", "modifier"), ("a_faire", "tester")])

    def test_sous_agent(self):
        sous_client = FauxClient([appel("Lire_Fichier", chemin="app.py"), texte("f est définie app.py:1")])

        def fabrique(cle):
            return Agent(sous_client, ReglagesAgent("faux"), "sous-système", [outils.lire_fichier], nom=cle)
        a, client = self.agent([appel("Deleguer", type_agent="explorateur", tache="où est f ?"), texte("Réponse.")],
                               fabrique_sous_agent=fabrique, sous_agents={"explorateur": "explore"})
        self.assertIn("Deleguer", a.noms_outils)
        res = a.tour("où est f ?")
        self.assertEqual(res.texte, "Réponse.")
        self.assertIn("f est définie app.py:1", a.messages[2]["content"])
        # Le contexte du sous-agent ne pollue pas l'agent principal
        self.assertNotIn("return x", json.dumps(a.messages))
        self.assertEqual(sous_client.requetes[0]["messages"][0]["content"], "sous-système")
        self.assertEqual(a.stats.appels_llm, 4)

    def test_sous_agent_inconnu(self):
        a, _ = self.agent([appel("Deleguer", type_agent="pirate", tache="x"), texte("ok")],
                          fabrique_sous_agent=lambda c: None, sous_agents={"explorateur": "explore"})
        a.tour("x")
        self.assertIn("inconnu", a.messages[2]["content"])

    def test_allegement_puis_resume(self):
        a, client = self.agent([texte("résumé : on a lu app.py"), texte("suite")])
        a.reglages.contexte = 4096
        gros = "x" * 20000
        a.messages = [{"role": "user", "content": "lis tout"},
                      {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "Lire_Fichier",
                                                                                         "arguments": {}}}]},
                      {"role": "tool", "tool_name": "Lire_Fichier", "content": gros},
                      {"role": "assistant", "content": "lu"}]
        a.tour("et maintenant ?")
        self.assertIn("on a lu app.py", a.resume)
        self.assertIn("RÉSUMÉ", client.requetes[-1]["messages"][0]["content"])
        self.assertLess(a.taux_contexte(), 0.7)

    def test_export_import(self):
        a, _ = self.agent([texte("ok")])
        a.tour("salut")
        b, _ = self.agent([])
        b.importer(json.loads(json.dumps(a.exporter())))
        self.assertEqual(b.messages, a.messages)


class TestJournal(Projet):
    def test_annuler_modification_et_creation(self):
        self.sb.journal = Journal(self.racine)
        self.sb.journal.nouveau_tour("tour 1")
        outils.modifier_fichier.func("app.py", "return x", "return x * 2")
        outils.ecrire_fichier.func("nouveau.py", "y = 1\n")
        self.assertEqual(self.sb.journal.fichiers_du_tour(), ["app.py", "nouveau.py"])
        self.assertIn("+    return x * 2", self.sb.journal.diff_session())
        titre, fichiers = self.sb.journal.annuler_dernier()
        self.assertEqual(titre, "tour 1")
        self.assertEqual((self.racine / "app.py").read_text(), "def f(x):\n    return x\n")
        self.assertFalse((self.racine / "nouveau.py").exists())
        self.assertIsNone(self.sb.journal.annuler_dernier())

    def test_annule_le_dernier_tour_non_vide(self):
        j = self.sb.journal = Journal(self.racine)
        j.nouveau_tour("a")
        outils.modifier_fichier.func("app.py", "return x", "return 1")
        j.nouveau_tour("b (question sans modification)")
        self.assertEqual(j.annuler_dernier()[0], "a")


class TestValidationEcriture(Projet):
    def test_refus_avec_consigne(self):
        vus = []

        def valideur(rel, avant, apres):
            vus.append((rel, avant is None))
            return "utilise plutôt une constante"
        self.sb.valideur_ecriture = valideur
        res = outils.ecrire_fichier.func("b.py", "x = 1\n")
        self.assertIn("refusée", res)
        self.assertIn("utilise plutôt une constante", res)
        self.assertFalse((self.racine / "b.py").exists())
        self.assertEqual(vus, [("b.py", True)])

    def test_acceptation(self):
        self.sb.valideur_ecriture = lambda rel, avant, apres: True
        self.assertIn("✅", outils.modifier_fichier.func("app.py", "return x", "return 2"))

    def test_lecture_seule_prioritaire(self):
        self.sb.lecture_seule = True
        self.sb.valideur_ecriture = lambda *a: True
        self.assertIn("lecture seule", outils.ecrire_fichier.func("c.py", "x"))

    def test_demandeur_consigne_transmise(self):
        sb = Sandbox(self.racine, interactif=True)
        sb.demandeur = lambda titre, details, genre, argv: "lance plutôt pytest -x"
        res = sb.executer("touch z.txt")
        self.assertIn("lance plutôt pytest -x", res)
        self.assertFalse((self.racine / "z.txt").exists())


class TestInterface(Projet):
    def test_non_interactif_refuse_les_ecritures_en_mode_normal(self):
        from agent_code.interface import Interface
        ui = Interface(self.sb, mode="normal", console=None)
        ui.console = None
        self.sb.valideur_ecriture = ui.valider_ecriture
        self.assertIn("non interactive", outils.ecrire_fichier.func("d.py", "x"))
        ui.mode = "auto"
        self.assertIn("✅", outils.ecrire_fichier.func("d.py", "x"))

    def test_interpretation_des_reponses(self):
        from agent_code.interface import Interface
        self.assertIs(Interface._interpreter(""), True)
        self.assertIs(Interface._interpreter("n"), False)
        appele = []
        self.assertIs(Interface._interpreter("t", sur_toujours=lambda: appele.append(1)), True)
        self.assertEqual(appele, [1])
        self.assertEqual(Interface._interpreter("garde l'ancien nom"), "garde l'ancien nom")

    def test_resumes(self):
        from agent_code.interface import resume_arguments, resume_resultat
        self.assertEqual(resume_arguments("Lire_Fichier", {"chemin": "a.py", "ligne_debut": 3, "ligne_fin": 9}),
                         "a.py l.3-9")
        self.assertEqual(resume_resultat("❌ raté\ndétail")[1], True)
        self.assertIn("+2 lignes", resume_resultat("a\nb\nc")[0])


class _FauxOllamaStream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        corps = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).dernier = corps
        if corps["model"] == "absent":
            msg = json.dumps({"error": "model 'absent' not found"}).encode()
            self.send_response(404)
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
            return
        lignes = [{"message": {"role": "assistant", "content": "Je "}, "done": False},
                  {"message": {"role": "assistant", "content": "regarde."}, "done": False},
                  {"message": {"role": "assistant", "content": "", "tool_calls": [
                      {"function": {"name": "Lire_Fichier", "arguments": {"chemin": "app.py"}}}]}, "done": False},
                  {"message": {"role": "assistant", "content": ""}, "done": True, "prompt_eval_count": 120,
                   "eval_count": 7, "total_duration": 2_000_000_000}]
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for x in lignes:
            self.wfile.write((json.dumps(x) + "\n").encode())
            self.wfile.flush()


class TestClientOllama(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.serveur = HTTPServer(("127.0.0.1", 0), _FauxOllamaStream)
        threading.Thread(target=cls.serveur.serve_forever, daemon=True).start()
        cls.client = ClientOllama(f"http://127.0.0.1:{cls.serveur.server_port}")

    @classmethod
    def tearDownClass(cls):
        cls.serveur.shutdown()

    def test_streaming_et_appels(self):
        morceaux = []
        rep = self.client.chat("m", [{"role": "user", "content": "x"}], outils=[outils.lire_fichier.schema()],
                               options={"num_ctx": 32768}, sur_texte=morceaux.append)
        self.assertEqual(morceaux, ["Je ", "regarde."])
        self.assertEqual(rep.appels[0].nom, "Lire_Fichier")
        self.assertEqual((rep.tokens_prompt, rep.tokens_sortie, rep.duree_s), (120, 7, 2.0))
        envoye = _FauxOllamaStream.dernier
        self.assertEqual(envoye["options"]["num_ctx"], 32768)
        self.assertTrue(envoye["stream"])

    def test_modele_absent(self):
        with self.assertRaises(ErreurLLM) as ctx:
            self.client.chat("absent", [])
        self.assertIn("ollama pull", str(ctx.exception))

    def test_serveur_injoignable(self):
        with self.assertRaises(ErreurLLM) as ctx:
            ClientOllama("http://127.0.0.1:9", timeout=2).chat("m", [])
        self.assertIn("injoignable", str(ctx.exception))


class TestSession(Projet):
    def setUp(self):
        super().setUp()
        from agent_code import config
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"AGENT_CODE_CONFIG": "/inexistant.env", "AGENT_CODE_WEB": "false"}), \
                mock.patch.object(config, "fichiers_config_candidats", return_value=[]):
            self.cfg = config.charger_config()
        from agent_code.session import Session
        self.session = Session(self.cfg, self.sb, Evenements(), mode="normal")

    def test_mentions(self):
        (self.racine / "src").mkdir()
        (self.racine / "src" / "m.py").write_text("VALEUR = 42\n")
        (self.racine / ".env").write_text("SECRET=1\n")
        texte, joints = self.session.developper_mentions("regarde @src/m.py et @.env et toto@exemple.com")
        self.assertEqual(joints, ["src/m.py"])
        self.assertIn("VALEUR = 42", texte)
        self.assertNotIn("SECRET", texte)

    def test_modes(self):
        noms = lambda: set(self.session.agent.noms_outils)  # noqa: E731
        self.assertIn("Ecrire_Fichier", noms())
        self.session.appliquer_mode("lecture")
        self.assertNotIn("Ecrire_Fichier", noms())
        self.assertTrue(self.sb.lecture_seule)
        self.assertIn("MODE : lecture", self.session.agent.systeme)
        self.session.appliquer_mode("normal")
        self.assertFalse(self.sb.lecture_seule)
        self.assertNotIn("chercheur", self.session._descriptions_sous_agents())   # web désactivé

    def test_sauvegarde_et_reprise(self):
        self.session.agent.client = FauxClient([texte("bonjour !")])
        res, modifies = self.session.envoyer("salut")
        self.assertEqual((res.texte, modifies), ("bonjour !", []))
        from agent_code.session import Session
        autre = Session(self.cfg, self.sb, Evenements())
        precedentes = autre.sessions_precedentes()
        self.assertEqual(precedentes[0]["titre"], "salut")
        self.assertEqual(autre.reprendre(precedentes[0]["fichier"]), 2)

    def test_contexte_contient_instructions_projet(self):
        (self.racine / "AGENT_CODE.md").write_text("# Projet\nToujours utiliser pytest.\n")
        self.session.rafraichir_contexte()
        self.assertIn("Toujours utiliser pytest", self.session.agent.systeme)
        self.assertEqual(self.session.contexte.fichier_instructions, "AGENT_CODE.md")


if __name__ == "__main__":
    unittest.main()



class TestRepliJsonMarkdown(unittest.TestCase):
    def test_bloc_json(self):
        t = ('Allons-y.\n1. Lancer les tests :\n   ```json\n   {"name": "Executer_Commande", "arguments": '
             '{"commande": "python3 -m unittest"}}\n   ```\n2. Corriger…\n```json\n{"name": "Lire_Fichier", '
             '"arguments": {"chemin": "a.py"}}\n```')
        appels, propre = extraire_appels_texte(t, {"Executer_Commande", "Lire_Fichier"})
        self.assertEqual([(a.nom, a.arguments) for a in appels],
                         [("Executer_Commande", {"commande": "python3 -m unittest"})])
        self.assertTrue(propre.startswith("Allons-y."))

    def test_json_sans_rapport(self):
        appels, _ = extraire_appels_texte('```json\n{"name": "config", "valeur": 1}\n```', {"Lire_Fichier"})
        self.assertEqual(appels, [])


class TestFuiteStreamingOllama(unittest.TestCase):
    """Ollama 0.40 (qwen3-coder, streaming) : le 1er appel fuit dans le texte, les suivants sont structurés."""

    class Serveur(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            lignes = [{"message": {"content": "Je regarde.\n\n<function=Lire_Fichier>\n<parameter=chemin>\na.py\n"}},
                      {"message": {"content": "</parameter>\n</function>\n</tool_call>"}},
                      {"message": {"content": "", "tool_calls": [
                          {"function": {"name": "Trouver_Fichiers", "arguments": {"motif": "*.py"}}}]}},
                      {"message": {"content": ""}, "done": True}]
            self.send_response(200)
            self.end_headers()
            for x in lignes:
                self.wfile.write((json.dumps(x) + "\n").encode())

    def test_fusion_dans_l_ordre(self):
        serveur = HTTPServer(("127.0.0.1", 0), self.Serveur)
        threading.Thread(target=serveur.serve_forever, daemon=True).start()
        try:
            c = ClientOllama(f"http://127.0.0.1:{serveur.server_port}")
            rep = c.chat("m", [], outils=[outils.lire_fichier.schema(), outils.trouver_fichiers.schema()])
        finally:
            serveur.shutdown()
        self.assertEqual([(a.nom, a.arguments) for a in rep.appels],
                         [("Lire_Fichier", {"chemin": "a.py"}), ("Trouver_Fichiers", {"motif": "*.py"})])
        self.assertEqual(rep.texte, "Je regarde.")

    def test_json_sur_plusieurs_lignes(self):
        t = '{"name": "Lire_Fichier", "arguments": {"chemin": "a.py"}}\n{"name": "Trouver_Fichiers", "arguments": {}}'
        appels, _ = extraire_appels_texte(t, {"Lire_Fichier", "Trouver_Fichiers"})
        self.assertEqual(appels[0].nom, "Lire_Fichier")

    def test_bloc_python(self):
        t = 'Étape 1 :\n```python\n{"name": "Lire_Fichier", "arguments": {"chemin": "a.py"}}\n```'
        appels, _ = extraire_appels_texte(t, {"Lire_Fichier"})
        self.assertEqual(appels[0].arguments, {"chemin": "a.py"})


class TestRelance(Projet):
    def test_relance_si_l_agent_annonce_sans_agir(self):
        a, client = self.agent([texte("Je vais d'abord lire app.py :"), appel("Lire_Fichier", chemin="app.py"),
                                texte("f renvoie x (app.py:2).")])
        res = a.tour("que fait f ?")
        self.assertEqual(res.texte, "f renvoie x (app.py:2).")
        self.assertIn("Message automatique", client.requetes[1]["messages"][-1]["content"])

    def test_pas_de_relance_pour_une_vraie_reponse(self):
        a, client = self.agent([texte("f renvoie simplement son argument.")])
        self.assertEqual(a.tour("que fait f ?").texte, "f renvoie simplement son argument.")
        self.assertEqual(len(client.requetes), 1)

    def test_relances_limitees(self):
        a, client = self.agent([texte("Mettre_A_Jour_Plan : [ ] a")] * 3)
        a.tour("x")
        self.assertEqual(len(client.requetes), 3)


class TestRelancesCiblees(unittest.TestCase):
    NOMS = ["Lire_Fichier", "Modifier_Fichier", "Executer_Commande", "Lancer_Tests"]

    def test_code_montre_au_lieu_d_etre_applique(self):
        from agent_code.moteur import RELANCE_APPLIQUER, raison_relance
        t = "Voici la correction :\n```python\ndef moyenne(v):\n    return sum(v) / len(v)\n```\nC'est tout."
        self.assertEqual(raison_relance(t, self.NOMS, demande="corrige le bug"), RELANCE_APPLIQUER)
        self.assertIsNone(raison_relance(t, self.NOMS, demande="corrige le bug", a_ecrit=True))
        self.assertIsNone(raison_relance(t, self.NOMS, demande="explique-moi moyenne"))

    def test_renvoi_a_l_utilisateur(self):
        from agent_code.moteur import RELANCE_VERIFIER, raison_relance
        t = "J'ai corrigé calc.py. Relancez les tests pour vérifier."
        self.assertEqual(raison_relance(t, self.NOMS, demande="corrige", a_ecrit=True), RELANCE_VERIFIER)
        self.assertIsNone(raison_relance(t, self.NOMS, demande="corrige", a_ecrit=True, a_verifie=True))

    def test_reponse_finale_normale(self):
        from agent_code.moteur import raison_relance
        t = "Corrigé : calc.py:3 divisait mal. Les 2 tests passent."
        self.assertIsNone(raison_relance(t, self.NOMS, demande="corrige", a_ecrit=True, a_verifie=True))
