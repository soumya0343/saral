#!/usr/bin/env bash
# Assemble the Hugging Face Space from this repo into deploy/hf-space/.build and upload it.
#
#   deploy/hf-space/build.sh                 # build + upload to $SPACE (default soumya0343/saral)
#   UPLOAD=0 deploy/hf-space/build.sh        # build only (inspect .build/)
#
# Only what the Space runs is shipped: the backend, data/, alembic.ini, the static frontend, and
# this folder's app.py / README.md / packages.txt. requirements.txt is generated from pyproject.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HERE="$REPO/deploy/hf-space"
OUT="$HERE/.build"
SPACE="${SPACE:-soumya0343/saral}"

rm -rf "$OUT"
mkdir -p "$OUT"
cp "$HERE/app.py" "$HERE/README.md" "$HERE/packages.txt" "$REPO/alembic.ini" "$OUT/"
rsync -a --exclude '__pycache__' --exclude '*.pyc' "$REPO/backend" "$OUT/"
rsync -a "$REPO/data" "$OUT/"

python3 - "$REPO/pyproject.toml" > "$OUT/requirements.txt" <<'PY'
import sys, tomllib
d = tomllib.load(open(sys.argv[1], "rb"))["project"]
print("# Generated from pyproject.toml by deploy/hf-space/build.sh — do not edit.")
print("# torch and gradio come from the ZeroGPU image (torch<=2.13, gradio = sdk_version).")
print("\n".join(d["dependencies"] + d["optional-dependencies"]["embeddings"]))
PY

# Static frontend calling the API on the same origin.
(cd "$REPO/frontend" && rm -rf .next-export && STATIC_EXPORT=1 NEXT_PUBLIC_API_URL=/api npx next build)
mkdir -p "$OUT/static"
cp -R "$REPO/frontend/.next-export/"*.html "$REPO/frontend/.next-export/_next" "$OUT/static/"
rm -rf "$OUT/static/_next/cache"
for page in index.html eval.html; do
  [ -f "$OUT/static/$page" ] || { echo "static export is missing $page" >&2; exit 1; }
done

echo "built $OUT ($(du -sh "$OUT" | cut -f1))"
if [ "${UPLOAD:-1}" = "1" ]; then
  hf upload "$SPACE" "$OUT" . --type space \
    --commit-message "Deploy $(git -C "$REPO" rev-parse --short HEAD)$(git -C "$REPO" diff --quiet || echo '+dirty')"
fi
