# Contributing to Jarvis

Thanks for helping improve Jarvis. This project is a local, voice-first Gemini
Live assistant with a Python backend and a React frontend. Changes should keep
that architecture intact and preserve its security and approval boundaries.

## Before you start

- Open an issue or discuss the change before starting a large feature,
  refactor, dependency addition, or architectural change.
- Bug fixes should include a regression test that fails without the fix.
- Do not include credentials, `.env` files, local databases, user screenshots,
  call history, or personal file paths in commits or issues.
- Do not weaken approvals, path protections, or the local-only server boundary
  to make a change easier.

## Development environment

Requirements:

- Python **3.11+**
- Node.js **20.19+** and npm for frontend development
- Windows 11 is the primary target; macOS/Linux code paths should remain intact

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install -e ".[dev]"

cd frontend
npm install
npm run build
cd ..

python main.py
```

For frontend hot reload:

```powershell
python main.py --dev
```

The console entry point `jarvis` runs the same `web.launch.main` startup path.
Source checkouts keep configuration in the repository `config/` directory.
Installed packages use the platform user-data directory documented in the
README.

## Project layout

| Area | Responsibility |
|---|---|
| `main.py` | Gemini Live session loop and tool orchestration |
| `web/` | FastAPI server, WebSocket events, runtime facade and launcher |
| `actions/` | Tool modules, declarations, dispatch and timeouts |
| `core/` | AgentRun, approvals, memory, scheduler, notifications, SIP, Gemini helpers |
| `Skills/` | Skills engine and built-in/user Skill packages |
| `config/` | Settings, credentials and user data in a source checkout |
| `frontend/` | React/TypeScript UI and production Vite build |
| `tests/` | Python unit/integration/browser-smoke tests |

## Code guidelines

- Keep the existing architecture: one Gemini Live session, one tool registry,
  one approval system, one configuration store.
- Prefer the standard library and existing dependencies. Adding a dependency
  requires a clear justification and must not duplicate existing capability.
- Validate external paths and archive entries. Never trust model-supplied
  paths, tokens, confirmation flags, or archive names.
- Return truthful structured results. Report the actual failure instead of a
  generic success, and verify filesystem/archive results where applicable.
- Use temporary files and atomic replacement for destructive writes when the
  existing code path supports it.
- Keep user-facing tool results bounded; large output must be paginated or
  truncated with an explicit marker.
- Preserve Windows compatibility, including path, junction and case-insensitive
  filename behavior.
- Do not redesign the dashboard or add unrelated UI surfaces for backend work.
- Keep secrets out of logs, tool results, approvals and error messages.

## Adding or changing a tool

1. Add the module under `actions/` using the `*_tool.py` convention.
2. Register its action(s) in `actions/tool_calling.py`.
3. Add a complete Gemini function declaration with required/optional parameters.
4. Reuse existing path resolution, approvals and timeout conventions.
5. Update `core/prompt.txt` only where model intent needs clarification.
6. Add tests covering success, validation, failure, security, registration and
   declaration consistency.
7. Run the tool-specific tests before the full suite.

Tool actions must not perform destructive or high-impact operations without
the centralized approval path. Never add a parallel approval mechanism.

## Tests and checks

Backend:

```powershell
python -m pytest -q
```

Frontend:

```powershell
cd frontend
npm test -- --run
npm run typecheck
npm run build
```

Ruff on changed Python files:

```powershell
python -m ruff check <changed-python-files>
```

The opt-in browser smoke suite requires Playwright and a built frontend:

```powershell
$env:AZE_UI_TESTS="1"
python -m pytest tests/test_web_ui_smoke.py -q
```

Do not remove or weaken failing tests. If behavior changed intentionally, update
the expectation and explain why in the change description.

## Pull request checklist

- [ ] The change stays within scope and preserves the existing architecture.
- [ ] Destructive paths still use the central approval service.
- [ ] No secrets, local configuration, databases or personal data are included.
- [ ] New behavior has regression tests.
- [ ] Full Python tests pass.
- [ ] Frontend tests, TypeScript and production build pass when frontend changed.
- [ ] Ruff passes for changed Python files.
- [ ] Documentation (README/CHANGELOG) is updated when user-facing behavior changes.
- [ ] I have not claimed an operation succeeded without verifying the actual result.

## Reporting security issues

Do not publish API keys, tokens, passwords, call recordings, screenshots, local
paths, or personal data in a public issue. Report the affected component and a
minimal reproduction that does not expose secrets.
