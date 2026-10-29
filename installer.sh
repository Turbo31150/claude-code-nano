#!/usr/bin/env bash
# installer.sh — brique « Claude · Nano » : Claude Code branché sur NanoGPT (Turbo OS, 29/10/2026).
# Copie les fichiers dans ~/jarvis/{bin,turbo,config,tests}, le lanceur dans ~/.local/bin et ~/.local/share.
# Tout fichier existant est d'abord copié en .avant-brique-<date>. Aucune clé n'est fournie ni créée.
set -euo pipefail
ICI="$(cd "$(dirname "$0")" && pwd)"
D=$(date +%Y%m%d-%H%M%S)
poser() {  # poser <source> <destination> [droits]
  mkdir -p "$(dirname "$2")"
  [[ -e "$2" ]] && cp -p "$2" "$2.avant-brique-$D" && echo "  copie de sécurité : $2.avant-brique-$D"
  cp "$1" "$2"; chmod "${3:-644}" "$2"; echo "  posé : $2"
}
for f in claude-nano claude-nano-session nanogpt-mcp nanogpt-interrupteur nanogpt-diagnostic nanogpt-openclaw-sync; do poser "$ICI/bin/$f" "$HOME/jarvis/bin/$f" 755; done
poser "$ICI/turbo/nanogpt_client.py" "$HOME/jarvis/turbo/nanogpt_client.py"
[[ -e "$HOME/jarvis/config/nanogpt-politique.json" ]] && echo "  politique existante conservée : ~/jarvis/config/nanogpt-politique.json" \
  || poser "$ICI/config/nanogpt-politique.json" "$HOME/jarvis/config/nanogpt-politique.json"
poser "$ICI/tests/test_nanogpt_client.py" "$HOME/jarvis/tests/test_nanogpt_client.py"
mkdir -p "$HOME/.local/bin"; ln -sfn "$HOME/jarvis/bin/claude-nano" "$HOME/.local/bin/claude-nano"; echo "  lien : ~/.local/bin/claude-nano"
poser "$ICI/bureau/claude-nano.svg" "$HOME/.local/share/icons/claude-nano.svg"
mkdir -p "$HOME/.local/share/applications"
sed "s#@HOME@#$HOME#g" "$ICI/bureau/claude-nano.desktop" > "$HOME/.local/share/applications/claude-nano.desktop"
echo "  lanceur : ~/.local/share/applications/claude-nano.desktop"
mkdir -p "$HOME/.config/nanogpt"; chmod 700 "$HOME/.config/nanogpt"
echo
echo "Reste à faire (une seule fois) : créer le fichier d'identifiants avec VOTRE clé NanoGPT, en 600 :"
echo "  1. ouvrir un éditeur sur ~/.config/nanogpt/credentials.env"
echo "  2. y écrire deux lignes :  NANOGPT_API_KEY=<votre clé>   et   NANOGPT_BASE_URL=https://api.nano-gpt.com/api/v1"
echo "  3. chmod 600 ~/.config/nanogpt/credentials.env"
echo "Puis : nanogpt-diagnostic   (lecture seule)   et   claude-nano   (menu des modèles de votre abonnement)."
