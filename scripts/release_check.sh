#!/usr/bin/env sh
set -eu
export PYTHONDONTWRITEBYTECODE=1

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"
CHECK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/asm-check-XXXXXX")
export CHECK_DIR
trap 'rm -rf "$CHECK_DIR"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

python3 - <<'PY'
from pathlib import Path
for path in Path("scripts").glob("*.py"):
    compile(path.read_text(), str(path), "exec")
print("python syntax ok")
PY

python3 - <<'PY'
from pathlib import Path
import os
temp = Path(os.environ['CHECK_DIR'])
p = Path("scripts/agent_skill_manager.py")
s = p.read_text()
start = s.index("<script>") + len("<script>")
end = s.index("</script>", start)
(temp / "asm-ui.js").write_text(s[start:end])
ui = Path("assets/maintenance.html").read_text()
(temp / "asm-maintenance-ui.js").write_text(ui.split("<script>", 1)[1].split("</script>", 1)[0])
PY
if command -v node >/dev/null 2>&1; then
  node --check "$CHECK_DIR/asm-ui.js"
  node --check "$CHECK_DIR/asm-maintenance-ui.js"
  node --check assets/project-space.js
else
  echo "release check failed: node is required for JavaScript syntax validation" >&2
  exit 1
fi

python3 -B -m unittest discover -s tests

TMP_HOME="$CHECK_DIR/home"
ASM_HOME="$TMP_HOME" python3 scripts/agent_skill_manager.py list >"$CHECK_DIR/list.out"
test -f "$TMP_HOME/config.json"
test -f "$TMP_HOME/skills.db"

ASM_HOME="$TMP_HOME" python3 scripts/agent_skill_manager.py --help >"$CHECK_DIR/help.out"

if git ls-files --cached | grep -E '(^|/)(__pycache__/|[^/]+\.pyc$)' >"$CHECK_DIR/tracked-python-cache.txt"; then
  echo "release check failed: tracked Python cache files found" >&2
  cat "$CHECK_DIR/tracked-python-cache.txt" >&2
  exit 1
fi

echo "release check ok"
