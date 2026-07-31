#!/usr/bin/env bash
# Run from the drive root on any Linux PC to get a working porta-winux
# with zero prior installation. Uses the drive's static restic if present.
set -euo pipefail
DRIVE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PORTA_WINUX_DRIVE="$DRIVE"
export PATH="$DRIVE/bin:$PATH"

if ! command -v restic >/dev/null; then
  echo "restic not found. Either:"
  echo "  - install it (dnf install restic / apt install restic), or"
  echo "  - drop a static binary at $DRIVE/bin/restic"
  echo "    (https://github.com/restic/restic/releases, chmod +x)"
  exit 1
fi

# Prefer an installed porta-winux; fall back to the copy shipped on the drive.
if command -v porta-winux >/dev/null; then
  exec porta-winux --drive "$DRIVE" "${@:-list}"
elif [ -d "$DRIVE/porta-winux-src" ]; then
  exec python3 -c "import sys; sys.path.insert(0, '$DRIVE/porta-winux-src'); \
from porta_winux.cli import main; sys.exit(main())" --drive "$DRIVE" "${@:-list}"
else
  echo "porta-winux not installed and no porta-winux-src/ on drive."
  echo "Copy the repo's src/porta_winux to $DRIVE/porta-winux-src/porta_winux, or pip install it."
  exit 1
fi
