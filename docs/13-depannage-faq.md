# 13. Dépannage et FAQ

[← Développement](12-developpement.md) · [Sommaire](README.md)

## Table des matières

- [13.1 Le réflexe : le diagnostic](#131-le-réflexe--le-diagnostic)
- [13.2 Installation et démarrage](#132-installation-et-démarrage)
- [13.3 Performances](#133-performances)
- [13.4 Comportement de l'agent](#134-comportement-de-lagent)
- [13.5 Refus et validations](#135-refus-et-validations)
- [13.6 Missions et automatisation](#136-missions-et-automatisation)
- [13.7 Docker](#137-docker)
- [13.8 Où trouver l'information ?](#138-où-trouver-linformation-)
- [13.9 Questions fréquentes](#139-questions-fréquentes)

---

## 13.1 Le réflexe : le diagnostic

```bash
agent_code --diagnostic .
```

Il vérifie Python, la configuration chargée, git, les dépendances, Ollama, le modèle et ses capacités, la
mémoire disponible, l'espace disque et le dépôt git du projet. Chaque ligne ❌ ou ⚠️ indique la correction.
Voir [Installation › Vérifier](02-installation.md#27-vérifier-linstallation).

## 13.2 Installation et démarrage

| Symptôme | Cause probable | Solution |
|---|---|---|
| `agent_code: command not found` | `~/.local/bin` absent du `PATH` | `pipx ensurepath`, puis rouvre le terminal |
| `❌ Python 3.11 ou plus récent est requis` | Python trop ancien | Installe Python 3.12 et réinstalle (`PYTHON=python3.12 make dev`) |
| `Ollama injoignable sur http://localhost:11434` | Ollama arrêté | `ollama serve` ou `systemctl start ollama` |
| `Accès refusé par … (HTTP 401/403)` | Clé du reverse proxy absente ou fausse | `AGENT_CODE_OLLAMA_API_KEY` |
| `Modèle introuvable sur le serveur Ollama` | Modèle non téléchargé | Accepte le téléchargement proposé, ou `ollama pull qwen3-coder:30b` |
| `… ne gère pas les appels d'outils` | Modèle sans capacité `tools` | `AGENT_CODE_MODELE=qwen3-coder:30b` (ou un autre modèle « tools ») |
| `⚠️ Ollama n'est pas local : ton code sera envoyé à …` | URL distante | Normal si voulu ; `AGENT_CODE_OLLAMA_DE_CONFIANCE=true` si la machine est à toi |
| `Refus : le bac à sable ne peut pas être / ou ton $HOME entier` | Lancé depuis `~` | `cd` dans le dossier d'un projet |
| `Pas de terminal : utilise -p "demande" ou -m <mission> -t "demande"` | Lancé dans un script sans option | Voir [Automatisation](05-automatisation.md) |
| Configuration ignorée | Un autre fichier est chargé en premier, ou la variable est déjà dans l'environnement | `--diagnostic` affiche le fichier chargé ; `env \| grep AGENT_CODE` |
| `AGENT_CODE_… invalide, valeur par défaut utilisée` | Valeur mal formée | Corrige la valeur (nombre attendu, mode `normal`/`auto`/`lecture`…) |
| Affichage sans couleurs ni panneaux | `rich` absent | `pip install rich` (ou `make installer`) |
| Pas de complétion ni de ⇧Tab | `prompt_toolkit` absent | `pip install prompt_toolkit` |

## 13.3 Performances

| Symptôme | Solution |
|---|---|
| La 1re réponse met ≈ 30 s | Normal sur CPU : lecture initiale du prompt. Les suivantes réutilisent le cache. Le préchauffage démarre dès l'ouverture : tape ta demande tranquillement |
| Tout est très lent, la machine rame (swap) | `ollama ps` : un seul modèle doit être chargé (`ollama stop <autre>`). Ferme les applications gourmandes. Réduis `AGENT_CODE_CONTEXTE=16384` |
| `⚠️ mémoire : ≈ 22 Go nécessaires, 15 Go disponibles` | Même remède ; ou modèle plus léger (`./setup_modeles.sh gpt-oss:20b`) |
| Deux modèles configurés | Garde un seul `AGENT_CODE_MODELE` (Ollama alternerait les deux en mémoire) |
| Le modèle se décharge entre deux demandes | Augmente `AGENT_CODE_GARDER_MODELE` (ex. `2h`) |
| Les réponses ralentissent au fil de la conversation | Mémoire de travail haute : `/compacter`, ou `/effacer` si tu changes de sujet |
| Tâche simple > 5 min | Formule plus précisément (fichiers avec `@`, erreur complète) ; vérifie qu'aucun autre programme n'utilise le CPU |

Ordres de grandeur (Ryzen AI 9 365, 32 Go, sans GPU) : génération ≈ 16 tokens/s ; tâche simple 1,5 à 2,5 min.

## 13.4 Comportement de l'agent

| Symptôme | Solution |
|---|---|
| L'agent tourne en rond | Ctrl+C, puis précise ta demande ou donne la piste ; `/compacter` si la mémoire est haute. L'anti-boucle bloque déjà les appels identiques répétés |
| Il annonce une action mais s'arrête | Il est relancé automatiquement (2 fois max). Sinon réponds « fais-le » |
| Il montre du code au lieu de modifier | Relancé automatiquement si ta demande est une action (« corrige », « ajoute »…). Sinon : « applique-le » |
| Il te demande de lancer les tests | Relancé automatiquement ; sinon : « lance-les toi-même » |
| `Limite d'étapes atteinte` | Il te fait un point d'étape ; réponds « continue », ou augmente `AGENT_CODE_MAX_ETAPES` |
| `Modifier_Fichier` échoue (« ancien_texte introuvable ») | Il reçoit le contenu actuel et réessaie seul ; s'il échoue encore, demande-lui de relire le fichier |
| Une modification n'est pas bonne | Au moment du diff : tape une consigne au lieu de « o ». Après coup : `/annuler` |
| Il ignore une convention du projet | Écris-la dans `AGENT_CODE.md` (ou `/memo`) ; elle sera relue à chaque session |
| Réponses en anglais | Rare ; rappelle « réponds en français ». Les règles l'imposent déjà |
| Il dit qu'un fichier est « sensible » | C'est voulu (`.env`, clés, `*secret*`…). Utilise `.env.example` pour les exemples |
| `🗜️ Mémoire de travail presque pleine : résumé…` | Normal au-delà de 70 % : la conversation continue avec un résumé |

## 13.5 Refus et validations

| Message | Signification | Que faire |
|---|---|---|
| `⛔ Commande bloquée par la politique de sécurité : caractères shell interdits` | Pipe, redirection, `;`, `$`… | Une seule commande simple ; pour un pipe, lance-la toi-même avec `!` |
| `⛔ … exécutable interdit : sudo` (ou `rm` récursif, `git push`…) | Commande jamais autorisée aux agents | Fais-la toi-même si nécessaire |
| `⛔ argument hors du bac à sable` | Chemin hors du projet | Normal ; travaille dans le projet |
| `⛔ Mode lecture seule` | Mode 📖 ou `--lecture-seule` | ⇧Tab / `/mode normal` (impossible avec `--lecture-seule`) |
| `⛔ Zone protégée en écriture : .vscode` | `.git`, `.vscode`, `.idea`, `.envrc`, `.venv`, `node_modules`, `.agent_code` | Modifie ces fichiers toi-même |
| `⛔ Le contenu contient un secret masqué` | Le modèle a recopié `***MASQUÉ***` | Il doit modifier seulement les lignes concernées (il le sait) |
| `⛔ Action refusée … (ou session non interactive)` | Pas de terminal, ou tu as refusé | En automatisation : `--autoriser-tests`, `--autoriser-cmd`, `--mode auto` |
| `(refusé : session non interactive, utilise --mode auto)` | Écriture avec `-p` en mode normal | Ajoute `--mode auto` |
| « toujours » n'est pas proposé | Commande sensible (`python -c`, `--fix`…) | Validation à chaque fois, par conception |
| Trop de validations | — | Réponds `t` pour un préfixe, ou passe en mode auto pour les diffs ; liste blanche pour les commandes récurrentes |

## 13.6 Missions et automatisation

| Symptôme | Solution |
|---|---|
| `⛔ Une autre mission tourne déjà sur ce projet` (code 3) | Attends la fin ; le verrou est libéré automatiquement même si le processus meurt |
| `Cette mission nécessite le web` | Retire `--sans-web` / mets `AGENT_CODE_WEB=true` (mission `veille`) |
| Mission arrêtée (code 124) | `--timeout-mission` trop court ; un rapport partiel est quand même écrit |
| Le testeur ne lance pas les tests en cron | Ajoute `--autoriser-tests` |
| Je veux annuler une mission terminée | Bilan « A » (stash) ; après coup : `git stash -u` ou `git diff <référence>` (le SHA est affiché et dans le JSON `reference_git`) |
| Je ne veux pas que ma branche bouge | `--branche` |
| `le mode --branche nécessite git` | Initialise le dépôt (`git init`) |
| Commit « [agent_code] avant mission … » dans mon historique | C'est le checkpoint ; tu peux le garder, ou le fusionner avec `git rebase -i` plus tard |
| L'audit nocturne ne tourne pas | `systemctl --user list-timers` ; `loginctl enable-linger $USER` ; `journalctl --user -u agent_code-revue` |
| Le service audite le mauvais projet | `systemctl --user edit agent_code-revue.service` → `Environment=PROJET=…` |

## 13.7 Docker

| Symptôme | Solution |
|---|---|
| Fichiers créés appartenant à un autre utilisateur | `make docker` (reconstruit avec ton UID via `AGENT_CODE_UID`) |
| `dubious ownership` git | Même cause ; l'image déclare `/workspace` comme `safe.directory` |
| `agent_code` attend Ollama longtemps au 1er lancement | `ollama-init` télécharge le modèle (18,6 Go) ; suis avec `docker compose logs -f ollama-init` |
| Accéder à Ollama depuis l'hôte | Décommente `ports: ["127.0.0.1:11434:11434"]` |
| Utiliser le GPU | Décommente le bloc `deploy` (nécessite `nvidia-container-toolkit`) |

## 13.8 Où trouver l'information ?

| Besoin | Emplacement |
|---|---|
| Ce que l'agent a fait (actions, refus, validations) | `.agent_code/audit.log` |
| Conversations passées | `.agent_code/sessions/` (ou `/reprendre`) |
| Rapports de mission | `.agent_code/rapports/` |
| Notes durables | `.agent_code/notes.md` |
| Mémoire du projet | `AGENT_CODE.md` |
| Configuration effective | `agent_code --diagnostic` |
| Service systemd | `journalctl --user -u agent_code-revue` |

## 13.9 Questions fréquentes

**Mon code est-il envoyé sur Internet ?**
Non. Le modèle tourne dans Ollama sur ta machine. Seules la recherche web (texte de la requête, sans secret)
et la lecture de pages que tu valides sortent sur Internet ; désactive-les avec `--sans-web`. Si tu configures
un Ollama distant, agent_code t'en avertit.

**Faut-il une carte graphique ?**
Non. `qwen3-coder:30b` est un modèle MoE qui tourne correctement sur un processeur récent avec 32 Go de RAM.
Un GPU accélère nettement.

**Est-ce aussi bon que Claude Code ou Copilot ?**
Non, un modèle local de cette taille reste en dessous des meilleurs modèles en ligne. Il est en revanche
privé, gratuit, hors ligne, et suffisant pour beaucoup de tâches courantes (corrections, tests, explications,
revues). agent_code compense une partie des faiblesses par des relances et des vérifications.

**Puis-je utiliser un autre modèle ?**
Oui, tout modèle Ollama avec la capacité `tools` : `--modele`, `/modele` ou `AGENT_CODE_MODELE`. Le mode le
plus fiable reste qwen3-coder (format d'entraînement géré nativement).

**L'agent peut-il casser mon projet ?**
En mode normal, rien n'est modifié sans ton accord ; `/annuler` restaure les fichiers ; les missions font un
checkpoint git. Les commandes destructrices sont bloquées. Les commandes que tu valides (tests, build)
exécutent toutefois le code du projet avec tes droits.

**Peut-il lire mes mots de passe ?**
Les fichiers sensibles (`.env`, clés, certificats, `.ssh/`…) lui sont inaccessibles, et les secrets qu'il
croise ailleurs sont masqués. Le masquage est heuristique : garde tes secrets dans des fichiers `.env`.

**Peut-il pousser sur GitHub ?**
Non : `git push`, `git remote`, `git config` sont interdits aux agents, et l'environnement des commandes ne
contient pas l'accès à ton agent SSH ni tes jetons.

**Pourquoi un commit « [agent_code] avant mission » ?**
C'est le checkpoint qui permet d'annuler une mission. Avec `--branche`, ta branche reste intacte.

**Fonctionne-t-il sous Windows ?**
Pas en natif ; utilise WSL2 ou Docker.

**Puis-je l'utiliser dans plusieurs projets ?**
Oui : chaque projet a son propre `.agent_code/` (sessions, notes, rapports, audit) et son `AGENT_CODE.md`.

**AGENTS.md / CLAUDE.md sont-ils pris en compte ?**
Oui, si `AGENT_CODE.md` n'existe pas (dans cet ordre).

**Comment repartir de zéro dans un projet ?**
`/effacer` pour la conversation ; supprimer `.agent_code/` pour tout l'historique local (sessions, notes,
rapports, audit).

---

[← Développement](12-developpement.md) · [Sommaire](README.md)
