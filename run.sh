#!/usr/bin/env bash
# Launch the captcha solver MCP server on the best available interpreter.
# The plugin venv has the neural engine (ddddocr + onnxruntime); the bundled
# DSH python cannot load onnxruntime (hardened runtime, library validation),
# so it is only used as a fallback with tesseract + template engines.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -x "$HERE/.venv/bin/python" ]; then
  PY="$HERE/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  echo "captcha-solver: no python interpreter found" >&2
  exit 1
fi
cd "$HERE/server"
exec "$PY" -m captcha_solver.mcp_server "$@"
