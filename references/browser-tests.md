# Browser regression tests

Runtime requirements for these development tests: Node.js, Playwright and a
Playwright Chromium installation. These are not requirements for running the
Skills Manager Python backend.

Run a test with an output path outside the repository:

```sh
PYTHONDONTWRITEBYTECODE=1 node tests/browser_stability.cjs /tmp/asm-browser-result.json
```

`ASM_PLAYWRIGHT_MODULE` optionally selects an installed Playwright module.
`ASM_BROWSER_EXECUTABLE` optionally selects a Chromium-based browser executable.
Without overrides the runners use standard Node module resolution and the
browser installed by Playwright. On Windows, set these environment variables
using PowerShell before invoking Node.

The browser fixtures use temporary installation and data directories, not the
running personal management service. Live probes are separate utilities and
must not be used as automatic destructive release tests.

`node tests/browser_project_space.cjs` verifies project selection, remembered
exclusions, rescanning, batch analysis, reload recovery, checkbox-only selection,
single-line project rows and cleanup impacts. Its disposable projects also cover
cancelled moves, confirmed moves, restore conflicts and confirmed trash purges.
Desktop and mobile viewports are checked without touching real user projects.
