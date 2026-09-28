/**
 * 3D ULPIN Vertical Property & Cadastral Viewer
 * Built with CesiumJS & Proj4js
 */

// Base URL for the FastAPI backend (configurable)
const API_BASE_URL = window.API_BASE_URL || "http://localhost:8000";

// Cesium Ion token placeholder (Optional; falls back to flat ellipsoid / open imagery)
Cesium.Ion.defaultAccessToken = window.CESIUM_ION_TOKEN || "";

// Proj4 definitions (UTM Zone 43N Bengaluru / Karnataka default)
proj4.defs("EPSG:32643", "+proj=utm +zone=43 +datum=WGS84 +units=m +no_defs");

// State management
let viewer = null;
let allUnitEntities = [];
let discrepancyEntities = [];
let availableLevels = new Set();
let ownerColorMap = new Map();

// Stable color palette for owners
const OWNER_PALETTE = [
  "#0ea5e9", // Sky blue
  "#10b981", // Emerald green
  "#f59e0b", // Amber
  "#8b5cf6", // Purple
  "#ec4899", // Pink
  "#06b6d4", // Cyan
  "#f97316", // Orange
  "#6366f1", // Indigo
  "#14b8a6", // Teal
  "#84cc16", // Lime
];

/**
 * Deterministically compute a stable hex color from an owner string / ID
 */
function getOwnerColor(owner) {
  if (!owner || owner === "Unknown" || owner === "None") {
    return "#64748b"; // Neutral slate gray
  }
  let hash = 0;
  for (let i = 0; i < owner.length; i++) {
    hash = owner.charCodeAt(i) + ((hash << 5) - hash);
  }
  const index = Math.abs(hash) % OWNER_PALETTE.length;
  return OWNER_PALETTE[index];
}

/**
 * Coordinate converter: converts [x, y] to [lon, lat] in WGS84
 */
function toWgs84(pt) {
  const [x, y] = pt;
  if (Math.abs(x) > 180 || Math.abs(y) > 90) {
    return proj4("EPSG:32643", "EPSG:4326", [x, y]);
  }
  return [x, y];
}

/**
 * Initialize CesiumJS Viewer
 */
function initCesium() {
  viewer = new Cesium.Viewer("cesiumContainer", {
    terrainProvider: new Cesium.EllipsoidTerrainProvider(),
    baseLayerPicker: false,
    geocoder: false,
    homeButton: false,
    infoBox: false,
    selectionIndicator: false,
    sceneModePicker: true,
    navigationHelpButton: false,
    animation: false,
    timeline: false,
    fullscreenButton: true,
  });

  // Enable lighting and depth test against terrain
  viewer.scene.globe.depthTestAgainstTerrain = true;
  viewer.scene.globe.enableLighting = false;

  // Set default view near Bengaluru demo duplex area
  viewer.camera.setView({
    destination: Cesium.Cartesian3.fromDegrees(77.5945, 12.9716, 250),
    orientation: {
      heading: Cesium.Math.toRadians(15.0),
      pitch: Cesium.Math.toRadians(-35.0),
      roll: 0.0,
    },
  });

  // Setup click handler for unit picking
  const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
  handler.setInputAction(function (movement) {
    const pickedObject = viewer.scene.pick(movement.position);
    if (Cesium.defined(pickedObject) && pickedObject.id && pickedObject.id._unitData) {
      fetchUnitDetails(pickedObject.id._unitData.ulpin_3d);
    }
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);

  // Setup UI event listeners
  document.getElementById("levelSelect").addEventListener("change", (e) => {
    filterByLevel(e.target.value);
  });

  document.getElementById("btnRunAsBuilt").addEventListener("click", () => {
    runAsBuiltCheck();
  });

  document.getElementById("btnResetCamera").addEventListener("click", () => {
    if (allUnitEntities.length > 0) {
      viewer.flyTo(allUnitEntities, {
        duration: 1.5,
        offset: new Cesium.HeadingPitchRange(Cesium.Math.toRadians(20), Cesium.Math.toRadians(-30), 80),
      });
    }
  });

  document.getElementById("btnCloseModal").addEventListener("click", () => {
    document.getElementById("unitModal").style.display = "none";
  });

  const dataSourceModal = document.getElementById("dataSourceModal");
  const btnDataSource = document.getElementById("btnDataSource");
  const btnCloseDataSource = document.getElementById("btnCloseDataSource");

  if (btnDataSource && dataSourceModal) {
    btnDataSource.addEventListener("click", () => {
      dataSourceModal.style.display = dataSourceModal.style.display === "block" ? "none" : "block";
    });
  }

  if (btnCloseDataSource && dataSourceModal) {
    btnCloseDataSource.addEventListener("click", () => {
      dataSourceModal.style.display = "none";
    });
  }
}

/**
 * Fetch active units from GET /units and render 3D solids
 */
async function loadUnits() {
  const loading = document.getElementById("loadingIndicator");
  loading.style.display = "block";
  loading.textContent = "Loading spatial units from API...";

  try {
    const response = await fetch(`${API_BASE_URL}/units`);
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }
    const data = await response.json();

    // Clear previous entities
    allUnitEntities.forEach((ent) => viewer.entities.remove(ent));
    allUnitEntities = [];
    availableLevels.clear();
    ownerColorMap.clear();

    const features = data.features || [];
    features.forEach((feature) => {
      const props = feature.properties || {};
      const geom = feature.geometry || {};
      const owner = props.owner || "Unknown";
      const colorHex = getOwnerColor(owner);

      ownerColorMap.set(owner, colorHex);
      if (props.level !== undefined && props.level !== null) {
        availableLevels.add(props.level);
      }

      // Extract footprint coordinates
      let linearRing = [];
      if (geom.type === "Polygon" && geom.coordinates && geom.coordinates.length > 0) {
        linearRing = geom.coordinates[0];
      } else if (geom.type === "MultiPolygon" && geom.coordinates && geom.coordinates[0]) {
        linearRing = geom.coordinates[0][0];
      }

      if (linearRing.length < 3) return;

      const flatCoords = [];
      linearRing.forEach((pt) => {
        const [lon, lat] = toWgs84(pt);
        flatCoords.push(lon, lat);
      });

      const zMin = props.z_min ?? 0;
      const zMax = props.z_max ?? 3;

      const entity = viewer.entities.add({
        name: `ULPIN: ${props.ulpin_3d}`,
        polygon: {
          hierarchy: Cesium.Cartesian3.fromDegreesArray(flatCoords),
          height: zMin,
          extrudedHeight: zMax,
          material: Cesium.Color.fromCssColorString(colorHex).withAlpha(0.85),
          outline: true,
          outlineColor: Cesium.Color.WHITE.withAlpha(0.6),
          outlineWidth: 2,
        },
      });

      entity._unitData = props;
      entity._flatCoords = flatCoords;
      allUnitEntities.push(entity);
    });

    updateLevelDropdown();
    updateLegend();

    if (allUnitEntities.length > 0) {
      viewer.flyTo(allUnitEntities, {
        duration: 2.0,
        offset: new Cesium.HeadingPitchRange(Cesium.Math.toRadians(25), Cesium.Math.toRadians(-28), 90),
      });
    }

    // Now check for discrepancies
    await checkDiscrepancies();
  } catch (err) {
    console.error("Failed to load units:", err);
    loading.textContent = `Error: ${err.message}`;
    return;
  } finally {
    loading.style.display = "none";
  }
}

/**
 * Update floor/level filter dropdown options
 */
function updateLevelDropdown() {
  const select = document.getElementById("levelSelect");
  select.innerHTML = '<option value="all">All Levels</option>';

  const sortedLevels = Array.from(availableLevels).sort((a, b) => a - b);
  sortedLevels.forEach((lvl) => {
    const opt = document.createElement("option");
    opt.value = lvl;
    opt.textContent = `Level ${lvl}`;
    select.appendChild(opt);
  });
}

/**
 * Filter rendered entities by level
 */
function filterByLevel(selectedLevel) {
  allUnitEntities.forEach((ent) => {
    if (selectedLevel === "all") {
      ent.show = true;
    } else {
      ent.show = ent._unitData.level == selectedLevel;
    }
  });
}

/**
 * Update the floating legend UI
 */
function updateLegend() {
  const container = document.getElementById("legendItems");
  container.innerHTML = "";

  ownerColorMap.forEach((color, owner) => {
    const item = document.createElement("div");
    item.className = "legend-item";
    item.innerHTML = `
      <div class="legend-swatch" style="background-color: ${color};"></div>
      <span>${owner}</span>
    `;
    container.appendChild(item);
  });

  // Discrepancy indicator in legend
  const discItem = document.createElement("div");
  discItem.className = "legend-item";
  discItem.innerHTML = `
    <div class="legend-swatch" style="background: rgba(225, 29, 72, 0.8); border: 1px dashed #ffffff;"></div>
    <span>Discrepancy (Extra Storey)</span>
  `;
  container.appendChild(discItem);
}

/**
 * Fetch discrepancy records from GET /discrepancies and highlight non-match items
 */
async function checkDiscrepancies() {
  try {
    const res = await fetch(`${API_BASE_URL}/discrepancies`);
    if (!res.ok) return;
    const discrepancies = await res.json();

    // Clear existing discrepancy highlight entities
    discrepancyEntities.forEach((e) => viewer.entities.remove(e));
    discrepancyEntities = [];

    const banner = document.getElementById("discrepancyBanner");
    const activeDiscrepancy = discrepancies.find((d) => d.class && d.class !== "match");

    if (activeDiscrepancy) {
      banner.style.display = "flex";
      document.getElementById("discrepancyText").textContent =
        `⚠️ Flagged: ${activeDiscrepancy.class} (+${activeDiscrepancy.extra_volume_m3.toFixed(1)} m³) detected`;

      // Find top height of all units to render extra storey overlay
      let maxZ = 0;
      let topCoords = null;

      allUnitEntities.forEach((ent) => {
        if (ent._unitData.z_max > maxZ) {
          maxZ = ent._unitData.z_max;
          topCoords = ent._flatCoords;
        }
      });

      if (topCoords && maxZ > 0) {
        // Approximate extra volume as a highlighted red wireframe / pulsing overlay
        const extraHeight = activeDiscrepancy.class === "extra_storey" ? 3.0 : 1.5;
        const overlay = viewer.entities.add({
          name: "Flagged Discrepancy: Extra Volume",
          polygon: {
            hierarchy: Cesium.Cartesian3.fromDegreesArray(topCoords),
            height: maxZ,
            extrudedHeight: maxZ + extraHeight,
            material: Cesium.Color.RED.withAlpha(0.6),
            outline: true,
            outlineColor: Cesium.Color.WHITE,
            outlineWidth: 3,
          },
        });
        discrepancyEntities.push(overlay);
      }
    } else {
      banner.style.display = "none";
    }
  } catch (e) {
    console.warn("Could not check discrepancies:", e);
  }
}

/**
 * Run as-built check button handler
 */
async function runAsBuiltCheck() {
  const btn = document.getElementById("btnRunAsBuilt");
  btn.disabled = true;
  btn.textContent = "Checking...";

  try {
    // Try POST /discrepancies/run if available, else re-fetch GET /discrepancies
    const runRes = await fetch(`${API_BASE_URL}/discrepancies/run`, { method: "POST" });
    if (!runRes.ok) {
      await checkDiscrepancies();
    } else {
      await checkDiscrepancies();
    }
  } catch (err) {
    await checkDiscrepancies();
  } finally {
    btn.disabled = false;
    btn.textContent = "Run as-built check";
  }
}

/**
 * Fetch and display details for a clicked spatial unit from GET /units/{ulpin_3d}
 */
async function fetchUnitDetails(ulpin_3d) {
  const modal = document.getElementById("unitModal");
  const content = document.getElementById("modalContent");
  modal.style.display = "block";
  content.innerHTML = `<p style="color:#94a3b8;">Loading details for ${ulpin_3d}...</p>`;

  try {
    const res = await fetch(`${API_BASE_URL}/units/${encodeURIComponent(ulpin_3d)}`);
    if (!res.ok) {
      throw new Error(`Unit ${ulpin_3d} not found (HTTP ${res.status})`);
    }
    const data = await res.json();
    const ownerName = data.owner || (data.current_rights && data.current_rights[0]?.party_name) || "Unknown";

    content.innerHTML = `
      <div class="detail-row">
        <div class="detail-label">3D ULPIN</div>
        <div class="ulpin-tag">${data.ulpin_3d}</div>
      </div>
      <div class="detail-row">
        <div class="detail-label">Parent Parcel ULPIN</div>
        <div class="detail-value">${data.parent_ulpin}</div>
      </div>
      <div class="detail-row">
        <div class="detail-label">Level / Storey</div>
        <div class="detail-value">Level ${data.level}</div>
      </div>
      <div class="detail-row">
        <div class="detail-label">Dwelling Group</div>
        <div class="detail-value">${data.dwelling_group || "N/A"}</div>
      </div>
      <div class="detail-row">
        <div class="detail-label">Current Owner</div>
        <div class="detail-value" style="color: #38bdf8; font-weight: 600;">${ownerName}</div>
      </div>
      <div class="detail-row">
        <div class="detail-label">Elevation Range (Z)</div>
        <div class="detail-value">${data.z_min} m – ${data.z_max} m (Datum: ${data.datum_source || "N/A"})</div>
      </div>
      <div class="detail-row">
        <div class="detail-label">Status</div>
        <div class="detail-value">
          <span class="badge" style="background: rgba(16, 185, 129, 0.2); color: #34d399; border-color: rgba(16, 185, 129, 0.3);">
            ${data.status}
          </span>
        </div>
      </div>
    `;
  } catch (err) {
    content.innerHTML = `<p style="color: #f87171;">Failed to load unit details: ${err.message}</p>`;
  }
}

// Start application when DOM is ready
window.addEventListener("DOMContentLoaded", () => {
  initCesium();
  loadUnits();
  loadSourceMetadata();
});

/**
 * Attempt to load data/raw/SOURCES.json if served from repo root or relative path
 */
async function loadSourceMetadata() {
  const possiblePaths = ["data/raw/SOURCES.json", "../data/raw/SOURCES.json"];
  for (const path of possiblePaths) {
    try {
      const res = await fetch(path);
      if (res.ok) {
        const data = await res.json();
        if (data.filename) {
          const fnEl = document.getElementById("srcFilename");
          if (fnEl) fnEl.textContent = data.filename;
        }
        if (data.source_url) {
          const urlEl = document.getElementById("srcUrl");
          if (urlEl) {
            urlEl.href = data.source_url;
            urlEl.textContent = data.source_url;
          }
        }
        if (data.license) {
          const licEl = document.getElementById("srcLicense");
          if (licEl) licEl.textContent = data.license;
        }
        if (data.sha256) {
          const shaEl = document.getElementById("srcSha256");
          if (shaEl) shaEl.textContent = data.sha256;
        }
        break;
      }
    } catch (_) {
      // Fallback values are already hardcoded in DOM
    }
  }
}
