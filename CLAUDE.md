# CLAUDE.md

Project-specific instructions for Claude. Read this before running tests, linting,
or pushing changes — every section below corresponds to a failure mode that has
already burned a session.

## Project layout

- `backend/` — FastAPI app (`main.py`), pytest suite under `backend/tests/`,
  ruff + mypy + pytest config in `backend/pyproject.toml`.
- `frontend/` — Vite + React 19 + Carbon. Vitest tests in `frontend/src/__tests__/`,
  ESLint 9 flat config in `frontend/eslint.config.js`, build config in
  `frontend/vite.config.js`.
- `docs/` — GitHub Pages source (static HTML/CSS, served at
  `https://it-rec.github.io/schemabuilder/`). Plain HTML — Jekyll is disabled
  via `docs/.nojekyll`. See "GitHub Pages site (`docs/`)" below.
- `.github/workflows/ci.yml` — the source of truth for what "green" means.

## Golden rule

Before reporting a task done, you MUST run the same commands CI runs and they
MUST all exit 0. Order:

1. `backend`: `ruff check .` → `pytest tests/ -q`
2. `frontend`: `npm run lint` → `npm run test:ci` → `npm run build`

If you only ran one of them, you only checked half the project.

---

## Backend

Always `cd backend` first. `pyproject.toml` lives there; pytest's `testpaths`,
ruff's `extend-exclude`, and the `conftest.py` `sys.path` shim are all
**relative to `backend/`**. Running `pytest` from the repo root silently
ignores the config and may not even discover the tests.

### Install deps (test/lint only — DO NOT install requirements.txt for tests)

```bash
cd backend
pip install fastapi pydantic pytest httpx ruff mypy python-multipart
```

Why not `pip install -r requirements.txt`? It pulls Docling + torch (hundreds
of MB, multi-minute install). The tests **mock Docling** — they never need it.
Installing the full requirements file is the #1 reason a backend session times
out or "tests fail" (they actually fail to even start).

`python-multipart` is needed by FastAPI's `UploadFile` paths even though CI
forgets to list it explicitly — install it.

### Run tests

```bash
cd backend
python -m pytest tests/ -q
```

Expected: `300 passed` (or more — number grows with the rigorous suites).
Suite is fast (< 5s) because Docling is mocked.

If pytest says `No module named pytest`, install via `pip install pytest` (it
isn't always on PATH as the same Python interpreter — prefer
`python -m pytest` over bare `pytest`).

### Lint + type-check

```bash
cd backend
ruff check .                      # MUST be clean — CI gate is hard
mypy --ignore-missing-imports --no-strict-optional main.py   # advisory in CI
```

### OpenAPI snapshot

`openapi-snapshot.json` is committed and CI fails the build (and pytest
fails `test_openapi_snapshot_is_up_to_date`) if it drifts. Anything that
adds/removes a route, renames a path param, or changes a Pydantic model
schema requires regenerating it:

```bash
cd backend
python export_openapi.py SNAPSHOT   # writes openapi-snapshot.json
git add openapi-snapshot.json
```

Then commit alongside the API change. A frontend-only change is rare to
trigger this — but if `services/api.js` adds a call to an endpoint the
backend doesn't yet have, that's a separate kind of drift the snapshot
won't catch.

Ruff config (`backend/pyproject.toml`):
- target: `py311` (not 3.14 — don't use match-statement features, `Self`
  imports from `typing`, etc.)
- rules: `E, F, I, B`; `E501` (line length) and `B008` (FastAPI `Depends`) are
  off.
- `tests/*` ignores `B011` (`assert False` is fine in tests).

If ruff complains about import order (`I001`), fix it — don't `# noqa` it.

---

## Frontend

Always `cd frontend` first.

### Install deps

```bash
cd frontend
npm ci --no-audit --no-fund
```

Use `npm ci`, not `npm install`. `package-lock.json` is the contract; `npm ci`
gives a reproducible tree and is what CI uses. If `node_modules/` is empty,
**every** command below fails with cryptic errors — install first.

### Tests

```bash
cd frontend
npm run test:ci          # vitest run — one-shot, scripted
```

Do NOT use `npm test` from a non-interactive shell — it launches Vitest in
watch mode and hangs forever. `test:ci` runs `vitest run` (single pass).

Expected: `Test Files 9 passed (9)` / `Tests 77 passed (77)` (App,
BatchExtractModal, DefinitionEditor, DefinitionHistory, DocumentList,
DocumentViewer, ExampleTeacher, FieldsPanel, TextEntriesPanel).

Vitest exposes `describe / it / test / expect / vi` as globals (via
`test.globals: true` in `vite.config.js`). Use `vi.fn()`, `vi.mock()`,
`vi.spyOn()` — not the legacy `jest.*` equivalents. The test-file override
in `eslint.config.js` declares those globals so referencing them won't
trip `no-undef`.

### Lint

```bash
cd frontend
npm run lint             # eslint --max-warnings 0
```

Local lint is **stricter than CI**: the npm script uses `--max-warnings 0`, CI
uses `--max-warnings 999`. If `npm run lint` fails locally, CI may still pass —
but treat warnings as failures anyway, because the next person to bump the CI
cap will break.

Common offenders:
- `react-hooks/exhaustive-deps` — fix the deps array, don't disable the rule.
- `testing-library/prefer-find-by` — use `findBy*` instead of `waitFor` +
  `getBy*`. (See commit `aaf6a69` for the pattern.)
- `jsx-a11y/*` — Carbon components mostly handle this; only triggers on raw
  HTML.
- `no-alert` is enabled on purpose. The three intentional `window.confirm`
  call sites carry `// eslint-disable-next-line no-alert`; if you remove
  one of those confirms, also remove the disable, otherwise ESLint 9 flags
  it as a stale directive (which it treats as a warning → CI fail at 0).
  When the disable is above a multi-line `if (...)`, put it on the same
  line as the `!window.confirm(` call inside the parentheses, not above
  the `if (` — ESLint 9 attaches `disable-next-line` to exactly one line.

### Build

```bash
cd frontend
npm run build           # vite build — output goes to frontend/build/
```

If lint passes but `vite build` warns about unresolved URLs (e.g.
`~@ibm/plex/...didn't resolve at build time`), it means Carbon's SCSS
emitted a webpack `~`-prefixed `url()` that the alias in `vite.config.js`
(`{ find: /^~(.+)$/, replacement: "$1" }`) isn't matching — fix the alias.
A warning here is a real bug: the resulting CSS will reference a literal
`~@ibm/plex/...` URL and the font will 404 in production.

### Dev server / env vars

```bash
cd frontend
npm run dev             # vite dev server on http://localhost:3000
```

Client-side env vars must be prefixed `VITE_` (not `REACT_APP_`) and are
read via `import.meta.env.VITE_FOO`. The two we use today are `VITE_API_URL`
and `VITE_API_TIMEOUT_MS` (see `src/services/api.js`). If you add another and
forget the `VITE_` prefix, Vite silently drops it and you'll get `undefined`
at runtime — there is no helpful error.

---

## GitHub Pages site (`docs/`)

The Pages site at `https://it-rec.github.io/schemabuilder/` is a static
HTML/CSS landing page served straight out of `docs/` on the default branch.
It is **not** a Jekyll site — `docs/.nojekyll` disables Jekyll so the files
are served verbatim. Pages config: Settings → Pages → Source =
"Deploy from a branch" → Branch = default branch, folder = `/docs`.

Files:
- `docs/index.html` — feature index (anchor links grouped by area) at the
  top, longer per-feature descriptions below.
- `docs/styles.css` — Carbon-flavored typography, `prefers-color-scheme`
  dark mode, responsive grid.
- `docs/.nojekyll` — empty sentinel. Do not delete; without it GitHub Pages
  runs Jekyll and may rewrite or skip files.

### When to update it

Update `docs/index.html` whenever you ship a feature that ends up in the
"Shipped on branch …" list in `FEATURES.md`. The two should describe the
same set of capabilities, just with different audiences:
- `FEATURES.md` — internal backlog + brief shipping notes for contributors.
- `docs/index.html` — user-facing description of what works today.

Concretely, when adding a feature:
1. Add a `<li><a href="#anchor">Name</a></li>` entry under the right
   group in the "At a glance" grid.
2. Add a matching `<h3 id="anchor">Name</h3>` + paragraph under "Detailed
   descriptions" — a couple of sentences in plain English, no internal
   ticket numbers, no branch names, no `match_reason` jargon without
   explaining it.
3. If the feature changes the README's setup / API / config tables, update
   the README too — the Pages site links into those sections by anchor.

When removing or renaming a feature, delete both the index entry **and**
the `<h3>` block. Orphan anchors are a real footgun: the "At a glance"
links will 404 in-page silently.

### Sanity check before pushing

After editing `docs/index.html`, run this — it catches `href="#x"` with no
matching `id="x"` (the most common breakage):

```bash
python3 -c "
import re
html = open('docs/index.html').read()
ids   = set(re.findall(r'id=\"([^\"]+)\"', html))
hrefs = set(re.findall(r'href=\"#([^\"]+)\"', html))
missing = hrefs - ids
print('broken anchors:', sorted(missing) if missing else 'none')
" </dev/null
```

Section heading ids used only via `aria-labelledby` (e.g. `toc-heading`,
`details-heading`) are expected to be "never linked" — that's fine.

There is no CI gate on this file. It will not be flagged by ruff, pytest,
eslint, or vitest. Treat the sanity check as the gate.

---

## What CI runs (`.github/workflows/ci.yml`)

For reference — keep parity with this:

| Job      | Steps                                                                  |
| -------- | ---------------------------------------------------------------------- |
| backend  | `pip install fastapi pydantic pytest httpx ruff mypy python-multipart` → `ruff check .` → `mypy ... \|\| true` → `python export_openapi.py` ↔ `openapi-snapshot.json` drift check → `pytest tests/ -q` |
| frontend | `npm ci` → `npm run lint -- --max-warnings 999` → `npm run test:ci` (vitest run) → `npm run build` (vite build) |

Python 3.11, Node 20.

> ⚠️ **Backend install list must match `.github/workflows/ci.yml` exactly.**
> If you add an endpoint that pulls a new transitive (e.g. `python-multipart`
> for `UploadFile`, or `pypdfium2` if a new test stops mocking it), you MUST
> update **both** this CLAUDE.md install list **and** the workflow's
> `Install lint/test tooling` step. CI uses the workflow file; local uses
> this CLAUDE.md. They will drift silently because passing locally proves
> nothing about CI. Past failure mode: an UploadFile route added without
> updating CI, every pytest fails at import-time with a misleading
> "Form data requires python-multipart" stack.

---

## Common pitfalls (each has bitten before)

1. **Running pytest from the repo root.** pyproject.toml is in `backend/`;
   pytest won't find its config. → `cd backend` first.
2. **Installing `requirements.txt` for tests.** Pulls Docling + torch.
   Unnecessary and slow. → use the lean install list above.
3. **Forgetting `python-multipart`.** FastAPI form/upload handling breaks at
   import time (the route walker calls `ensure_multipart_is_installed` when
   it sees an `UploadFile` param). → include it in BOTH the local install
   list above AND the CI workflow's install step. They drift silently.
4. **`npm test` instead of `npm run test:ci`.** Hangs in Vitest watch mode. →
   always `test:ci` when scripted.
5. **Empty `frontend/node_modules/`.** Lint, test, build all fail with
   nonsense errors. → `npm ci` first.
6. **Local lint passes, CI fails (or vice versa).** Local = `--max-warnings 0`,
   CI = `--max-warnings 999`. → match the stricter one (0).
7. **Editing code without running ruff.** Backend CI fails on `I001` import
   order regularly. → run `ruff check .` before committing.
8. **Targeting Python 3.14 features.** Ruff target is `py311`, CI Python is
   3.11. The 3.14 mention in README is for the deployment side, not tests.
9. **Mypy "passes" in CI but errors locally.** CI uses `|| true` for mypy. It's
   advisory — still worth fixing, but don't block on it the way you would on
   ruff/pytest.
10. **Adding `# noqa` to silence ruff.** Almost always wrong. Fix the import
    order, remove the unused import, etc. Suppression is a last resort.
11. **Pytest passes locally, CI's pytest fails on import.** Symptom: every
    test fails with a `RuntimeError` or `ImportError` in the collection
    phase rather than in an assertion. Cause: a runtime dep that's in the
    local install list but missing from `.github/workflows/ci.yml`. See the
    "Backend install list must match" callout above. Diagnose by
    reproducing the CI install in a fresh venv:
    ```bash
    python -m venv /tmp/ci && . /tmp/ci/bin/activate
    pip install fastapi pydantic pytest httpx ruff mypy   # exactly what CI installs
    cd backend && pytest tests/ -q
    ```
    Whatever fails there is what CI will fail with.
12. **Shipping a feature without updating `docs/index.html`.** The Pages
    site is a separate artifact from the README and `FEATURES.md`; nothing
    in CI checks that it's in sync. → when a feature lands in the
    "Shipped on branch …" list in `FEATURES.md`, add a matching entry to
    `docs/index.html` (both the index link and the description block).
    See "GitHub Pages site (`docs/`)" above.
13. **Deleting `docs/.nojekyll`.** Empty file, looks like clutter; without
    it GitHub Pages runs the site through Jekyll, which mangles files
    whose names start with `_` and applies a default theme over `index.html`.
    → leave it alone.

---

## Quick "is everything green?" recipe

Run this end-to-end before claiming done:

```bash
# Backend
cd backend
pip install -q fastapi pydantic pytest httpx ruff mypy python-multipart
ruff check . && python -m pytest tests/ -q

# Frontend
cd ../frontend
[ -d node_modules ] || npm ci --no-audit --no-fund
npm run lint && npm run test:ci && npm run build
```

If any of the four gates (ruff, pytest, eslint, vitest, vite build) is red,
the task is not done.

---

## Commit and Pull Requests

When you commit or create pull request, leave out any claude references in comments and title.

So NO `Generated with [Claude Code]` lines or equal.
