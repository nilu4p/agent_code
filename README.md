# ✻ agent_code (v1) — ton assistant de code 100 % local

Un agent de programmation **façon Claude Code**, qui tourne entièrement sur ta machine avec Ollama et
`qwen3-coder:30b`. Tu lui parles en français, il explore ton projet, modifie les fichiers (tu valides chaque
diff), lance les tests et vérifie son travail. Ton code ne quitte jamais ton poste.

> 📚 **Documentation complète : [docs/](docs/README.md)** — [présentation](docs/01-presentation.md) ·
> [installation](docs/02-installation.md) · [guide d'utilisation](docs/03-guide-utilisateur.md) ·
> [missions](docs/04-missions-et-sous-agents.md) · [automatisation](docs/05-automatisation.md) ·
> [configuration](docs/06-configuration.md) · [architecture](docs/07-architecture.md) ·
> [moteur](docs/08-moteur-agentique.md) · [outils](docs/09-outils.md) · [sécurité](docs/10-securite.md) ·
> [technologies](docs/11-technologies.md) · [développement](docs/12-developpement.md) ·
> [dépannage](docs/13-depannage-faq.md)

```
› les tests de calc.py échouent, corrige
● Lancer_Tests()
  ⎿ CODE RETOUR: 1  (+23 lignes)
● Lire_Fichier(calc.py)
  ⎿ 📄 calc.py (lignes 1-9 / 9)  (+9 lignes)
● Modifier_Fichier(calc.py)
╭─ ✏️  Modifier calc.py  +5 −2 ─────────────────────────╮
│ -    return sum(valeurs) / len(valeurs) - 1          │
│ +    return sum(valeurs) / len(valeurs)              │
╰──────────────────────────────────────────────────────╯
Appliquer ? oui · non · toujours · ou tape une consigne › o
● Lancer_Tests()
  ⎿ CODE RETOUR: 0  (+7 lignes)
J'ai corrigé calc.py:3 (le « - 1 » faussait la moyenne) et calc.py:9 (médiane des listes paires). Les 2 tests passent.
· 95s · 5 étape(s) · 412 tokens · 16 tok/s · mémoire 14%
📝 1 fichier(s) modifié(s) : calc.py · /diff pour voir · /annuler pour revenir en arrière
```

## Démarrage rapide

```bash
cd ~/Bureau/mes_projets_a_cote/agent_code
make installer            # installe la commande `agent_code` (pipx ou uv) + crée ~/.config/agent_code/agent_code.env
cd ~/projets/mon_app
agent_code                    # c'est tout : au 1er lancement, il propose de télécharger le modèle (18,6 Go)
```

Prérequis : [Ollama](https://ollama.com/download) démarré, Python ≥ 3.11, git. Vérifie tout avec `agent_code --diagnostic .`

## Au quotidien

Lance `agent_code` dans ton projet et décris ce que tu veux, en langage naturel :

- « explique-moi comment marche l'authentification »
- « ajoute un endpoint /health avec un test »
- « ce traceback : … corrige » (colle-le directement)
- « relis mes changements avant que je committe »

| Raccourci | Effet |
|---|---|
| `@chemin` | joint un fichier ou un dossier à ta demande (**Tab** pour compléter) |
| `!commande` | lance une commande toi-même ; sa sortie est transmise à l'agent |
| **⇧Tab** | change de mode : 🛡️ normal → ⚡ auto → 📖 lecture |
| **Ctrl+C** | interrompt l'agent (la conversation continue) · deux fois pour quitter |
| **Échap+Entrée** | nouvelle ligne dans ta demande |
| `agent_code -c` | reprend la dernière conversation du projet |

**Modes**
- 🛡️ **normal** (défaut) : chaque modification s'affiche en diff coloré et attend ton accord. Réponds `o`, `n`,
  `t` (toujours : passe en auto) ou **tape directement une consigne** (« garde l'ancien nom ») : l'agent en tient compte.
- ⚡ **auto** : les modifications s'appliquent seules ; `/annuler` revient en arrière. Les commandes restent validées.
- 📖 **lecture** : l'agent ne peut rien modifier — idéal pour explorer ou faire préparer un plan.

**Commandes**

| Commande | Effet |
|---|---|
| `/aide` | l'aide |
| `/annuler` | défait les modifications de fichiers du dernier tour (même sans git) |
| `/diff` | tout ce que la session a modifié |
| `/commit` | l'agent propose un message de commit, tu valides (tes hooks git s'exécutent) |
| `/init` | analyse le projet et écrit `AGENT_CODE.md` : la mémoire du projet, relue à chaque session |
| `/memo <note>` | note durable sur le projet (`.agent_code/notes.md`) |
| `/mode`, `/modele` | change de mode ou de modèle |
| `/contexte`, `/compacter` | occupation de la mémoire du modèle ; résumé de la conversation pour la libérer |
| `/effacer`, `/reprendre` | nouvelle conversation ; reprendre une précédente |
| `/missions`, `/mission <nom> <demande>` | équipes multi-agents (voir plus bas) |

💡 **Commence par `/init`** sur un nouveau projet : `AGENT_CODE.md` (stack, commandes de test, conventions) rend
l'agent nettement plus pertinent. Tu peux l'éditer à la main ; `AGENTS.md` et `CLAUDE.md` sont aussi reconnus.

## Sous-agents et missions

L'agent principal peut **déléguer** une tâche à un sous-agent qui travaille dans son propre contexte et ne lui
renvoie que sa conclusion — indispensable avec une mémoire de travail limitée :

| Sous-agent | Rôle |
|---|---|
| `explorateur` | répond à une question précise sur le code (lecture seule) |
| `relecteur` | relit des changements et classe les problèmes 🔴/🟠/🟢 |
| `testeur` | écrit des tests ciblés et les lance |
| `chercheur` | cherche sur le web (si `AGENT_CODE_WEB=true`) |

Les **missions** enchaînent plusieurs agents spécialisés, chacun recevant le travail du précédent, et produisent
un rapport dans `.agent_code/rapports/` : `dev` (architecte → développeur → QA → relecteur), `debug`, `revue`
(qualité + sécurité OWASP), `tests`, `doc`, `explique`, `question`, `projet`, `veille`, `devops`.
En session : `/mission revue le module api/`. En ligne de commande : `agent_code . -m revue -t "tout le projet"`.

## Scripts, cron, serveur

```bash
agent_code -p "explique le module auth"                      # une demande, une réponse (lecture seule conseillée : --mode lecture)
agent_code -p "corrige le lint" --mode auto --autoriser-cmd "ruff check"
agent_code . -m dev -T demande.md --branche --autoriser-tests --timeout-mission 3600 --json
```

Sans terminal, **toute action qui demande une validation est refusée**, sauf ce que tu autorises explicitement :
`--mode auto` (modifications), `--autoriser-tests`, `--autoriser-cmd "préfixe exact"` (jamais pour `python -c`,
`--fix`, ni une commande bloquée comme `sudo` ou `git push`). `--branche` isole le travail d'une mission dans
`agent_code/<mission>-<date>` sans toucher à ta branche. `--json` écrit un résumé en dernière ligne. Un verrou empêche
deux missions simultanées sur un même projet.

| Code de sortie | 0 ok · 1 échec · 2 usage/config · 3 projet occupé · 124 délai dépassé · 130 interrompu |
|---|---|

- **Docker** (serveur sans Ollama installé) : `make docker`, puis
  `PROJET=~/projets/app docker compose run --rm agent_code` (session) ou `… agent_code -m revue -t "tout"`.
  Ollama n'est pas exposé sur le réseau ; agent_code tourne sans privilèges, avec ton UID. GPU : bloc `deploy` à décommenter.
- **Audit nocturne** avec systemd : `deploy/systemd/agent_code-revue.{service,timer}` (instructions dans le fichier).
- **Ollama distant** derrière un reverse proxy : `AGENT_CODE_OLLAMA_URL` + `AGENT_CODE_OLLAMA_API_KEY` (et
  `AGENT_CODE_OLLAMA_DE_CONFIANCE=true` si la machine est à toi).

## Modèle et performances

`qwen3-coder:30b` est un modèle « MoE » : 30 milliards de paramètres, mais seuls ~3 milliards travaillent par
mot généré. Il tourne donc correctement **sans carte graphique**. Mesures sur un Ryzen AI 9 365 avec 32 Go RAM :

| | |
|---|---|
| Mémoire | ≈ 22 Go (modèle 18,6 Go + 3,2 Go pour 32k tokens de contexte) |
| Génération | ≈ 16 tokens/s |
| Lecture du prompt | ≈ 100 tokens/s la 1re fois, quasi instantané ensuite (cache d'Ollama) |
| Tâche simple (lire, corriger, tester) | 1,5 à 2,5 min |

Pour que ça reste fluide :
- La session **préchauffe** le modèle pendant que tu tapes ta première demande.
- Ferme les applications gourmandes ; `agent_code --diagnostic` t'alerte si la RAM manque. Sinon : `AGENT_CODE_CONTEXTE=16384`.
- N'aie qu'**un seul** modèle chargé dans Ollama (`ollama ps` ; `ollama stop <modèle>` pour libérer la mémoire).
- Machine plus modeste : `./setup_modeles.sh gpt-oss:20b` (≈ 14 Go), un peu moins bon en code.

Côté fiabilité, agent_code compense les faiblesses connues des modèles locaux :
- elle construit elle-même le prompt au format d'entraînement de qwen3-coder (le streaming d'Ollama 0.40 laisse fuiter des appels d'outils) ;
- elle relance le modèle quand il annonce une action sans la faire, ou montre du code au lieu de l'appliquer ;
- elle reconnaît les appels d'outils écrits en texte ;
- elle bloque les appels identiques en boucle ;
- elle résume automatiquement la conversation quand la mémoire de travail dépasse 70 %.

## Sécurité

| Risque | Protection |
|---|---|
| Modifications non voulues | diff à valider (mode normal), `/annuler` par tour, checkpoint git + bilan Garder/Voir/Annuler pour les missions, mode `--branche` |
| Sortie du dossier (`../`, chemins absolus, liens symboliques) | chemins résolus et vérifiés, y compris dans les arguments des commandes ; parcours sans suivre les liens |
| Injection de commande | pas de shell : commande découpée par `shlex`, `; \| & > $ \`` refusés |
| Commandes destructrices | refusées d'office : `sudo`, `rm -rf`, `dd`, `git push/clean/reset --hard`, `curl -d`, `bash -c`, lanceurs (`env`, `xargs`, `timeout`…) |
| Fatigue de validation | `ls`, `git status`, `grep`… passent seuls sauf s'ils écrivent ; « toujours » mémorise un préfixe pour la session ; listes blanches exactes |
| Fuite de secrets | `.env`, clés, `*.pem`, `.ssh/`… illisibles par les agents, exclus des recherches, des `@mentions` et des commits ; secrets masqués dans les sorties, les diffs et le journal ; variables `*TOKEN*`, `*KEY*`… retirées de l'environnement des commandes |
| Injection de prompt (web, fichiers, notes) | contenus externes présentés comme des données ; protection SSRF ; pages web validées |
| Exécution de code à l'insu | `.git/`, `.vscode/`, `.idea/`, `.envrc`, `.venv/`, `node_modules/` non modifiables ; git d'agent_code sans hooks |
| Processus fous, missions sans fin | groupe de processus tué au délai, sorties bornées, `--timeout-mission`, limite d'étapes par demande |
| Traçabilité | `.agent_code/audit.log` (JSON lines, rotation), conversations dans `.agent_code/sessions/` |
| Confidentialité | aucune télémétrie, aucun proxy, alerte si Ollama n'est pas local |

## Configuration

Fichier cherché dans cet ordre : `$AGENT_CODE_CONFIG`, `.env` à côté du code, `~/.config/agent_code/agent_code.env`
(le `.env` du projet cible n'est **jamais** lu). Toutes les options sont commentées dans `.env.example`.
Les plus utiles : `AGENT_CODE_MODELE`, `AGENT_CODE_CONTEXTE`, `AGENT_CODE_MODE`, `AGENT_CODE_WEB`, `AGENT_CODE_MAX_ETAPES`.

## Architecture

```
agent_code/cli.py          ligne de commande : session, -p, -m, --diagnostic, codes de sortie
agent_code/repl.py         session interactive : saisie, /commandes, @mentions, !commandes
agent_code/interface.py    affichage : spinner, appels d'outils, diffs, validations, plan
agent_code/session.py      agent principal + sous-agents, mentions, sauvegarde/reprise
agent_code/moteur.py       boucle agentique, sous-agents, plan, relances, gestion du contexte
agent_code/llm.py          client Ollama (stdlib) : API native ou gabarit qwen3-coder brut, streaming
agent_code/profils.py      prompts : agent principal, sous-agents, rôles des missions
agent_code/outils.py       outils des agents (lecture, recherche, édition, commandes, tests, git, web)
agent_code/journal.py      journal des modifications (/annuler, /diff)
agent_code/securite.py     bac à sable, politique de commandes, secrets, audit, verrou
agent_code/missions.py     catalogue des missions (ajouter une mission = ajouter un Mission(...))
agent_code/orchestrateur.py  missions : étapes, checkpoint git, branche, délai, bilan
agent_code/diagnostic.py   agent_code --diagnostic
```

Le moteur et le client Ollama n'utilisent que la bibliothèque standard ; `rich` et `prompt_toolkit` servent
à l'interface (avec repli si absents). Démarrage instantané, installation légère.

## Développement

```bash
make dev && source .venv/bin/activate     # venv + pip install -e '.[outils]'
make test                                 # 106 tests, sans Ollama (faux serveur et faux modèle)
make lint
```

## Dépannage

| Symptôme | Solution |
|---|---|
| Quelque chose ne va pas | `agent_code --diagnostic .` |
| Très lent, la machine rame | `ollama ps` : un seul modèle chargé ? Sinon `ollama stop <autre>` ; ou `AGENT_CODE_CONTEXTE=16384` |
| La 1re réponse met ~30 s | normal : lecture initiale du prompt sur CPU ; les suivantes réutilisent le cache |
| L'agent tourne en rond | Ctrl+C, puis précise ta demande ; `/compacter` si la mémoire est haute |
| Une modification n'est pas bonne | `/annuler` (ou réponds par une consigne au moment du diff) |
| `agent_code: command not found` | `pipx ensurepath`, puis rouvre le terminal |
| Pas de terminal (cron, CI) | `-p "…"` ou `-m … -t "…"` ; autorise explicitement ce qui doit passer sans validation |
| `Une autre mission tourne déjà` | attends la fin (verrou libéré automatiquement si le processus meurt) |
| Docker : fichiers créés appartenant à un autre utilisateur | `make docker` (reconstruit avec ton UID) |
