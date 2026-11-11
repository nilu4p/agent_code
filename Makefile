# Raccourcis :  make installer | make dev | make test | make lint | make diagnostic | make docker
PYTHON ?= python3.12
VENV   ?= .venv

.PHONY: aide installer dev test lint diagnostic modeles docker
aide:
	@grep -E "^[a-z]+:.*##" $(MAKEFILE_LIST) | sed -E "s/:.*## /\t/"

installer: ## Installe la commande `agent_code` pour ton utilisateur (pipx, sinon uv)
	@if command -v pipx >/dev/null; then pipx install --force ".[outils]"; \
	elif command -v uv >/dev/null; then uv tool install --force ".[outils]"; \
	else echo "❌ installe pipx (sudo apt install pipx) ou uv"; exit 1; fi
	@agent_code --init-config

dev: ## Crée le venv de développement (.venv) avec les dépendances
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -U pip
	$(VENV)/bin/pip install -e ".[outils]"

test: ## Lance les tests (sécurité, outils, config, CLI)
	$(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3) -m unittest discover tests -v

lint: ## Analyse statique
	ruff check agent_code tests main.py

diagnostic: ## Vérifie Ollama, modèles, fenêtre de contexte, dépendances
	$(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3) main.py --diagnostic .

modeles: ## Télécharge le modèle (qwen3-coder:30b) et met la config à jour
	./setup_modeles.sh

docker: ## Construit l image et démarre Ollama en conteneur
	AGENT_CODE_UID=$$(id -u) docker compose --profile outil build agent_code
	docker compose up -d ollama
