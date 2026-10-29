# claude-code-nano — Claude Code sur NanoGPT

Turbo OS, 29/10/2026. Claude Code (testé en 2.1.312) sert d'interface. Le modèle qui raisonne et code est celui de **ton** abonnement NanoGPT (Kimi, DeepSeek, GLM…), et non Claude. La brique est fournie **sans clé** : mets la tienne.

## Contenu

| Fichier | Rôle |
|---|---|
| `bin/claude-nano` | Lance Claude Code sur NanoGPT. Menu dynamique de **tous** les modèles texte de ton abonnement (catalogue du jour), profils, refus des modèles Claude, Remote Control et trafic non essentiel coupés. |
| `bin/claude-nano-session` | Nouvelle session tmux Claude · Nano (`auto` en argument = mode Auto). |
| `bin/nanogpt-mcp` | Lanceur du serveur MCP officiel `@nanogpt/mcp@1.5.1` (images, recherche…), qui lit la clé dans le fichier. |
| `turbo/nanogpt_client.py` | Connecteur unique, en bibliothèque standard Python : 14 opérations (texte, vision, image, édition, vidéo, voix, transcription, embeddings, recherche, lots, suivi), politique, reprises sur 429/Retry-After, journal sans contenu. |
| `config/nanogpt-politique.json` | Politique : `actif` (interrupteur), profils de modèles, payant interdit par défaut, plafond USD/jour, appelants. |
| `bin/nanogpt-interrupteur` | `etat` / `off` / `on` : coupe ou rétablit NanoGPT en une commande. |
| `bin/nanogpt-openclaw-sync` | Donne à OpenClaw tous les modèles NanoGPT, avec la même méthode que le menu et `/model` (`--essai` = validation seule). |
| `bin/nanogpt-diagnostic` | État en lecture seule ; `--valider` lance des essais réels bornés. |
| `tests/test_nanogpt_client.py` | 23 tests hors réseau (faux serveur 127.0.0.1). |
| `bureau/` | Icône et lanceur de bureau. |
| `conf/openclaw-nanogpt.json` | Configuration OpenClaw prête à fusionner : fournisseur `nanogpt` (7 modèles), secret par fichier, MCP `nanogpt`, 5 agents `nano-*` (rédacteur, codeur, média, veille, auto). Remplacer `~` par le chemin absolu de ton dossier personnel. |
| `conf/catalogue-nanogpt-20261029.json` | Catalogue NanoGPT **complet** du 29/10 : texte 663 (324 abonnement), image 252 (4), vidéo 197, audio 112, 3D 15, embeddings 15, rerank 1, avec capacités et prix. |
| `conf/identifiants-nanogpt.exemple` | Modèle du fichier d'identifiants (sans clé). |

**Modèles** : le menu de `claude-nano` propose **tous** les modèles texte de NanoGPT (628 le 29/10), lus dans le catalogue du jour. Les modèles de l'abonnement viennent d'abord, puis les payants, marqués 💲 : ils exigent du solde, sinon HTTP 402. Les modèles Claude (`anthropic/*`, `claude-*`) sont exclus par la politique (`modeles_interdits_prefixes`) ; retire le préfixe pour les autoriser.

## Installation

```bash
git clone https://github.com/Turbo31150/claude-code-nano.git && cd claude-code-nano && bash installer.sh
```

Il faut ensuite créer `~/.config/nanogpt/credentials.env` (droits 600) avec un éditeur, sur deux lignes : `NANOGPT_API_KEY=<ta clé>` et `NANOGPT_BASE_URL=https://api.nano-gpt.com/api/v1`.

Prérequis : Claude Code (`claude` dans `~/.local/bin`), `python3`, `whiptail`, `tmux`, `node`/`npx` (pour le MCP), `gnome-terminal` (lanceur de bureau).

## Utilisation

```bash
claude-nano                 # menu : ★ Auto + 628 modèles (abonnement puis 💲 payants ; ∥ = outils en parallèle, ⚠ = sans outils)
claude-nano --modeles       # la même liste, en texte
claude-nano -P coding       # profils : coding, reasoning, fast, research, vision, auto, premium (voir la politique)
claude-nano -P premium      # auto-model-premium : NanoGPT choisit un modèle haut de gamme, inclus dans l'abonnement (testé 29/10)
claude-nano -m <id> -p "…"  # modèle précis, une réponse puis sortie
nanogpt-diagnostic          # vérification (droits, compte, catalogues, motif de clé)
nanogpt-interrupteur off    # coupure immédiate
```

**Dans Claude Code, `/model` liste les modèles de ton abonnement** : profils en tête (« ★ premium », « coding »…), puis les autres modèles inclus. Le lanceur écrit lui-même cette liste à chaque démarrage. La découverte native de Claude Code ne garde que les modèles « claude » ou « anthropic » ; elle est rendue inopérante pour ne pas écraser la liste. Choisis avec la touche **`s`** (cette session seulement) : **Entrée** enregistre le modèle comme défaut de `~/.claude/settings.json`, que partage ta commande `claude` habituelle. Une session déjà ouverte garde l'ancienne liste : ouvre-en une neuve (l'icône le fait à chaque clic).

`~/.claude/settings.json` n'est **pas** modifié : la commande `claude` habituelle reste sur ton compte Anthropic.

## Ce qui a été mesuré (29/10/2026)

- **Fournisseur réel.** La génération passe par NanoGPT : `modelUsage` indique le modèle choisi et le compteur d'abonnement augmente. Après durcissement, on mesure **0 connexion** du processus à api.anthropic.com.
- **Base d'URL.** Claude Code ajoute `/v1/messages`. La base doit donc être `https://nano-gpt.com/api`. Avec `…/api/v1`, chaque requête faisait un détour par une redirection 307. Le lanceur est corrigé.
- **Précédence des réglages.** Le bloc `env` de `~/.claude/settings.json` l'emporte sur les `export` du lanceur ; seul `--settings` l'emporte sur lui. Le lanceur passe donc le plafond de sortie (8 000 jetons) et le délai par `--settings`.
- **Streaming et outils.** L'API Messages de NanoGPT, en streaming comme sans, renvoie des `tool_use` corrects, y compris 3 appels parallèles, et se termine par `message_stop`. Résultat : 15 sur 15 en direct et 9 sur 9 via Claude Code.
- **Coût.** Un tour coûte environ **31 000 jetons d'entrée** (prompt système, CLAUDE.md, outils) sur le quota hebdomadaire. Le `total_cost_usd` affiché par Claude Code est une estimation au tarif Claude, **fausse** ici.
- **Fausses gratuités.** Avec un solde à 0 $, les embeddings répondent 402 alors que NanoGPT les liste « abonnement ». Vidéo, voix, transcription, recherche web et lots sont payants. La politique les bloque localement.
- **Mode `--bare`.** Il est déconseillé : il désactive les hooks (garde-fous).
