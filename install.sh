#!/bin/sh
# Installs Tippex into ~/.tippex and puts a `tippex` command in ~/.local/bin.
set -e
REPO="${TIPPEX_REPO:-https://github.com/dannyfriar/tippex.git}"
DEST="$HOME/.tippex"
BIN="$HOME/.local/bin"

command -v git >/dev/null || { echo "git is required (run: xcode-select --install)"; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required (run: xcode-select --install)"; exit 1; }

if [ -d "$DEST/.git" ]; then
  echo "Updating Tippex in $DEST"
  git -C "$DEST" pull -q --ff-only
else
  echo "Installing Tippex to $DEST"
  git clone -q "$REPO" "$DEST"
fi

python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install -q --disable-pip-version-check -r "$DEST/requirements.txt"

mkdir -p "$BIN"
ln -sf "$DEST/tippex" "$BIN/tippex"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zshrc"
    echo "Added ~/.local/bin to your PATH in ~/.zshrc (open a new Terminal window to use it)."
    ;;
esac

echo "Done. Run: tippex file.pdf"
