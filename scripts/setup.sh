#!/usr/bin/env bash
# One-shot environment setup for the captcha solver plugin.
#   ./scripts/setup.sh [--mirror <pip index url>]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIRROR="${1:-https://pypi.tuna.tsinghua.edu.cn/simple}"
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for cand in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
  done
fi
echo "captcha-solver: using $PY"
"$PY" -m venv "$HERE/.venv"
"$HERE/.venv/bin/pip" install --quiet --upgrade pip -i "$MIRROR"
"$HERE/.venv/bin/pip" install --quiet -i "$MIRROR" ddddocr numpy pillow
"$HERE/.venv/bin/python" -c "import ddddocr, onnxruntime, numpy, PIL; print('engines ok:', onnxruntime.__version__)"
if ! command -v tesseract >/dev/null 2>&1; then
  echo "captcha-solver: tesseract not found — install it (brew install tesseract tesseract-lang) for the fallback OCR engine" >&2
fi
echo "captcha-solver: ready -> $HERE/.venv"
