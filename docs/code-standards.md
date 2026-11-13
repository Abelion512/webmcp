# Code standards

How code in this repo should be structured and written, where the
industry standard comes from, where this repo deliberately deviates,
and what automated checks enforce it. Read this before adding a file,
a function, or a test.

## Sources (industry standard)

| Topic | Source |
| --- | --- |
| Python style, naming, layout | [PEP 8](https://peps.python.org/pep-0008/) — consistency within a project beats any single guide; project-specific guides win on conflict |
| Python readability, docstrings, function size | [Google Python Style Guide](https://google.github.io/styleguide/pyguide.md) — small focused functions (~40 lines is the "think about splitting" signal), descriptive names, no abbreviations |
| Clean code principles | Martin, *Clean Code* Ch.3 — small functions, one thing, one level of abstraction, intention-revealing names, no side effects, command/query separation |
| JavaScript style | [Airbnb JS Style Guide](https://github.com/airbnb/javascript) — camelCase, descriptive names (no single letters), consistent formatting |
| Repository structure | [Software Engineering Guide §2.14](https://software-engineering-guide.github.io/software-engineering-guide/chapters/02-14-project-and-repository-structure/) — least surprise layout, README as front door, separate source/tests/docs/scripts physically |
| Test strategy | [Test pyramid (Fowler)](https://martinfowler.com/articles/practical-test-pyramid.html) — many small fast unit tests, some integration, very few e2e; descriptive test names |

The project skill `code-review-and-quality` (five-axis review:
correctness, readability, architecture, security, performance) is the
review checklist that applies these sources at merge time.

## Repository structure (this repo)

```text
bridge.py                  WebSocket bridge (asyncio subprocess supervisor)
bridge_auth.py             Handshake policy: origin allowlist, token file, rotation
workspace_mcp.py           Workspace MCP server (28 tools, stdio)
setup-config.py            Writes config.json (Linux only)
tests/test_*.py            Python unit tests (stdlib unittest style; pytest config in pyproject.toml)
e2e/e2e_live.py             Live e2e: real bridge + subprocess, ephemeral port
e2e/smoke_extension_live.py Live handshake/rotation smoke in headless Chrome
snapgent-extension/
  background.js            Service worker: owns the bridge WebSocket
  popup.html / popup.js    Popup UI (status only, no logic)
  core/                    Provider-agnostic core (config, parser, main loop)
  providers/<site>.js      Per-AI-site DOM adapters
  test-*.js                Node stub-DOM tests (zero npm deps)
docs/                      Design, stability, security/ops, this file
.github/workflows/         CI (pytest + node + e2e)
.github/labeler.yml        Path labels
```

Rules:

- **New concern → new place.** Source at root (flat, stdlib-only, no
  package — see deviations), tests next to the code they test
  (`test_<module>.py`, `test-<area>.js`), one-off tooling in
  `scripts/`, docs in `docs/`. Never mix them without a reason.
- **Dependencies flow one way.** Extension → bridge → MCP subprocess.
  `workspace_mcp` must not know about the bridge; the bridge must not
  know workspace internals. Providers must not know the agent loop.
- **README is the front door.** Every new top-level file or tool must
  be reachable from README (structure tree or a linked doc).

## Writing good lines of code

1. **Names narrate.** Functions are verbs (`normalize_target`,
   `refresh_bridge_status`), variables are nouns, booleans read as
   questions (`is_inside_root`, `alive`). If a name needs a comment to
   explain it, rename it. No single-letter names outside tiny scopes
   (loop counters, `e` in except). Python: `snake_case`, classes
   `CapWords`, constants `UPPER_CASE`. JS: `camelCase`, classes
   `PascalCase`.
2. **Small, one thing, one level of abstraction.** Target <40 lines per
   function. If you can extract a name for a block, extract it. If a
   function needs "and" in its description, split it.
3. **Pure first, side effects at the edge.** Core logic takes inputs
   and returns outputs; file I/O, subprocess, and network live in thin
   wrappers (`_run_process`, `log`, `send`). Command functions change
   state, query functions answer — not both.
4. **Fail closed at boundaries.** Validate external input (tool args,
   WebSocket frames, config) before use, with explicit errors — see
   `docs/security-and-ops.md`. Never `eval`, never shell-string
   commands, never `innerHTML` with bridge data (popup uses
   `textContent` only).
5. **Comments explain why, not what.** Non-obvious intent, incident
   references, and platform quirks only. Code that describes itself
   needs no comment.
6. **No dead code.** Unused imports, unreachable branches, and
   superseded helpers are deleted in the same change (pyflakes in CI
   catches the import case).
7. **Tests are part of the change.** Every behavior change ships with a
   test that fails without it. Test names state behavior:
   `test_symlink_traversal_does_not_leak_external_files`, not
   `test_search`. Pyramid shape: many unit (`test_*.py`, stub-DOM
   `test-*.js`), one live e2e (`e2e/e2e_live.py`), all in CI.

## Enforcement (automated, not willpower)

| Check | Where | Command |
| --- | --- | --- |
| Python unit | CI `tests` job | `/usr/bin/python3 -m pytest -q` |
| Python static (unused imports/names) | CI `tests` job | `python -m pyflakes workspace_mcp.py bridge.py bridge_auth.py setup-config.py tests/*.py e2e/*.py` |
| JS syntax + stub-DOM | CI `tests` job | `node --check` per file in review; `node snapgent-extension/test-*.js` |
| Live e2e | CI `e2e` job | `python e2e/e2e_live.py` |
| Pairing/rotation handshake | Manual (needs Chrome) | `python e2e/smoke_extension_live.py` |
| Editor formatting baseline | `.editorconfig` | 4-space Python, 2-space JS/HTML/YAML, LF, final newline, 100-col soft limit |

## Known deviations (documented, not drift)

- **Tests in `tests/`, e2e in `e2e/`.** The project stays flat at the root for
  runtime modules (`bridge.py`, `workspace_mcp.py`), but test and live-e2e
  scripts live in their own folders so the root lists only shippable code.
  `python -m pytest -q` from the root works via `pythonpath = ["."]` in
  `pyproject.toml`; `python -m unittest discover -s tests` works via `python -m`.
- **Line length ~100, not 79/80.** The 28-tool catalog is written as
  one `_tool(...)` per line for scanability; PEP 8 itself says
  consistency within a module wins. New code should stay ≤100.
- **Large files exist.** `core/main.js` (~4.8k lines) is the known
  decomposition candidate; `bridge.py` (~850) and `workspace_mcp.py`
  (~700) are next. Per the review skill, a change that materially
  grows a large file should extract helpers first. Do not add new
  subsystems to `main.js` — put them in `core/` or a provider.
- **No `requirements.txt` / `pyproject` dependencies.** Runtime is
  stdlib + `websockets` only; CI installs test deps explicitly. Adding
  a dependency needs the review-skill checklist (maintained, licensed,
  vulnerabilities checked, lockfile reviewed).
- **Docstrings are sparse.** Tool descriptions in `TOOLS` and comments
  at non-obvious sites carry the contract for this MCP surface;
  Google-style mandatory docstrings apply to new *public* Python APIs.

## Audit snapshot (2026-11-04, branch `hardening-e2e-ci-docs`)

| Area | Verdict | Evidence |
| --- | --- | --- |
| Naming (Python/JS) | Pass | `snake_case` / `camelCase` throughout; no single-letter identifiers outside loops |
| Function size | Pass with exceptions | New helpers (`_inside_root`, `refresh_bridge_status`, `serverLines`) are small; `core/main.js` is the outlier above |
| Structure | Pass with deviation | One-way dependencies hold; tests-at-root documented above |
| Static checks | **Fixed this change** | pyflakes found 1 unused import (`test_bridge.py: asyncio`) — removed; pyflakes now clean and in CI |
| Formatting enforcement | **Added this change** | `.editorconfig` + `pyproject.toml` (pytest config) added; previously none |
| Dead code | Pass (after popup fix) | `serverLines`/`fmtUptime`/`fmtRss` were added but unused mid-task; wired into `render()` and covered by `test-popup-status.js` |
| Tests | Pass | 23 pytest + 64 Node stub-DOM assertions + 22 live e2e checks, all in CI |
