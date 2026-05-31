#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

python3 - <<'PY'
from pathlib import Path
source = Path("scripts/agent_skill_manager.py").read_text()
compile(source, "scripts/agent_skill_manager.py", "exec")
print("python syntax ok")
PY

python3 - <<'PY'
from pathlib import Path
p = Path("scripts/agent_skill_manager.py")
s = p.read_text()
start = s.index("<script>") + len("<script>")
end = s.index("</script>", start)
Path("/tmp/asm-ui.js").write_text(s[start:end])
PY
if command -v node >/dev/null 2>&1; then
  node --check /tmp/asm-ui.js
else
  echo "node not found; skipping embedded JavaScript syntax check"
fi

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
