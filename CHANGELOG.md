# Changelog

## 1.0.0 — first stable release

### Packaging and installation

- Added `pip install .` packaging with setuptools metadata for Jarvis 1.0.0.
- Runtime dependencies are declared once in `requirements.txt` and consumed by
  `setup.py`; development-only tools are separated into `requirements-dev.txt`.
- Added the `jarvis` console entry point, backed by the existing
  `web.launch.main` startup path.
- Included the production React build and built-in Skills in the wheel.
- Installed copies keep configuration, databases, logs and user-installed Skills
  in the platform user-data directory instead of `site-packages`.
- Source checkouts retain the existing repository-local configuration layout.

### Cleanup and release hygiene

- Removed Python/test caches, debug output files and the legacy PATH shim
  installer.
- Expanded `.gitignore` for databases, local contacts, telephony history,
  caches and temporary files while keeping `frontend/dist` in the release.
- Added `pyproject.toml`, `MANIFEST.in` and release dependency separation.
- Unified the application version at 1.0.0.
- Added the MIT `LICENSE` (copyright (c) 2026 Amirxon Gandjayev) and
  `THIRD_PARTY_LICENSES.md`. Removed two bundled UI assets with undocumented
  provenance; the non-WebGL reactor fallback is now rendered from CSS.
  Third-party redistribution concerns (including ebooklib AGPL) require legal
  review before public distribution.

### Reliability and safety

- Completed the tool audit across all registered actions.
- Hardened file writing: content-aware create/write, corrupt Office-file repair,
  read-back verification, exclusive creation, temp cleanup and drive-root
  protection.
- Hardened archive handling: safe ZIP creation/extraction/modification with ZIP
  Slip protection, archive-bomb limits, duplicate/case-collision checks, symlink
  refusal and atomic archive replacement.
- Hardened phone call lifecycle and duplicate-call prevention.
- Hardened approval resume, AgentRun state recovery, scheduler firing/cancel
  races, notification delivery and persistent memory bulk deletion.
- Added explicit screenshot capture with strict opt-in/privacy behavior.

### Verification

- Full Python suite, frontend Vitest suite, TypeScript typecheck, production
  build, Ruff checks and package installation tests are documented in the
  release report.
