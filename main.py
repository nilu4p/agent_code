#!/usr/bin/env python3
"""Lanceur sans installation : `python main.py ...` équivaut à la commande `agent_code ...`.
(Installation recommandée : `pipx install .` ou `uv tool install .`, voir README.)"""

from agent_code.cli import point_entree

if __name__ == "__main__":
    point_entree()
