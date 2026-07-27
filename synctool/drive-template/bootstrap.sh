#!/usr/bin/env bash
# Run from the drive root on any Linux PC to get a working synctool
# with zero prior installation. Uses the drive's static restic if present.
set -euo pipefail
DRIVE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SYNCTOOL_DRIVE="$DRIVE"
export PATH="$DRIVE/bin:$PATH"

if ! command -v restic >/dev/null; then
  echo "restic not found. Either:"
  echo "  - install it (dnf install restic / apt install restic), or"
  echo "  - drop a static binary at $DRIVE/bin/restic"
  echo "    (https://github.com/restic/restic/releases, chmod +x)"
  exit 1
fi

# Prefer an installed synctool; fall back to the copy shipped on the drive.
if command -v synctool >/dev/null; then
  exec synctool --drive "$DRIVE" "${@:-list}"
elif [ -d "$DRIVE/synctool-src" ]; then
  exec python3 -c "import sys; sys.path.insert(0, '$DRIVE/synctool-src'); \
from synctool.cli import main; sys.exit(main())" --drive "$DRIVE" "${@:-list}"
else
  echo "synctool not installed and no synctool-src/ on drive."
  echo "Copy the repo's src/synctool to $DRIVE/synctool-src/synctool, or pip install it."
  exit 1
fi
