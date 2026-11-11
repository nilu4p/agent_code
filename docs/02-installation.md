# 2. Installation

[← Présentation](01-presentation.md) · [Sommaire](README.md) · [Guide d'utilisation →](03-guide-utilisateur.md)

## Table des matières

- [2.1 Prérequis](#21-prérequis)
- [2.2 Matériel recommandé](#22-matériel-recommandé)
- [2.3 Installer Ollama](#23-installer-ollama)
- [2.4 Installer agent_code](#24-installer-agent_code)
  - [Méthode A — commande globale (recommandée)](#méthode-a--commande-globale-recommandée)
  - [Méthode B — environnement de développement](#méthode-b--environnement-de-développement)
  - [Méthode C — sans installation](#méthode-c--sans-installation)
  - [Méthode D — Docker](#méthode-d--docker)
- [2.5 Télécharger le modèle](#25-télécharger-le-modèle)
- [2.6 Créer la configuration](#26-créer-la-configuration)
- [2.7 Vérifier l'installation](#27-vérifier-linstallation)
- [2.8 Premier lancement](#28-premier-lancement)
- [2.9 Mettre à jour / désinstaller](#29-mettre-à-jour--désinstaller)

---

## 2.1 Prérequis

| Élément | Version | Pourquoi |
|---|---|---|
| **Linux** ou **macOS** (Windows via WSL) | — | Fonctions POSIX : groupes de processus, verrou `flock`, délai `SIGALRM` |
| **Python** | ≥ 3.11 (3.12 recommandé) | `tomllib`, `Path.is_relative_to` ; vérifié au démarrage |
| **Ollama** | récent | Fait tourner le modèle d'IA localement |
| **git** | toute version récente | Checkpoints, annulation des missions, `/commit`, mode `--branche` |
| **pipx** ou **uv** | — | Pour installer la commande `agent_code` (méthode A) |

## 2.2 Matériel recommandé

Le modèle par défaut, `qwen3-coder:30b`, est de type MoE : il tourne **sans carte graphique**.

| Configuration | Modèle conseillé | Remarques |
|---|---|---|
| 32 Go de RAM ou plus | `qwen3-coder:30b` (défaut) | ≈ 22 Go utilisés (18,6 Go de modèle + 3,2 Go pour 32k tokens de contexte) |
| 24 Go de RAM | `qwen3-coder:30b` avec `AGENT_CODE_CONTEXTE=16384` | Moins de mémoire de travail, résumés plus fréquents |
| 16 Go de RAM | `gpt-oss:20b` (≈ 14 Go) | Un peu moins bon en code |
| GPU NVIDIA | n'importe lequel des précédents | Ollama l'utilise automatiquement ; nettement plus rapide |

Mesures de référence (Ryzen AI 9 365, 32 Go, sans GPU) : ≈ 16 tokens/s en génération ; ≈ 100 tokens/s en
lecture du prompt la première fois, quasi instantané ensuite grâce au cache d'Ollama.

Prévois aussi **≈ 20 Go d'espace disque** pour le modèle.

## 2.3 Installer Ollama

Suis les instructions officielles : <https://ollama.com/download>. Sous Linux :

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama serve              # ou : systemctl start ollama (si installé comme service)
ollama list               # doit répondre (liste éventuellement vide)
```

## 2.4 Installer agent_code

Récupère d'abord le code :

```bash
git clone <url-de-ton-dépôt> agent_code
cd agent_code
```

### Méthode A — commande globale (recommandée)

```bash
make installer
```

Ce que fait cette cible (voir `Makefile`) :

1. `pipx install --force ".[outils]"` (ou `uv tool install --force ".[outils]"` si pipx est absent) : installe
   la commande `agent_code` dans `~/.local/bin`, dans un environnement isolé, avec les outils optionnels
   (ruff, pyyaml, pytest) ;
2. `agent_code --init-config` : crée `~/.config/agent_code/agent_code.env` à partir du modèle commenté
   (permissions `600`, car il peut contenir une clé d'API).

> Si `agent_code: command not found` : lance `pipx ensurepath` puis rouvre ton terminal.

### Méthode B — environnement de développement

```bash
make dev                      # crée .venv + pip install -e '.[outils]'
source .venv/bin/activate
agent_code --version
```

Installation « éditable » : tes modifications du code sont prises en compte immédiatement.

### Méthode C — sans installation

```bash
pip install -r requirements.txt
python main.py --diagnostic .
python main.py ~/projets/mon_app
```

`main.py` est un simple lanceur équivalent à la commande `agent_code`.

### Méthode D — Docker

Pour un serveur (ou un poste sans Ollama installé), le dépôt fournit un `Dockerfile` et un `docker-compose.yml`
qui démarrent Ollama **et** agent_code :

```bash
make docker                   # construit l'image avec TON UID et démarre Ollama
PROJET=~/projets/mon_app docker compose run --rm agent_code              # session interactive
PROJET=~/projets/mon_app docker compose run --rm agent_code -m revue -t "tout le projet"
```

Détails (volumes, GPU, sécurité du conteneur) : [Automatisation › Docker](05-automatisation.md#57-docker).

## 2.5 Télécharger le modèle

Trois façons, au choix :

```bash
# 1. Laisser agent_code le proposer au premier lancement (barre de progression)
agent_code

# 2. Avec le script fourni (met aussi la configuration à jour)
./setup_modeles.sh                  # qwen3-coder:30b
./setup_modeles.sh gpt-oss:20b      # alternative plus légère

# 3. Directement avec Ollama
ollama pull qwen3-coder:30b
```

`setup_modeles.sh` télécharge le modèle, puis écrit `AGENT_CODE_MODELE=<modèle>` dans le premier fichier de
configuration trouvé (`$AGENT_CODE_CONFIG`, sinon `.env` du dépôt, sinon `~/.config/agent_code/agent_code.env`).

> Le modèle choisi **doit gérer les appels d'outils** (capacité `tools` dans Ollama). agent_code le vérifie
> et refuse de démarrer sinon.

## 2.6 Créer la configuration

```bash
agent_code --init-config            # crée ~/.config/agent_code/agent_code.env (ne remplace jamais un fichier existant)
```

Le fichier est commenté ligne par ligne. Les réglages les plus utiles :

```ini
AGENT_CODE_MODELE=qwen3-coder:30b
AGENT_CODE_CONTEXTE=32768        # 16384 si la RAM manque
AGENT_CODE_MODE=normal           # normal | auto | lecture
AGENT_CODE_WEB=true
```

La liste complète est dans [Configuration](06-configuration.md).

## 2.7 Vérifier l'installation

```bash
agent_code --diagnostic .
```

Exemple de sortie :

```
🩺 DIAGNOSTIC D'AGENT_CODE
  ✅ python            3.12.3 (/usr/bin/python3)
  ✅ configuration     /home/moi/.config/agent_code/agent_code.env
  ✅ git               git version 2.43.0
  ✅ rich              14.3.4 (affichage)
  ✅ prompt_toolkit    3.0.53 (saisie confortable)
  ...
  ✅ ollama            v0.x.y sur http://localhost:11434
  ✅ modèle code       qwen3-coder:30b (18.6 Go + contexte 32768 tokens ≈ 3.2 Go)
  ✅ disque            120.4 Go libres sur /home/moi/projets/app
  ✅ projet            /home/moi/projets/app (dépôt git)

✅ 0 erreur(s), 0 alerte(s).
```

Ce que vérifie le diagnostic :

| Vérification | Niveau en cas de problème |
|---|---|
| Version de Python ≥ 3.11 | ❌ erreur |
| Fichier de configuration trouvé | ⚠️ alerte (valeurs par défaut utilisées) |
| git installé | ❌ erreur |
| `rich` installé | ❌ erreur |
| `prompt_toolkit`, `httpx`, `ddgs`, `ruff`, `pyyaml`, `pytest` | ⚠️ alerte (fonction dégradée) |
| Ollama joignable (et clé d'API valide) | ❌ erreur |
| Ollama local, ou marqué « de confiance » | ⚠️ alerte de confidentialité |
| Modèle présent et capable d'appeler des outils | ❌ erreur |
| RAM disponible suffisante (modèle + cache de contexte calculé) | ⚠️ alerte |
| Deux modèles différents configurés | ⚠️ alerte (Ollama devra les alterner : lent) |
| Espace disque > 2 Go | ⚠️ alerte |
| Projet sous git | ⚠️ alerte |
| Approbations automatiques actives | ⚠️ alerte (rappel) |

Code de sortie : `0` sans erreur, `1` s'il y a au moins une erreur.

## 2.8 Premier lancement

```bash
cd ~/projets/mon_app
agent_code
```

Au démarrage, agent_code :

1. affiche une bannière (projet, branche git, modèle, taille de la mémoire) ;
2. vérifie qu'Ollama répond et que le modèle est installé — **propose de le télécharger** sinon ;
3. vérifie que le modèle sait appeler des outils ;
4. t'avertit si Ollama n'est pas local ;
5. charge `AGENT_CODE.md` (ou `AGENTS.md`, `CLAUDE.md`) s'il existe — sinon il te suggère `/init` ;
6. **préchauffe** le modèle en arrière-plan pendant que tu tapes ta première demande.

💡 Premier réflexe conseillé sur un nouveau projet : tape `/init`. L'agent analyse le projet et écrit
`AGENT_CODE.md` (stack, commandes de test, conventions), relu automatiquement à chaque session.

## 2.9 Mettre à jour / désinstaller

```bash
# Mettre à jour (après git pull)
make installer                     # réinstalle la commande (--force)

# Désinstaller
pipx uninstall agent_code          # ou : uv tool uninstall agent_code
rm -rf ~/.config/agent_code ~/.local/share/agent_code    # configuration et historique de saisie
# Dans chaque projet utilisé : supprimer .agent_code/ (sessions, rapports, audit, notes)
```

---

[← Présentation](01-presentation.md) · [Sommaire](README.md) · [Guide d'utilisation →](03-guide-utilisateur.md)
