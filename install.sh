#!/usr/bin/env bash
# Install a `ballsheet` launcher on your PATH (~/.local/bin by default).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${1:-$HOME/.local/bin}"
LAUNCHER="$BIN_DIR/ballsheet"

mkdir -p "$BIN_DIR"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec python3 "$APP_DIR/ballsheet.py" "\$@"
EOF
chmod +x "$LAUNCHER"
echo "Installed: $LAUNCHER"

# dash / "Show Applications" entry with the BallSheet icon (best effort).
if [ -x "$APP_DIR/install-desktop.sh" ]; then
  "$APP_DIR/install-desktop.sh" "$BIN_DIR" || true
fi

if ! printf '%s' ":$PATH:" | grep -q ":$BIN_DIR:"; then
  echo "NOTE: $BIN_DIR is not on your PATH."
  echo "  Add this line to ~/.bashrc then restart your shell:"
  echo "    export PATH=\"$BIN_DIR:\$PATH\""
else
  echo "Run it with:  ballsheet"
fi
