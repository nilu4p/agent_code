"""Catalogue des missions : chaque mission = une équipe d'agents + un enchaînement de tâches."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Etape:
    agent: str
    description: str
    attendu: str
    contexte: tuple[int, ...] = ()   # indices des étapes précédentes à fournir en contexte


@dataclass(frozen=True)
class Mission:
    cle: str
    titre: str
    emoji: str
    resume: str
    lecture_seule: bool              # True → aucun outil d'écriture/exécution de code
    etapes: tuple[Etape, ...] = field(default_factory=tuple)
    invite: str = "Décris ta demande"
    web_requis: bool = False


RAPPORT = (
    "Termine par un rapport Markdown structuré : ## Résumé, ## Détails, ## Fichiers concernés, "
    "## Prochaines étapes recommandées."
)

MISSIONS: dict[str, Mission] = {m.cle: m for m in [
    Mission(
        "dev", "Développer une fonctionnalité", "🛠️",
        "Plan → implémentation → tests → revue de code.",
        lecture_seule=False,
        invite="Quelle fonctionnalité / modification veux-tu ?",
        etapes=(
            Etape("architecte",
                  "Demande : « {demande} ».\nExplore le projet (arborescence, signatures, recherche) et "
                  "produis un plan d'implémentation : fichiers à créer/modifier, ce qui change dans chacun, "
                  "points d'attention (sécurité, compatibilité), et comment vérifier que ça marche.",
                  "Un plan numéroté, fichier par fichier, directement exécutable par un développeur."),
            Etape("developpeur",
                  "Implémente le plan de l'architecte pour la demande « {demande} ». Pour chaque fichier : lis, "
                  "modifie, vérifie la syntaxe. Ne modifie rien hors du plan sans le justifier.",
                  "La liste des fichiers modifiés/créés avec un résumé des changements et le résultat des vérifications.",
                  (0,)),
            Etape("testeur",
                  "Écris ou complète les tests couvrant les changements pour « {demande} », puis lance-les. "
                  "Si un test échoue à cause du code, corrige le code de façon minimale et relance.",
                  "Les tests ajoutés et la sortie exacte de leur exécution (succès/échecs).",
                  (0, 1)),
            Etape("relecteur",
                  "Relis l'ensemble des modifications (Git_Diff) faites pour « {demande} ». Vérifie qu'elles "
                  "respectent le plan, ne cassent rien et n'introduisent pas de faille. " + RAPPORT,
                  "Rapport de revue final classé par gravité, avec verdict : ✅ prêt / ⚠️ à reprendre.",
                  (0, 1, 2)),
        ),
    ),
    Mission(
        "debug", "Corriger un bug", "🐞",
        "Reproduction → cause racine → correctif minimal → test de non-régression.",
        lecture_seule=False,
        invite="Décris le bug (message d'erreur, traceback, comportement attendu vs obtenu) :",
        etapes=(
            Etape("debogueur",
                  "Bug signalé : « {demande} ».\n1) Localise le code concerné (Rechercher_Code, traceback). "
                  "2) Reproduis si possible. 3) Identifie la cause racine. 4) Applique un correctif minimal. "
                  "5) Vérifie (syntaxe + tests).",
                  "Cause racine expliquée, correctif appliqué (fichier:ligne) et preuve que le bug est corrigé."),
            Etape("relecteur",
                  "Relis le correctif (Git_Diff) pour le bug « {demande} » : est-il correct, minimal, sans effet de bord ? "
                  "Propose un test de non-régression s'il manque. " + RAPPORT,
                  "Verdict de revue du correctif.", (0,)),
        ),
    ),
    Mission(
        "revue", "Audit qualité & sécurité", "🔍",
        "Revue de code + audit de sécurité OWASP, en lecture seule. Rapport détaillé.",
        lecture_seule=True,
        invite="Périmètre à auditer (ex : 'tout le projet', 'src/api', 'le diff depuis main') :",
        etapes=(
            Etape("relecteur",
                  "Périmètre : « {demande} ».\nFais une revue qualité : architecture, lisibilité, duplication, "
                  "gestion d'erreurs, tests manquants, performances, dette technique. Lance Analyser_Qualite.",
                  "Liste de constats classés 🔴/🟠/🟢 avec fichier:ligne et correctif proposé."),
            Etape("securite",
                  "Périmètre : « {demande} ». Audite la sécurité du code (OWASP Top 10, secrets, dépendances, "
                  "configurations). Complète la revue qualité précédente. " + RAPPORT,
                  "Rapport consolidé qualité + sécurité avec un plan de remédiation priorisé.", (0,)),
        ),
    ),
    Mission(
        "tests", "Écrire des tests", "🧪",
        "Analyse de la couverture → tests unitaires/intégration → exécution.",
        lecture_seule=False,
        invite="Que faut-il tester ? (module, fonction, ou 'tout le projet')",
        etapes=(
            Etape("architecte",
                  "Cible : « {demande} ».\nIdentifie le code non ou mal testé et liste les cas de test prioritaires "
                  "(nominal, limites, erreurs) ainsi que le framework/structure de tests à utiliser.",
                  "Plan de tests priorisé."),
            Etape("testeur",
                  "Écris les tests du plan pour « {demande} », lance-les, et itère jusqu'à ce qu'ils soient pertinents. "
                  "Ne modifie PAS le code de production : signale les bugs découverts. " + RAPPORT,
                  "Tests créés, résultat d'exécution, bugs découverts.", (0,)),
        ),
    ),
    Mission(
        "doc", "Documenter le projet", "📚",
        "README, guide d'architecture, docstrings.",
        lecture_seule=False,
        invite="Quelle documentation ? (ex : 'README complet', 'docstrings du module x', 'docs/ARCHITECTURE.md')",
        etapes=(
            Etape("architecte",
                  "Demande : « {demande} ».\nAnalyse le projet : objectif, stack, points d'entrée, "
                  "installation, configuration (variables d'env), architecture et flux principaux.",
                  "Synthèse technique factuelle du projet."),
            Etape("documentaliste",
                  "Rédige la documentation demandée (« {demande} ») à partir de l'analyse, et écris-la dans les "
                  "fichiers appropriés. Ne remplace pas une doc existante sans en conserver les informations utiles.",
                  "Fichiers de documentation écrits + résumé.", (0,)),
        ),
    ),
    Mission(
        "explique", "Comprendre un projet / du code", "🧭",
        "Onboarding : explique l'architecture ou un morceau de code (lecture seule).",
        lecture_seule=True,
        invite="Que veux-tu comprendre ? (ex : 'le projet en général', 'comment marche l'auth', 'fichier x.py')",
        etapes=(
            Etape("architecte",
                  "Question : « {demande} ».\nExplore et lis le code nécessaire puis explique de façon pédagogique : "
                  "vue d'ensemble, composants clés (fichier:ligne), flux de données, points d'attention, glossaire. "
                  "Ajoute un schéma en texte (mermaid ou ASCII) si utile.",
                  "Explication Markdown claire et sourcée."),
        ),
    ),
    Mission(
        "question", "Question rapide sur le code", "💬",
        "Un seul agent, réponse courte (lecture seule).",
        lecture_seule=True,
        invite="Ta question :",
        etapes=(
            Etape("architecte",
                  "Question : « {demande} ».\nRéponds précisément en t'appuyant sur le code (cite fichier:ligne). "
                  "Sois bref.",
                  "Réponse courte et sourcée."),
        ),
    ),
    Mission(
        "projet", "Cadrer un projet IT", "📋",
        "Cahier des charges, architecture cible, backlog (user stories), planning, risques.",
        lecture_seule=True,
        invite="Décris ton projet (objectif, utilisateurs, contraintes, délais, budget...) :",
        etapes=(
            Etape("chercheur",
                  "Projet : « {demande} ».\nIdentifie les choix technologiques pertinents (2-3 options par brique), "
                  "avec avantages/inconvénients, maturité et coût. Utilise la recherche web si disponible.",
                  "Comparatif technologique sourcé et recommandation."),
            Etape("chef_projet",
                  "Projet : « {demande} ». À partir du comparatif, produis un dossier de cadrage complet :\n"
                  "1. Contexte & objectifs (SMART)  2. Périmètre (in/out) & MVP  3. Exigences fonctionnelles et non fonctionnelles\n"
                  "4. Architecture cible (schéma mermaid)  5. Backlog : épopées → user stories avec critères d'acceptation et estimation\n"
                  "6. Planning par sprints/jalons  7. Risques (probabilité, impact, mitigation)  8. Prochaines actions concrètes.",
                  "Dossier de cadrage Markdown prêt à partager.", (0,)),
        ),
    ),
    Mission(
        "veille", "Veille / recherche technique", "🌐",
        "Recherche web, comparatif d'outils, état de l'art, synthèse sourcée.",
        lecture_seule=True, web_requis=True,
        invite="Sujet de recherche (ex : 'FastAPI vs Litestar en 2026', 'meilleure solution de logs pour k8s') :",
        etapes=(
            Etape("chercheur",
                  "Sujet : « {demande} ».\nFais plusieurs recherches, lis les pages les plus pertinentes, puis "
                  "rédige une synthèse : contexte, options, tableau comparatif, recommandation, sources (URL).",
                  "Synthèse Markdown sourcée."),
        ),
    ),
    Mission(
        "devops", "DevOps / CI / Docker", "🚢",
        "Dockerfile, compose, pipeline CI, Makefile, scripts de déploiement.",
        lecture_seule=False,
        invite="Que faut-il mettre en place ? (ex : 'Dockerfile + compose', 'CI GitHub Actions avec tests et lint')",
        etapes=(
            Etape("devops",
                  "Demande : « {demande} ».\nAnalyse la stack du projet puis crée/modifie les fichiers nécessaires "
                  "en suivant les bonnes pratiques (non-root, versions épinglées, cache, secrets en variables). "
                  "Vérifie la syntaxe des fichiers YAML/TOML/JSON.",
                  "Fichiers créés/modifiés et instructions d'utilisation."),
            Etape("securite",
                  "Relis la configuration DevOps produite pour « {demande} » (Git_Diff) : sécurité des images, secrets, "
                  "permissions CI, supply chain. " + RAPPORT,
                  "Revue sécurité de la config DevOps.", (0,)),
        ),
    ),
]}
