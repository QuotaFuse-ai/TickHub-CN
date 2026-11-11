#!/usr/bin/env bash
# 打最小包：wheel + SHA256SUMS。tag 与 pyproject.toml 的 version 必须一致。
# 用法：PYTHON=.venv/Scripts/python.exe bash scripts/build-release.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python}"
VERSION="$("$PY" -c 'import tomllib,pathlib;print(tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["version"])')"

rm -rf dist-build
"$PY" -m build --wheel --outdir dist-build
"$PY" - <<'PYCODE'
import glob
import hashlib
import pathlib

nl = chr(10)
lines = []
for name in sorted(glob.glob("dist-build/*.whl")):
    digest = hashlib.sha256(pathlib.Path(name).read_bytes()).hexdigest()
    lines.append(digest + "  " + pathlib.Path(name).name)
pathlib.Path("dist-build/SHA256SUMS").write_text(nl.join(lines) + nl, encoding="utf-8")
PYCODE
cat dist-build/SHA256SUMS
echo "version=$VERSION"
