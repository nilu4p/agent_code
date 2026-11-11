# 12. Développement et contribution

[← Technologies](11-technologies.md) · [Sommaire](README.md) · [Dépannage et FAQ →](13-depannage-faq.md)

## Table des matières

- [12.1 Structure du dépôt](#121-structure-du-dépôt)
- [12.2 Mettre en place l'environnement](#122-mettre-en-place-lenvironnement)
- [12.3 Les tests](#123-les-tests)
- [12.4 Conventions de code](#124-conventions-de-code)
- [12.5 Ajouter un outil](#125-ajouter-un-outil)
- [12.6 Ajouter une mission](#126-ajouter-une-mission)
- [12.7 Ajouter un rôle de mission](#127-ajouter-un-rôle-de-mission)
- [12.8 Ajouter un sous-agent](#128-ajouter-un-sous-agent)
- [12.9 Ajouter une commande `/`](#129-ajouter-une-commande-)
- [12.10 Ajouter une variable de configuration](#1210-ajouter-une-variable-de-configuration)
- [12.11 Modifier la politique de sécurité](#1211-modifier-la-politique-de-sécurité)
- [12.12 Prendre en charge un autre modèle](#1212-prendre-en-charge-un-autre-modèle)
- [12.13 Checklist avant de proposer une modification](#1213-checklist-avant-de-proposer-une-modification)

---

## 12.1 Structure du dépôt

```
agent_code/                     ← dépôt
├── agent_code/                 ← paquet Python
│   ├── __init__.py
│   ├── cli.py                  ligne de commande, aiguillage, codes de sortie
│   ├── config.py               configuration (.env, variables, bornes)
│   ├── defaut.env              modèle de configuration (copié par --init-config)
│   ├── diagnostic.py           agent_code --diagnostic
│   ├── interface.py            affichage rich, diffs, validations
│   ├── journal.py              journal d'annulation (/annuler, /diff)
│   ├── llm.py                  client Ollama, gabarit qwen3-coder, appels textuels
│   ├── missions.py             catalogue des missions
│   ├── moteur.py               boucle agentique
│   ├── orchestrateur.py        exécution des missions
│   ├── outils.py               outils des agents, git déterministe
│   ├── profils.py              prompts, sous-agents, rôles
│   ├── repl.py                 session interactive
│   ├── securite.py             bac à sable, politique, secrets, audit, verrou
│   └── session.py              agent principal, mentions, sauvegarde
├── tests/                      106 tests unittest, sans Ollama
│   ├── test_config.py
│   ├── test_exploitation.py
│   ├── test_moteur.py
│   ├── test_outils.py
│   └── test_securite.py
├── deploy/
│   └── systemd/                agent_code-revue.service / .timer
├── docs/                       cette documentation
├── main.py                     lanceur sans installation
├── pyproject.toml              métadonnées, dépendances épinglées, point d'entrée, ruff
├── requirements.txt            dépendances (installation git clone)
├── Makefile                    installer, dev, test, lint, diagnostic, modeles, docker
├── setup_modeles.sh            téléchargement du modèle + mise à jour de la config
├── Dockerfile / docker-compose.yml / .dockerignore
├── .env.example                identique à agent_code/defaut.env (vérifié par un test)
├── LICENSE                     licence MIT
└── README.md
```

## 12.2 Mettre en place l'environnement

```bash
make dev && source .venv/bin/activate    # venv + pip install -e '.[outils]'
make test                                # tests
make lint                                # ruff check agent_code tests main.py
make diagnostic                          # agent_code --diagnostic .
python main.py ~/un/projet/de/test       # lancer sans réinstaller
```

> L'installation est **éditable** (`-e`) : tes changements dans `agent_code/` sont pris en compte sans
> réinstaller. La commande globale installée par `make installer` (pipx), elle, doit être réinstallée.

## 12.3 Les tests

```bash
make test                                       # ou : python -m unittest discover tests -v
python -m unittest tests.test_securite -v       # un fichier
python -m unittest tests.test_moteur.TestAgent.test_sous_agent   # un test
```

**Aucun Ollama n'est nécessaire** : les tests utilisent un `FauxClient` (qui rejoue des réponses
prédéfinies et mémorise les requêtes) et de faux serveurs HTTP (`_FauxOllama`, `_FauxOllamaStream`) lancés
dans un thread. Chaque test travaille dans un dossier temporaire.

| Fichier | Classes | Ce qui est couvert |
|---|---|---|
| `test_config.py` | `TestDotenv` | Commentaires de fin de ligne, guillemets et `#` |
| `test_securite.py` | `TestSandbox` | Chemins normaux, traversal, évasion par lien symbolique, fichiers et dossiers sensibles, zones protégées (y compris profondes), refus de `/` et `$HOME`, commande non interactive refusée, grep récursif sans secrets, **timeout qui tue les petits-enfants**, audit masqué |
| | `TestPolitiqueCommandes` | Injections shell, commandes dangereuses, contournements (lanceurs…), commandes sûres qui écrivent → confirmation, AUTO, CONFIRMER |
| | `TestSecrets` | Masquage, texte normal intact, environnement nettoyé |
| `test_outils.py` | `TestOutils` | CRLF et permissions préservés, refus d'écrire un secret masqué, lecture masquée, plages hors limites, recherche sans liens ni sensibles, arborescence sans suivre les liens, options de tests refusées, URL privées refusées, checkpoint sans secrets et sur dépôt vide, lecture seule sans outils d'écriture |
| | `TestCommandeTests` | Repli sur unittest sans pytest |
| `test_moteur.py` | `TestOutil` | Schéma depuis la signature, alias et types, argument inconnu associé au requis, décorateur |
| | `TestReplisTexte`, `TestRepliJsonMarkdown`, `TestFuiteStreamingOllama` | Appels Qwen XML, JSON, blocs Markdown, JSON multiligne, fusion ordonnée des fuites de streaming, filtre d'affichage |
| | `TestAgent` | Boucle outil → réponse, erreur d'outil renvoyée, anti-répétition, limite d'étapes, **interruption avec historique cohérent**, erreur LLM, plan, sous-agents, allègement puis résumé, export/import |
| | `TestJournal` | Annulation de modification et création, dernier tour non vide |
| | `TestValidationEcriture`, `TestInterface` | Refus avec consigne, acceptation, priorité de la lecture seule, consigne transmise, refus non interactif, interprétation des réponses, résumés d'affichage |
| | `TestClientOllama` | Streaming et appels, modèle absent, serveur injoignable |
| | `TestSession` | Mentions, modes, sauvegarde et reprise, instructions du projet dans le contexte |
| | `TestRelance`, `TestRelancesCiblees` | Relance si annonce sans action, pas de relance pour une vraie réponse, relances limitées, code montré au lieu d'être appliqué, renvoi à l'utilisateur |
| `test_exploitation.py` | `TestConfig` | Priorité de `AGENT_CODE_CONFIG`, ordre de recherche, **`.env.example` synchronisé avec `defaut.env`** |
| | `TestListeBlanche` | Correspondance token par token, refus sans liste, la liste blanche ne couvre ni le code inline, ni les arguments dangereux, ni les commandes bloquées ; tests pré-autorisés |
| | `TestVerrou` | Deux missions simultanées |
| | `TestDiagnostic` | Modèles et capacités, alerte mémoire, calcul du cache KV, Ollama injoignable |
| | `TestBranche` | Travail isolé dans la branche, branche vide supprimée |
| | `TestDelai` | Délai dépassé, zéro = illimité |
| | `TestSilencieux` | `--silencieux` masque le texte des agents de mission |
| | `TestCLI` | `--liste`, non interactif sans mission, `--init-config` |

**Écrire un test du moteur** :

```python
class FauxClient:
    def __init__(self, reponses): self.reponses, self.requetes = list(reponses), []
    def chat(self, modele, messages, *, outils=None, options=None, sur_texte=None, keep_alive="30m"):
        self.requetes.append({"messages": [dict(m) for m in messages], "outils": outils})
        return self.reponses.pop(0)

client = FauxClient([
    ReponseLLM("", [AppelOutil("Lire_Fichier", {"chemin": "a.py"})]),
    ReponseLLM("Le fichier contient une fonction."),
])
agent = Agent(client, ReglagesAgent("test"), "système", [outils.lire_fichier])
res = agent.tour("que contient a.py ?")
assert res.texte == "Le fichier contient une fonction." and res.etapes == 2
```

## 12.4 Conventions de code

- **Langue** : identifiants, messages, docstrings et commentaires en **français** (sauf noms imposés par des
  API). Les messages affichés tutoient l'utilisateur.
- **Style** : ruff, lignes de 120 caractères, cible Python 3.11, `from __future__ import annotations`.
- **Stdlib d'abord** dans le cœur (`moteur`, `llm`, `securite`, `config`, `diagnostic`, `outils` hors web).
  Les imports de `rich`, `prompt_toolkit`, `httpx`, `ddgs` sont tardifs ou protégés par `try/except ImportError`.
- **Dataclasses** pour les structures de données ; `frozen=True` quand elles ne changent pas.
- **Outils** : renvoient toujours une chaîne, jamais d'exception vers le moteur (enveloppe `_protege`),
  préfixes `✅ ℹ️ ❌ ⛔ ⚠️` ([conventions](09-outils.md#99-conventions-des-résultats)).
- **Sécurité** : toute nouvelle interaction avec le système de fichiers passe par `Sandbox.resoudre()` ;
  toute exécution par `Sandbox.executer()` / `lancer()` ; toute sortie renvoyée au modèle par `rediger_secrets()`.
- **Exceptions qui doivent traverser** (Ctrl+C, délai) : `BaseException` (`KeyboardInterrupt`,
  `DelaiMissionDepasse`) ; ne jamais les avaler dans un `except Exception`.

## 12.5 Ajouter un outil

1. Écris la fonction dans `outils.py` avec le décorateur ; le **nom**, les **annotations** et la
   **docstring** deviennent le schéma vu par le modèle — soigne-les :

   ```python
   @tool("Compter_Lignes")
   def compter_lignes(chemin: str) -> str:
       """Compte les lignes d'un fichier texte du projet."""
       def impl() -> str:
           sb = _sb()
           p = sb.resoudre(chemin)              # ← toujours : bac à sable + fichiers sensibles
           if est_binaire(p):
               return f"ℹ️ {sb.relatif(p)} est binaire."
           n = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
           return f"✅ {sb.relatif(p)} : {n} lignes."
       return _protege(impl)                    # ← erreurs lisibles, secrets masqués, troncature
   ```

2. Ajoute-le aux **boîtes à outils** concernées : `outils_principaux()` dans `profils.py` (agent principal),
   `PROFILS` dans `outils.py` (rôles de mission), `SousAgent.outils()` (sous-agents).
3. S'il **écrit** ou **exécute du code**, ajoute-le à `OUTILS_ECRITURE` (retiré en lecture seule) et appelle
   `sb.preparer_ecriture()` / `sb.approuver()` avant d'agir.
4. Si un argument a un nom que les modèles risquent de deviner autrement, complète `ALIAS_ARGUMENTS`.
5. Optionnel : un résumé d'arguments lisible dans `interface.resume_arguments()`.
6. Ajoute des tests (`tests/test_outils.py`).

## 12.6 Ajouter une mission

Une mission est purement **déclarative** (`missions.py`) :

```python
Mission(
    "perf", "Optimiser les performances", "⚡",
    "Profilage → goulots d'étranglement → optimisation mesurée.",
    lecture_seule=False,
    invite="Que faut-il accélérer ? (endpoint, fonction, script)",
    etapes=(
        Etape("architecte",
              "Cible : « {demande} ».\nIdentifie les goulots d'étranglement probables et propose un plan "
              "d'optimisation mesurable.",
              "Plan d'optimisation priorisé, avec méthode de mesure."),
        Etape("developpeur",
              "Applique le plan pour « {demande} » et mesure avant/après. " + RAPPORT,
              "Changements, mesures avant/après.",
              (0,)),            # ← reçoit la sortie de l'étape 0
    ),
),
```

| Champ | Rôle |
|---|---|
| `cle` | Nom utilisé avec `-m` et `/mission` |
| `lecture_seule` | `True` → aucun outil d'écriture ni de test pour toutes les étapes, pas de checkpoint |
| `web_requis` | `True` → refusée si le web est désactivé |
| `Etape.agent` | Clé d'un rôle de `profils.DEFINITIONS` (et de `outils.PROFILS`) |
| `Etape.description` | Consigne ; `{demande}` est remplacé par la demande de l'utilisateur |
| `Etape.attendu` | Ajouté sous « Résultat attendu : » |
| `Etape.contexte` | Indices des étapes précédentes dont la sortie est transmise (8 000 caractères chacune) |
| `RAPPORT` | Constante à ajouter à la dernière étape : impose les sections du rapport final |

La mission apparaît automatiquement dans `--liste`, `/missions`, les choix de `-m` et la complétion.

## 12.7 Ajouter un rôle de mission

1. `profils.DEFINITIONS["cle"] = ("Intitulé", "Objectif", "Méthode")` → prompt du rôle.
2. `outils.PROFILS["cle"] = [...]` → boîte à outils (moindre privilège) ; ajoute la clé à `PROFILS_WEB` si
   le rôle peut utiliser le web.
3. Si c'est un rôle de réflexion (analyse, revue, rédaction), ajoute-le à `orchestrateur.ROLES_REFLEXION`
   (modèle et température « réflexion »).

## 12.8 Ajouter un sous-agent

Dans `profils.SOUS_AGENTS` :

```python
SousAgent("documentaliste",
          "rédige ou met à jour une documentation précise à partir du code",   # vu par l'agent principal
          "Tu es un rédacteur technique. …",                                   # prompt du sous-agent
          ecrit=True),
```

puis définis ses outils dans `SousAgent.outils()` (par défaut : lecture + git). Il devient disponible dans
l'outil `Deleguer` de l'agent principal (le schéma `enum` est mis à jour automatiquement).

## 12.9 Ajouter une commande `/`

Dans `repl.py` :

1. ajoute l'entrée dans `COMMANDES` (aide et complétion) ;
2. ajoute une méthode `cmd_<nom>(self, arg: str)` à `Repl` — elle est découverte automatiquement (`/ma-commande`
   → `cmd_ma_commande`). Un alias s'écrit `cmd_alias = cmd_nom`.

## 12.10 Ajouter une variable de configuration

1. Champ dans la dataclass `Config` (`config.py`) et lecture dans `charger_config()` avec `_bool`, `_int`
   (avec bornes) ou `_float`.
2. Documentation commentée dans **`agent_code/defaut.env` et `.env.example`** (le test
   `test_env_example_synchronise` vérifie qu'ils restent identiques).
3. Si utile, une option CLI dans `parser_arguments()` qui positionne la variable dans `main()`.
4. Mets à jour [Configuration](06-configuration.md).

## 12.11 Modifier la politique de sécurité

Tout est dans `securite.py`, sous forme de constantes lisibles :

| Constante | Contenu |
|---|---|
| `MOTIFS_SENSIBLES`, `MOTIFS_SENSIBLES_AUTORISES`, `DOSSIERS_SENSIBLES` | Fichiers illisibles |
| `ZONES_PROTEGEES_ECRITURE` | Zones non modifiables |
| `DOSSIERS_IGNORES` | Dossiers non parcourus |
| `EXECUTABLES_INTERDITS`, `PREFIXES_INTERDITS` | Commandes bloquées |
| `COMMANDES_SURES` | Commandes AUTO |
| `ARGS_DANGEREUX`, `PREFIXES_ARGS_DANGEREUX`, `ARGS_GIT_BRANCH_MUTANTS` | Arguments qui rendent une commande sensible |
| `SOUS_COMMANDES_GIT_INTERDITES`, `OPTIONS_CURL_ENVOI` | Règles git et réseau |
| `_MOTIFS_SECRETS`, `_MOTIF_CLE_VALEUR`, `_MOTIF_URL` | Masquage des secrets |
| `_VARS_RETIREES` + motif de `env_nettoye()` | Environnement des commandes |

Règle : **ajouter un test** dans `tests/test_securite.py` pour chaque règle ajoutée ou assouplie, en
particulier les tentatives de contournement.

## 12.12 Prendre en charge un autre modèle

- Tout modèle Ollama ayant la capacité `tools` fonctionne via `/api/chat` ; les appels écrits en texte sont
  rattrapés par `extraire_appels_texte()`.
- Le **mode brut** n'est activé que pour `qwen3-coder` (`utilise_gabarit_qwen3coder()`). Pour une autre
  famille dont l'intégration native serait fragile, on peut écrire une fonction de rendu équivalente à
  `rendre_qwen3coder()` (format d'entraînement du modèle) et l'analyse correspondante.
- Les paramètres d'inférence sont dans `ReglagesAgent.options()` (actuellement les recommandations Qwen).

## 12.13 Checklist avant de proposer une modification

- [ ] `make test` passe (sans Ollama)
- [ ] `make lint` ne signale rien
- [ ] Nouveaux comportements de sécurité couverts par des tests (y compris contournements)
- [ ] `defaut.env` et `.env.example` identiques si la configuration change
- [ ] Documentation (`docs/`, `README.md`) mise à jour
- [ ] Testé en vrai sur un petit projet : `python main.py ~/projet-test`
- [ ] Aucun secret, aucun chemin personnel dans le code ou les exemples

---

[← Technologies](11-technologies.md) · [Sommaire](README.md) · [Dépannage et FAQ →](13-depannage-faq.md)
