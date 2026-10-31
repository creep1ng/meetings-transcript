#!/usr/bin/env bash
# Builds and installs Actas for the current user:
#   ~/.local/bin/{actas,actasd}, systemd user service, .desktop entry,
#   Noctalia plugin (symlink) and a Hyprland rules snippet.
set -euo pipefail
cd "$(dirname "$0")"

BIN="$HOME/.local/bin"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"

cargo build --release -p actas -p actasd
install -Dm755 target/release/actas "$BIN/actas"
install -Dm755 target/release/actasd "$BIN/actasd"
install -Dm644 packaging/actasd.service "$CONF/systemd/user/actasd.service"
install -Dm644 packaging/actas.desktop "$DATA/applications/actas.desktop"
if [[ ! -e "$CONF/hypr/actas.lua" ]]; then install -Dm644 packaging/actas.lua "$CONF/hypr/actas.lua"; fi

PLUGIN="$DATA/noctalia/plugins/actas"
if [[ ! -e "$PLUGIN" || -L "$PLUGIN" ]]; then
  ln -sfn "$PWD/noctalia-plugin" "$PLUGIN"
fi

# Noctalia user template: renders the palette to ~/.config/actas/theme.json
# on every theme change; Actas reloads it live.
install -Dm644 packaging/noctalia-actas-theme.json "$CONF/noctalia/templates/actas-theme.json"
if [[ -f "$CONF/noctalia/config.toml" ]] && ! grep -q "theme.templates.user.actas" "$CONF/noctalia/config.toml"; then
  cat >> "$CONF/noctalia/config.toml" <<'TOML'

# Actas (grabación de reuniones): paleta para su interfaz gpui.
[theme.templates.user.actas]
input_path = "$XDG_CONFIG_HOME/noctalia/templates/actas-theme.json"
output_path = "$XDG_CONFIG_HOME/actas/theme.json"
TOML
fi

# A running UI process would keep the old binary; the daemon restarts it.
pkill -x actas 2>/dev/null || true

systemctl --user daemon-reload
systemctl --user enable --now actasd.service
systemctl --user restart actasd.service

if command -v noctalia >/dev/null 2>&1; then
  noctalia msg plugins enable actas/actas >/dev/null 2>&1 || true
  noctalia msg config-reload >/dev/null 2>&1 || true
  noctalia msg templates-apply >/dev/null 2>&1 || true
fi

cat <<MSG

Actas instalado.
  · Demonio:      systemctl --user status actasd
  · Modelos:      actas models download   (≈3 GB, única operación con red)
  · Noctalia:     añade el widget «Actas» a la barra (Ajustes → Barra)
  · Hyprland:     añade require("actas") a ~/.config/hypr/hyprland.lua
MSG
