# 3D ULPIN Cadastral & Vertical Property Viewer

A static, browser-based 3D viewer built with **CesiumJS** (via CDN, no build step or node/bundler required) to visualize 3D spatial units, vertical property ownership boundaries, floor levels, and plan-vs-as-built discrepancies.

---

## Features

1. **3D Solid Rendering**: Fetches registrable 3D units from the FastAPI backend (`GET /units`) and renders each spatial unit's extruded volume between `z_min` and `z_max`.
2. **Stable Ownership Color Coding**: Units are deterministically color-coded by registered owner/party using a stable hash. Units without owners render in neutral slate gray.
3. **Storey / Level Filter**: Dropdown menu allows isolating specific floors (e.g. Level 1, Level 2) or viewing all levels at once.
4. **Discrepancy Highlighting**: Polls `GET /discrepancies` and highlights flagged unapproved construction (e.g., extra storeys) in high-visibility red wireframe/volume overlays above the roofline. Includes a **Run as-built check** button.
5. **Interactive Unit Details Modal**: Clicking any 3D unit in the globe queries `GET /units/{ulpin_3d}` and opens a detailed modal showing `ulpin_3d`, `parent_ulpin`, `level`, `dwelling_group`, `owner`, `z_min/z_max`, and registration status.
6. **Zero-Configuration / Offline Capable**: Operates with a flat ellipsoid globe without requiring a paid Cesium Ion access token.

---

## How to Serve

From the project root:

```bash
# Start HTTP server on port 8080 serving the viewer directory
python -m http.server 8080 --directory viewer
```

Or change into the directory:

```bash
cd viewer
python -m http.server 8080
```

Then open your browser at:
[http://localhost:8080](http://localhost:8080)

---

## Backend URL Configuration

By default, the viewer connects to the local FastAPI backend at:
`http://localhost:8000`

To customize the backend endpoint, edit the constant at the top of [`viewer/app.js`](file:///Users/koushik/ulpin-prototype/viewer/app.js):

```javascript
// Base URL for the FastAPI backend
const API_BASE_URL = window.API_BASE_URL || "http://localhost:8000";
```

Or set `window.API_BASE_URL = "http://<custom-host>:<port>";` in the browser console or before loading `app.js`.

---

## Prerequisites

- FastAPI backend running (e.g., `uvicorn api.main:app --reload --port 8000`).
- Database seeded with the demo duplex units (`POST /buildings/ingest`).
