# Project Context: 3D ULPIN Generation and Vertical Property Mapping

**Project**: SIH 2026 demo prototype for problem SIH26011, "3D ULPIN Generation and Vertical Property Mapping".  
**Goal**: Ingest a real building (buildingSMART Duplex Apartment IFC), place it on a mock land parcel, build one closed 3D solid per unit, validate it (no overlap, containment), mint 3D ULPINs appended to the parent parcel ULPIN, support ownership transfer and merger with full history, flag plan vs as-built discrepancies, show everything in a CesiumJS 3D viewer, and export CityJSON.

---

## Fixed Rules

- **Tech Stack**: Python 3.11, FastAPI backend, PostgreSQL 16 + PostGIS 3 in Docker, CesiumJS frontend (plain HTML + JS, no build step).
- **Licensing & Dependencies**: Open-source libraries only. No paid APIs.
- **Offline Capable**: Everything runs offline after setup (the demo venue Wi-Fi may fail).
- **Coordinate Reference Systems**:
  - `EPSG:4326` (WGS84) for storage and display.
  - Local metric CRS (UTM zone for the parcel) for geometry maths.
- **Elevation / Heights**: Every height stores `datum_source` and `z_sigma` (metres).
- **Identifiers**: IDs are immutable and never reused.
- **Testing**: Write `pytest` tests for every module.

---

## Token and Quota Rules (Follow Strictly)

- Read only the files needed for the current task. Do not re-read a file you already read in this session unless it changed.
- Do not print whole files back to me. After changes, show only a summary of what changed (max 10 lines).
- Run tests as: `pytest -q -x --tb=short <only the relevant test file>`. Run the full suite only when I ask.
- Do not install optional heavy packages (`torch`, `paddleocr`, `open3d` GUI) unless the task needs them.
- If the same error persists after 3 attempts, stop and report the error and what you tried. Do not keep looping.
- No explanations or tutorials. End each task with: files changed, tests passed or failed, commit hash, next step.

---

## Git Rules (Follow at the End of Every Task)

- All work happens on the `main` branch. Do not create branches.
- Only commit when the task's tests pass. If tests are still failing when you stop, do not commit; report what is failing instead.
- Before committing, run `git status` and make sure no secrets, `.env` files, virtualenvs, `__pycache__`, large generated files or database volumes are staged.
- Stage only the files this task created or changed, then commit with a message in this format: `step-<N>: <short description>`, for example `step-3: IFC extraction of storeys and units`.
- Then run: `git push origin main`
- If the push fails, report the exact error and stop. Never force-push, rewrite history, change the remote or edit git credentials.
