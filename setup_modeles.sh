#!/usr/bin/env bash
# Télécharge le modèle d'agent_code et met la configuration à jour.
#
#   ./setup_modeles.sh                         # qwen3-coder:30b (recommandé avec 32 Go de RAM)
#   ./setup_modeles.sh gpt-oss:20b             # plus léger (≈ 14 Go)
#
# Inutile de créer des variantes « grand contexte » : agent_code fixe la fenêtre de contexte (AGENT_CODE_CONTEXTE)
# à chaque requête via l'API native d'Ollama. Note : `agent_code` propose aussi de télécharger le modèle au démarrage.
set -euo pipefail
command -v ollama >/dev/null || { echo "❌ ollama introuvable : https://ollama.com/download" >&2; exit 1; }
ollama list >/dev/null 2>&1 || { echo "❌ Le serveur Ollama ne répond pas : lance « ollama serve » (ou systemctl start ollama)" >&2; exit 1; }

MODELE="${1:-qwen3-coder:30b}"
echo "⬇️  $MODELE"
ollama pull "$MODELE"

# Fichier de config : $AGENT_CODE_CONFIG, sinon .env du dépôt, sinon ~/.config/agent_code/agent_code.env
ICI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${AGENT_CODE_CONFIG:-}"
if [[ -z "$CONF" ]]; then
  if [[ -f "$ICI/.env" ]]; then CONF="$ICI/.env"; else CONF="${XDG_CONFIG_HOME:-$HOME/.config}/agent_code/agent_code.env"; fi
fi
if [[ ! -f "$CONF" ]]; then
  echo "ℹ️  Pas de fichier de configuration : lance « agent_code --init-config », puis mets AGENT_CODE_MODELE=$MODELE"
  exit 0
fi
if grep -qE "^[[:space:]]*AGENT_CODE_MODELE=" "$CONF"; then
  sed -i.bak -E "s|^[[:space:]]*AGENT_CODE_MODELE=.*|AGENT_CODE_MODELE=$MODELE|" "$CONF"
else
  printf 'AGENT_CODE_MODELE=%s\n' "$MODELE" >> "$CONF"
fi
rm -f "$CONF.bak"
echo "✅ $CONF : AGENT_CODE_MODELE=$MODELE. Vérifie avec : agent_code --diagnostic"
