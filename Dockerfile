# Image d'agent_code (le serveur Ollama tourne à côté : voir docker-compose.yml)
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Même UID que ton utilisateur hôte : les fichiers écrits dans /workspace t'appartiennent
# et git n'y voit pas de « dubious ownership ».  AGENT_CODE_UID=$(id -u) docker compose build
ARG UID=1000
RUN useradd --create-home --uid "${UID}" agent_code

WORKDIR /opt/agent_code
COPY pyproject.toml README.md ./
COPY agent_code ./agent_code
RUN pip install --no-cache-dir '.[outils]'

USER agent_code
RUN git config --global --add safe.directory /workspace \
 && mkdir -p /home/agent_code/.local/share

ENV PYTHONUNBUFFERED=1 \
    AGENT_CODE_OLLAMA_URL=http://ollama:11434 \
    AGENT_CODE_OLLAMA_DE_CONFIANCE=true

WORKDIR /workspace
ENTRYPOINT ["agent_code"]
CMD ["--help"]
