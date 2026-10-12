#!/usr/bin/env sh
set -eu
export PYTHONDONTWRITEBYTECODE=1

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

python3 - <<'PY'
from pathlib import Path
for path in Path("scripts").glob("*.py"):
    compile(path.read_text(), str(path), "exec")
print("python syntax ok")
PY

python3 - <<'PY'
from pathlib import Path
p = Path("scripts/agent_skill_manager.py")
s = p.read_text()
start = s.index("<script>") + len("<script>")
end = s.index("</script>", start)
Path("/tmp/asm-ui.js").write_text(s[start:end])
ui = Path("assets/maintenance.html").read_text()
Path("/tmp/asm-maintenance-ui.js").write_text(ui.split("<script>", 1)[1].split("</script>", 1)[0])
PY
if command -v node >/dev/null 2>&1; then
  node --check /tmp/asm-ui.js
  node --check /tmp/asm-maintenance-ui.js
else
  echo "node not found; skipping embedded JavaScript syntax check"
fi

python3 -B -m unittest discover -s tests

TMP_HOME=$(mktemp -d "${TMPDIR:-/tmp}/asm-home-XXXXXX")
ASM_HOME="$TMP_HOME" python3 scripts/agent_skill_manager.py list >/tmp/asm-release-list.out
test -f "$TMP_HOME/config.json"
test -f "$TMP_HOME/skills.db"

python3 scripts/agent_skill_manager.py --help >/tmp/asm-release-help.out

if find . -name "__pycache__" -o -name "*.pyc" | grep -q .; then
  echo "release check failed: Python cache files found" >&2
  find . -name "__pycache__" -o -name "*.pyc" >&2
  exit 1
fi

echo "release check ok"
