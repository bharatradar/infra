// --- Frontend Config (loaded from /api/config) ---
let FRONTEND_CONFIG = {
    radar_poll_interval_ms: 2000,
    atc_poll_interval_ms: 5000,
    ops_poll_interval_ms: 30000,
    exec_poll_interval_ms: 60000,
    ws_reconnect_delay_ms: 3000,
    ws_enabled: false,
    ws_use_for_radar: false,
    ws_use_for_atc: false
};

// Load config from backend on startup
async function loadFrontendConfig() {
    try {
        const res = await fetch('/api/config');
        if (res.ok) {
            const cfg = await res.json();
            FRONTEND_CONFIG = { ...FRONTEND_CONFIG, ...cfg };
            console.log('[Config] Loaded frontend config:', FRONTEND_CONFIG);
        }
    } catch (e) {
        console.warn('[Config] Failed to load config, using defaults:', e);
    }
}

// --- WebSocket Configuration ---
// Values are overridden by FRONTEND_CONFIG from backend
let WS_ENABLED = FRONTEND_CONFIG.ws_enabled;
let WS_USE_FOR_RADAR = FRONTEND_CONFIG.ws_use_for_radar;
let WS_USE_FOR_ATC = FRONTEND_CONFIG.ws_use_for_atc;
const WS_URL = 'ws://localhost:8002';

// Store flights from WebSocket for shared use
let wsFlightsData = [];
let wsReconnectAttempts = 0;
const WS_MAX_RECONNECT = 5;

// --- Map viewport tracking (for dynamic radar radius) ---
let mapCenter = { lat: 20.5937, lon: 78.9629 };  // Default: center of India
let mapRadius = 1500;  // Default: 1500 miles (covers most of India)

function getRadiusFromZoom(zoom) {
    if (!zoom || zoom < 3) return 2500;
    if (zoom > 15) return 10;
    return Math.max(10, Math.min(2500, 1500 * Math.pow(2, 5 - zoom)));
}

function updateMapViewport() {
    if (!map) return;
    const view = map.getView();
    const center = ol.proj.toLonLat(view.getCenter());
    const zoom = view.getZoom() || 5;
    mapCenter = { lat: center[1], lon: center[0] };
    mapRadius = getRadiusFromZoom(zoom);
}

function initWebSocket() {
    if (!WS_ENABLED) {
        console.log('[WS] WebSocket disabled, using REST API');
        return;
    }
    
    console.log('[WS] Attempting to connect to:', WS_URL);
    
    try {
        ws = new WebSocket(WS_URL);
        
        ws.onopen = () => {
            console.log('WebSocket connected');
            wsReconnectAttempts = 0;
            // Request full flight list on connect
            ws.send(JSON.stringify({ action: 'get_all' }));
        };
        
        ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                handleWebSocketMessage(msg);
            } catch (e) {
                console.warn('WS message parse error:', e);
            }
        };
        
        ws.onclose = (e) => {
            console.log('WebSocket closed:', e.code, e.reason);
            // Don't reconnect if we're falling back to REST
            if (ws && typeof wsReconnectAttempts !== 'undefined' && wsReconnectAttempts < WS_MAX_RECONNECT && !fetchRadarData.restarted) {
                wsReconnectAttempts++;
                setTimeout(initWebSocket, FRONTEND_CONFIG.ws_reconnect_delay_ms * wsReconnectAttempts);
            }
        };
        
        ws.onerror = (e) => {
        console.error('[WS] WebSocket error:', e);
        console.warn('[WS] Falling back to REST API');
        // Fall back to REST if WebSocket fails
        if (!fetchRadarData.restarted) {
            fetchRadarData.restarted = true;
            fetchRadarData();
            if (!fetchATCTimer) {
            fetchATCTimer = setInterval(fetchATC, FRONTEND_CONFIG.atc_poll_interval_ms);
            }
        }
        ws = null;
    };
    } catch (e) {
        console.warn('WebSocket init failed:', e);
    }
}

function handleWebSocketMessage(msg) {
    if (msg.type === 'connected') {
        console.log('WS: Connected:', msg.message);
    } else if (msg.type === 'flight_snapshot') {
        // Initial flight data
        console.log('WS: Flight snapshot:', msg.count, 'flights');
        // Store for ATC stats if enabled
        if (WS_USE_FOR_ATC) {
            wsFlightsData = msg.flights || [];
        }
        processFlightData(msg.flights || []);
    } else if (msg.type === 'flight_update') {
        // Real-time update - add/update single flight
        if (msg.data) {
            updateSingleFlight(msg.data);
            // Update stored data
            if (WS_USE_FOR_ATC) {
                const idx = wsFlightsData.findIndex(f => f.hexid === msg.data.hexid);
                if (idx >= 0) {
                    wsFlightsData[idx] = msg.data;
                } else {
                    wsFlightsData.push(msg.data);
                }
            }
        }
    } else if (msg.type === 'aircraft_data') {
        // Response to get_aircraft
        console.log('WS: Aircraft data:', msg.data);
    }
}

function fixLatLon(ac) {
    const lat = parseFloat(ac.lat);
    const lon = parseFloat(ac.lon);
    if (!isNaN(lat) && !isNaN(lon) && Math.abs(lat) > 60 && Math.abs(lon) < 60) {
        ac.lat = lon;
        ac.lon = lat;
    }
}

function processFlightData(flights) {
    const newAircraft = {};
    flights.forEach(ac => {
        fixLatLon(ac);
        if (ac.lat && ac.lon) {
            newAircraft[ac.hexid] = {
                hex: ac.hexid,
                callsign: ac.callsign,
                lat: ac.lat,
                lon: ac.lon,
                alt: ac.alt || 0,
                gs: ac.speed || 0,
                heading: ac.heading || 0,
                origin: ac.origin || '',
                destination: ac.destination || ''
            };
        }
    });

    radarAircraft = newAircraft;

    const countEl = document.getElementById('radar-count');
    if (countEl) countEl.textContent = flights.length;

    if (!radarCtx) initRadarCanvas();
    if (radarCtx) updateRadarCanvas();
    updateRadarList();
}

function updateSingleFlight(flight) {
    fixLatLon(flight);
    if (!flight.lat || !flight.lon) return;

    radarAircraft[flight.hexid] = {
        hex: flight.hexid,
        callsign: flight.callsign,
        lat: flight.lat,
        lon: flight.lon,
        alt: flight.alt || 0,
        gs: flight.speed || 0,
        heading: flight.heading || 0,
        origin: flight.origin || '',
        destination: flight.destination || ''
    };

    if (!radarCtx) initRadarCanvas();
    if (radarCtx) updateRadarCanvas();
}

// --- 1. UI & TABS ---
let sidebarOpen = false;
let terminalModes = { arr: 'board', dep: 'board' }; 

// 🌟 Global Table Sorting Engine
let sortDirections = {}; 

function sortTable(tbodyId, colIndex, type) {
    const tbody = document.getElementById(tbodyId);
    if (!tbody) return;
    const rows = Array.from(tbody.querySelectorAll('tr'));
    
    if (rows.length === 1 && rows[0].cells.length === 1) return;

    const sortKey = `${tbodyId}-${colIndex}`;
    sortDirections[sortKey] = !sortDirections[sortKey];
    const isAsc = sortDirections[sortKey];

    const table = tbody.parentElement;
    const headers = table.querySelectorAll('th');
    headers.forEach((th, idx) => {
        const icon = th.querySelector('.sort-icon');
        if (icon) {
            icon.className = 'fa-solid ml-1 sort-icon text-gray-600 ' + 
                (idx === colIndex ? (isAsc ? 'fa-sort-up text-blue-400' : 'fa-sort-down text-blue-400') : 'fa-sort');
        }
    });

    rows.sort((a, b) => {
        let cellA = a.cells[colIndex].textContent.trim();
        let cellB = b.cells[colIndex].textContent.trim();

        if (type === 'numeric') {
            let numA = parseFloat(cellA.replace(/[^0-9.-]+/g,""));
            let numB = parseFloat(cellB.replace(/[^0-9.-]+/g,""));
            if(isNaN(numA)) numA = 0;
            if(isNaN(numB)) numB = 0;
            return isAsc ? numA - numB : numB - numA;
        } else {
            return isAsc ? cellA.localeCompare(cellB) : cellB.localeCompare(cellA);
        }
    });

    rows.forEach(row => tbody.appendChild(row));
}

function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebar-overlay');
    sidebarOpen = !sidebarOpen;
    if (sidebarOpen) {
        sidebar.classList.remove('-translate-x-full');
        overlay.classList.remove('hidden');
    } else {
        sidebar.classList.add('-translate-x-full');
        overlay.classList.add('hidden');
    }
}

const views = ['atc', 'ops', 'exec', 'ai', 'arr', 'dep'];
let currentTab = 'atc';
let tabTimers = {};

function switchTab(target) {
    views.forEach(v => {
        const viewEl = document.getElementById(`view-${v}`);
        const navEl = document.getElementById(`nav-${v}`);
        if(viewEl) viewEl.classList.add('hidden');
        if(navEl) navEl.classList.remove('nav-active', 'border-l-4', 'border-blue-500', 'bg-gray-800', 'text-white');
    });
    
    const targetView = document.getElementById(`view-${target}`);
    const targetNav = document.getElementById(`nav-${target}`);
    if(targetView) targetView.classList.remove('hidden');
    if(targetNav) targetNav.classList.add('nav-active', 'border-l-4', 'border-blue-500', 'bg-gray-800', 'text-white');
    
    // Stop all tab timers
    Object.values(tabTimers).forEach(t => clearInterval(t));
    tabTimers = {};
    
    currentTab = target;
    
    // Start polling for active tab only
    if (target === 'atc') {
        fetchATC();
        tabTimers.atc = setInterval(fetchATC, FRONTEND_CONFIG.atc_poll_interval_ms);
    } else if (target === 'ops') {
        fetchOps();
        tabTimers.ops = setInterval(fetchOps, FRONTEND_CONFIG.ops_poll_interval_ms);
    } else if (target === 'exec') {
        fetchExec();
        tabTimers.exec = setInterval(fetchExec, FRONTEND_CONFIG.exec_poll_interval_ms);
    } else if (target === 'ai') {
        fetchAIOperations();
    } else if (target === 'arr' || target === 'dep') {
        fetchSchedules();
    }
    
    if(target === 'atc' && map) setTimeout(() => map.invalidateSize(), 200);
    if (window.innerWidth < 768 && sidebarOpen) toggleSidebar();
}

function getFilters() {
    return { airport: document.getElementById('filter-airport').value, airline: document.getElementById('filter-airline').value };
}

async function loadFilterOptions() {
    try {
        const res = await fetch('/api/filters');
        const data = await res.json();
        
        data.airports.sort((a, b) => String(a.display).localeCompare(String(b.display), undefined, { sensitivity: 'base' }));
        data.airlines.sort((a, b) => String(a.display).localeCompare(String(b.display), undefined, { sensitivity: 'base' }));
        
        // Store coordinates for airport zoom
        airportCoords = {};
        data.airports.forEach(ap => {
            if (ap.lat && ap.lon) {
                airportCoords[ap.code] = { lat: ap.lat, lon: ap.lon };
            }
        });
        
        const apSelect = document.getElementById('filter-airport');
        apSelect.innerHTML = '<option value="ALL">🌐 ALL Airports</option>';
        data.airports.forEach(ap => apSelect.add(new Option(ap.display, ap.code)));
        
        const alSelect = document.getElementById('filter-airline');
        alSelect.innerHTML = '<option value="ALL">✈️ ALL Airlines</option>';
        data.airlines.forEach(al => alSelect.add(new Option(al.display, al.code)));
    } catch(e) { console.error("Filter Load Error:", e); }
}

function applyFilters() {
    fetchATC();
    fetchOps();
    fetchExec();
    fetchSchedules();
    handleAirportZoom();
}

// Store airport coordinates for map zooming
let airportCoords = {};

function handleAirportZoom() {
    const airport = document.getElementById('filter-airport').value;
    if (airport === 'ALL') {
        // Zoom out to India view
        if (map) {
            map.getView().animate({
                center: ol.proj.fromLonLat([78.9629, 20.5937]),
                zoom: 5,
                duration: 1000
            });
        }
    } else if (airportCoords[airport]) {
        // Zoom to specific airport
        const coords = airportCoords[airport];
        if (map) {
            map.getView().animate({
                center: ol.proj.fromLonLat([coords.lon, coords.lat]),
                zoom: 10,
                duration: 1000
            });
        }
        // Also update fullscreen map if open
        if (fullscreenMap && radarFullscreen) {
            fullscreenMap.getView().animate({
                center: ol.proj.fromLonLat([coords.lon, coords.lat]),
                zoom: 10,
                duration: 1000
            });
        }
    }
}

// --- 2. MAP SETUP (OpenLayers - tar1090 Style) ---
let map = null;
let markers = {};
let heatLayerGroup = null;
let olAircraftLayer = null;
let olMapInitialized = false;
let mapDimOverlay = null;
let isMapDimmed = false;

// tar1090 Aircraft Shapes Catalog
const TAR1090_SHAPES = {
    airliner: {
        w: 20, h: 28,
        viewBox: '-10 -10 40 50',
        strokeScale: 16,
        path: 'M10,0 L10,4 L18,8 L18,10 L12,10 L12,16 L16,18 L16,20 L10,18 L10,26 L12,28 L12,30 L8,28 L8,20 L2,22 L2,20 L8,18 L8,10 L2,10 L2,8 L8,4 L8,0 Z'
    },
    unknown: {
        w: 20, h: 28,
        viewBox: '-10 -10 40 50',
        strokeScale: 16,
        path: 'M10,0 L10,4 L18,8 L18,10 L12,10 L12,16 L16,18 L16,20 L10,18 L10,26 L12,28 L12,30 L8,28 L8,20 L2,22 L2,20 L8,18 L8,10 L2,10 L2,8 L8,4 L8,0 Z'
    }
};

// tar1090 Altitude Color System (HSL)
const ColorByAlt = {
    unknown: { h: 0, s: 0, l: 75 },
    ground: { h: 220, s: 0, l: 30 },
    air: {
        h: [
            { alt: 0, val: 20 }, { alt: 2000, val: 32.5 }, { alt: 4000, val: 43 },
            { alt: 6000, val: 54 }, { alt: 8000, val: 72 }, { alt: 9000, val: 85 },
            { alt: 11000, val: 140 }, { alt: 40000, val: 300 }, { alt: 51000, val: 360 }
        ],
        s: 88,
        l: [
            { h: 0, val: 53 }, { h: 20, val: 50 }, { h: 32, val: 54 }, { h: 40, val: 52 },
            { h: 46, val: 51 }, { h: 50, val: 46 }, { h: 60, val: 43 }, { h: 80, val: 41 },
            { h: 100, val: 41 }, { h: 120, val: 41 }, { h: 140, val: 41 }, { h: 160, val: 40 },
            { h: 180, val: 40 }, { h: 190, val: 44 }, { h: 198, val: 50 }, { h: 200, val: 58 },
            { h: 220, val: 58 }, { h: 240, val: 58 }, { h: 255, val: 55 }, { h: 266, val: 55 },
            { h: 270, val: 58 }, { h: 280, val: 58 }, { h: 290, val: 47 }, { h: 300, val: 43 },
            { h: 310, val: 48 }, { h: 320, val: 48 }, { h: 340, val: 52 }, { h: 360, val: 53 }
        ]
    }
};

// Data source colors (RGB)
const colorBySource = {
    adsb: 'rgb(42, 83, 99)',
    uat: 'rgb(41, 95, 62)',
    mlat: 'rgb(96, 87, 46)',
    tisb: 'rgb(99, 42, 63)',
    modeS: 'rgb(42, 42, 99)',
    adsc: 'rgb(39, 83, 39)'
};

function interpolateValue(value, stops, key = 'alt', valKey = 'val') {
    if (!stops || stops.length === 0) return 0;
    if (value <= stops[0][key]) return stops[0][valKey];
    if (value >= stops[stops.length - 1][key]) return stops[stops.length - 1][valKey];
    for (let i = 0; i < stops.length - 1; i++) {
        if (value >= stops[i][key] && value <= stops[i + 1][key]) {
            const t = (value - stops[i][key]) / (stops[i + 1][key] - stops[i][key]);
            return stops[i][valKey] + t * (stops[i + 1][valKey] - stops[i][valKey]);
        }
    }
    return stops[stops.length - 1][valKey];
}

function hslToRgb(h, s, l) {
    s /= 100; l /= 100;
    const k = n => (n + h / 30) % 12;
    const a = s * Math.min(l, 1 - l);
    const f = n => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
    return [
        Math.round(255 * f(0)),
        Math.round(255 * f(8)),
        Math.round(255 * f(4))
    ];
}

function getAltitudeColor(altitude) {
    if (altitude === null || altitude === undefined) {
        const c = ColorByAlt.unknown;
        const [r, g, b] = hslToRgb(c.h, c.s, c.l);
        return `rgb(${r},${g},${b})`;
    }
    if (altitude === 'ground') {
        const c = ColorByAlt.ground;
        const [r, g, b] = hslToRgb(c.h, c.s, c.l);
        return `rgb(${r},${g},${b})`;
    }
    const h = interpolateValue(altitude, ColorByAlt.air.h);
    const s = ColorByAlt.air.s;
    const l = interpolateValue(h, ColorByAlt.air.l, 'h', 'val');
    const [r, g, b] = hslToRgb(h, s, l);
    return `rgb(${r},${g},${b})`;
}

function createPlaneIconUrl(shape, fillColor, strokeColor, strokeWidth, scale) {
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${shape.w * scale}" height="${shape.h * scale}" viewBox="${shape.viewBox}">
        <g transform="scale(${scale / shape.strokeScale})">
            <path fill="${fillColor}" stroke="${strokeColor}" stroke-width="${strokeWidth}" d="${shape.path}"/>
        </g>
    </svg>`;
    return 'data:image/svg+xml;base64,' + btoa(svg);
}

let markerZoomDivide = 8.5;
let markerSmall = 1.0;
let markerBig = 1.18;
let iconScale = 1;

function getMarkerScale(zoom) {
    return (zoom < markerZoomDivide ? markerSmall : markerBig) * iconScale;
}

// ---------------------------------------------------------------------
// 🛩️ AIRCRAFT TYPE DATABASE & SHAPE SYSTEM (uses tar1090 markers)
// ---------------------------------------------------------------------
let aircraftDB = {};  // { hex: { reg, type, flags, desc } }
let aircraftDBReady = false;

async function loadAircraftDB() {
    try {
        const res = await fetch('/api/aircraft-db?hex_id=000000');
        if (!res.ok) return;
        const data = await res.json();
        if (data.status === 'success' || data.status === 'not_found') {
            aircraftDBReady = true;
            console.log('[AircraftDB] Backend database ready');
        }
    } catch (e) {
        console.warn('[AircraftDB] Failed to check DB status:', e);
    }
}

async function fetchAircraftTypes(hexIds) {
    if (!aircraftDBReady || !hexIds || hexIds.length === 0) return {};
    const missing = hexIds.filter(h => !aircraftDB[h]);
    if (missing.length === 0) return aircraftDB;
    try {
        const batch = missing.slice(0, 200).join(',');
        const res = await fetch(`/api/aircraft-db?batch=${batch}`);
        const data = await res.json();
        if (data.status === 'success' && data.data) {
            Object.assign(aircraftDB, data.data);
        }
    } catch (e) {
        console.warn('[AircraftDB] Batch fetch failed:', e);
    }
    return aircraftDB;
}

function getAircraftType(hex) {
    const entry = aircraftDB[hex];
    return entry ? entry.type : null;
}

// Cache for aircraft styles to avoid recreating them every frame
const _styleCache = new Map();

function getAircraftStyle(alt, rotation, zoom, isSelected, typeCode, hex) {
    try {
        const color = getAltitudeColor(alt);
        const zoomScale = zoom < markerZoomDivide ? markerSmall : markerBig;
        const baseScale = iconScale * zoomScale * 0.96;
        const selectMult = isSelected ? 1.3 : 1;
        const finalScale = baseScale * selectMult;

        // Debug: log availability of tar1090 globals on first call
        if (!window._tar1090DebugLogged) {
            window._tar1090DebugLogged = true;
            console.log('[tar1090] shapes loaded:', typeof shapes !== 'undefined' ? Object.keys(shapes).length : 'NO');
            console.log('[tar1090] getBaseMarker:', typeof getBaseMarker);
            console.log('[tar1090] svgShapeToURI:', typeof svgShapeToURI);
        }

        // Use tar1090 getBaseMarker if available
        let shapeName = 'unknown';
        let shapeScale = 1;
        if (typeof getBaseMarker === 'function') {
            const dbEntry = hex && aircraftDB[hex];
            const icaoType = typeCode || (dbEntry ? dbEntry.type : '');
            const result = getBaseMarker('', icaoType, '', '', 'adsb_icao', alt, false);
            shapeName = result[0];
            shapeScale = result[1];
        }

        if (typeof shapes === 'undefined') {
            console.warn('[tar1090] shapes object not loaded, using triangle fallback');
            return new ol.style.Style({
                image: new ol.style.RegularShape({
                    fill: new ol.style.Fill({ color: color }),
                    stroke: new ol.style.Stroke({ color: 'rgba(0,0,0,0.6)', width: 1.5 }),
                    points: 3, radius: 10 * finalScale, radius2: 0,
                    rotation: rotation, rotateWithView: true

                })
            });
        }

        const shape = shapes[shapeName] || shapes['unknown'];
        if (!shape) {
            console.warn('[tar1090] shape not found:', shapeName);
            return new ol.style.Style({
                image: new ol.style.RegularShape({
                    fill: new ol.style.Fill({ color: color }),
                    stroke: new ol.style.Stroke({ color: 'rgba(0,0,0,0.6)', width: 1.5 }),
                    points: 3, radius: 10 * finalScale, radius2: 0,
                    rotation: rotation, rotateWithView: true
                })
            });
        }

        const noRotate = shape.noRotate || false;
        const rotRad = noRotate ? 0 : rotation;
        const cacheKey = `${color}_${shapeName}_${finalScale.toFixed(2)}_${rotRad.toFixed(3)}_${isSelected}`;
        if (_styleCache.has(cacheKey)) {
            return _styleCache.get(cacheKey);
        }
        if (_styleCache.size > 2000) {
            _styleCache.clear();
        }

        // Generate SVG data URI using tar1090's svgShapeToURI
        let svgSrc;
        if (typeof svgShapeToURI === 'function') {
            svgSrc = svgShapeToURI(shape, color, 'rgba(0,0,0,0.6)', 0.7, finalScale * shapeScale);
        } else {
            console.warn('[tar1090] svgShapeToURI not found, using triangle fallback');
            return new ol.style.Style({
                image: new ol.style.RegularShape({
                    fill: new ol.style.Fill({ color: color }),
                    stroke: new ol.style.Stroke({ color: 'rgba(0,0,0,0.6)', width: 1.5 }),
                    points: 3, radius: 10 * finalScale, radius2: 0,
                    rotation: rotRad, rotateWithView: !noRotate
                })
            });
        }

        const style = new ol.style.Style({
            image: new ol.style.Icon({
                src: svgSrc,
                rotation: rotRad,
                rotateWithView: !noRotate,
                anchor: [0.5, 0.5],
                anchorXUnits: 'fraction',
                anchorYUnits: 'fraction'
            })
        });
        _styleCache.set(cacheKey, style);
        return style;
    } catch (e) {
        console.warn('Style error, using fallback:', e);
        return new ol.style.Style({
            image: new ol.style.Circle({
                radius: 6,
                fill: new ol.style.Fill({ color: '#ff0000' }),
                stroke: new ol.style.Stroke({ color: '#000', width: 1 })
            })
        });
    }
}

// Base map layers
const baseMapLayers = {};

async function initMainMap() {
    const canvasContainer = document.getElementById('atc-map-container');
    if (!canvasContainer) {
        console.warn('Radar container not found');
        return;
    }
    if (typeof ol === 'undefined') {
        setTimeout(initMainMap, 500);
        return;
    }

    try {
        const view = new ol.View({
            center: ol.proj.fromLonLat([78.9629, 20.5937]),
            zoom: 5,
            minZoom: 2,
            maxZoom: 15
        });

        // Create base layers
        baseMapLayers.osm = new ol.layer.Tile({
            source: new ol.source.OSM({ maxZoom: 17, attributionsCollapsible: false, transition: 250 }),
            name: 'osm', title: 'OpenStreetMap', type: 'base', visible: false
        });
        baseMapLayers.carto_voyager = new ol.layer.Tile({
            source: new ol.source.OSM({
                url: 'https://{a-d}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png',
                attributions: 'Powered by <a href="https://carto.com">CARTO.com</a>',
                maxZoom: 15, transition: 250
            }),
            name: 'carto_voyager', title: 'CARTO.com English', type: 'base', visible: false
        });
        baseMapLayers.carto_dark = new ol.layer.Tile({
            source: new ol.source.XYZ({
                url: 'https://{a-d}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
                maxZoom: 19, transition: 250
            }),
            name: 'carto_dark', title: 'CARTO Dark', type: 'base', visible: true
        });
        baseMapLayers.esri_satellite = new ol.layer.Tile({
            source: new ol.source.XYZ({
                url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
                maxZoom: 18, transition: 250
            }),
            name: 'esri_satellite', title: 'ESRI.com Sat.', type: 'base', visible: false
        });
        baseMapLayers.openfreemap_bright = new ol.layer.Tile({
            source: new ol.source.OSM({
                url: 'https://tiles.openfreemap.org/styles/bright/{z}/{x}/{y}.png',
                maxZoom: 18, transition: 250
            }),
            name: 'openfreemap_bright', title: 'OpenFreeMap Bright', type: 'base', visible: false
        });

        map = new ol.Map({
            target: canvasContainer,
            view: view,
            layers: [
                baseMapLayers.carto_dark,
                baseMapLayers.osm,
                baseMapLayers.carto_voyager,
                baseMapLayers.esri_satellite,
                baseMapLayers.openfreemap_bright
            ],
            controls: ol.control.defaults.defaults({ zoom: false, rotate: false }).extend([
                new ol.control.Zoom({ className: 'ol-zoom hidden' }),
                new ol.control.ScaleLine()
            ])
        });

        // Aircraft vector layer
        olAircraftLayer = new ol.layer.Vector({
            source: new ol.source.Vector(),
            style: (feature) => {
                const alt = feature.get('alt') || 0;
                const rotation = feature.get('rotation') || 0;
                const zoom = map.getView().getZoom() || 5;
                const typeCode = feature.get('typeCode') || '';
                const hex = feature.get('hexid') || '';
                return getAircraftStyle(alt, rotation, zoom, feature.get('selected') || false, typeCode, hex);
            }
        });
        map.addLayer(olAircraftLayer);

        // Map dimming overlay
        mapDimOverlay = document.createElement('div');
        mapDimOverlay.className = 'absolute inset-0 pointer-events-none z-[5] transition-opacity duration-300 opacity-0';
        mapDimOverlay.style.backgroundColor = 'rgba(0,0,0,0.45)';
        canvasContainer.appendChild(mapDimOverlay);

        // Hit detection
        map.on('pointermove', function(evt) {
            const features = map.getFeaturesAtPixel(evt.pixel);
            const popup = document.getElementById('radar-popup');
            if (features && features.length > 0) {
                const f = features[0];
                const props = f.getProperties();
                if (props.callsign !== undefined) {
                    showAircraftPopup(props, evt.pixel);
                }
            } else {
                popup.classList.add('hidden');
            }
        });

        map.on('click', function(evt) {
            const features = map.getFeaturesAtPixel(evt.pixel);
            if (features && features.length > 0) {
                const f = features[0];
                const props = f.getProperties();
                showAircraftPopup(props, evt.pixel);
                selectAircraftInList(props.hexid);
            }
        });

        map.on('moveend', function() {
            updateMapViewport();
        });

        // Initialize viewport tracking
        setTimeout(updateMapViewport, 100);

        olMapInitialized = true;
        console.log('OpenLayers map initialized (tar1090 style)');
        fetchATC();
        // Start animation loop
        if (!animationFrameId) {
            animationFrameId = requestAnimationFrame(interpolateAircraftPositions);
        }
    } catch(e) {
        console.error('OL init failed:', e);
    }
    return Promise.resolve();
}

function initLeafletMap() {
    return new Promise((resolve) => {
        const checkL = () => {
            if (typeof L !== 'undefined') {
                map = L.map('map', { zoomControl: false }).setView([20.5937, 78.9629], 5);
                L.control.zoom({ position: 'topright' }).addTo(map);
                L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', { maxZoom: 19 }).addTo(map);
                heatLayerGroup = L.layerGroup().addTo(map);
                resolve();
            } else {
                setTimeout(checkL, 100);
            }
        };
        checkL();
    });
}

function switchMapLayer(layerName) {
    Object.values(baseMapLayers).forEach(l => {
        l.setVisible(l.get('name') === layerName);
    });
    document.querySelectorAll('.layer-btn').forEach(btn => {
        if (btn.dataset.layer === layerName) {
            btn.classList.add('bg-gray-700');
        } else {
            btn.classList.remove('bg-gray-700');
        }
    });
}

function toggleMapDimming() {
    isMapDimmed = !isMapDimmed;
    const btn = document.getElementById('btn-dim-map');
    if (mapDimOverlay) {
        mapDimOverlay.style.opacity = isMapDimmed ? '1' : '0';
    }
    if (btn) {
        btn.innerHTML = isMapDimmed
            ? '<i class="fa-solid fa-sun mr-1"></i> Bright Map'
            : '<i class="fa-solid fa-moon mr-1"></i> Dim Map';
        btn.classList.toggle('bg-yellow-600', isMapDimmed);
        btn.classList.toggle('bg-gray-900/90', !isMapDimmed);
    }
}

function updateMapDimming() {
    if (mapDimOverlay) {
        mapDimOverlay.style.opacity = isMapDimmed ? '1' : '0';
    }
}

let olFeatureCache = {};
let pendingUpdate = false;
let animationFrameId = null;
const aircraftState = new Map(); // hex => {lat, lon, heading, speed, timestamp}
const aircraftTarget = new Map(); // hex => {lat, lon, heading, speed, timestamp} - API target for smooth transition
const TRANSITION_SPEED = 0.15; // 15% per frame = ~300ms to settle

// Smooth interpolation function
function lerp(start, end, t) {
    return start + (end - start) * t;
}

// Interpolate aircraft positions continuously with velocity extrapolation
function interpolateAircraftPositions() {
    if (!olAircraftLayer || !map) {
        if (animationFrameId) {
            cancelAnimationFrame(animationFrameId);
            animationFrameId = null;
        }
        return;
    }
    
    try {
        const source = olAircraftLayer.getSource();
        const features = source.getFeatures();
        const now = Date.now();
        
        features.forEach(feature => {
            const hex = feature.get('hexid');
            if (!hex) return;
            
            const state = aircraftState.get(hex);
            const target = aircraftTarget.get(hex);
            
            if (!state || !target) return;
            
            // --- Velocity Extrapolation ---
            // Between API polls, predict position using heading + speed
            // so aircraft glide smoothly along their flight path
            const dtSinceUpdate = (now - target.timestamp) / 1000;
            
            if (dtSinceUpdate > 0.5) {
                const speedKt = target.speed || state.speed || 0;
                const headingDeg = target.heading || state.heading || 0;
                
                if (speedKt > 5) {  // Only extrapolate if moving meaningfully
                    const headingRad = headingDeg * Math.PI / 180;
                    const lastExtrapTs = target._lastExtrapolated || target.timestamp;
                    const extrapDt = (now - lastExtrapTs) / 1000;
                    
                    if (extrapDt > 0) {
                        // Distance traveled in nautical miles
                        const distNm = (speedKt * extrapDt) / 3600;
                        const dLat = distNm * Math.cos(headingRad) / 60;
                        const latRad = target.lat * Math.PI / 180;
                        const lonFactor = Math.cos(latRad);
                        const dLon = lonFactor > 0.01
                            ? distNm * Math.sin(headingRad) / (lonFactor * 60)
                            : 0;
                        
                        target.lat += dLat;
                        target.lon += dLon;
                        target._lastExtrapolated = now;
                    }
                }
            }
            
            // Smoothly interpolate position towards (extrapolated) target
            const lat = lerp(state.lat, target.lat, TRANSITION_SPEED);
            const lon = lerp(state.lon, target.lon, TRANSITION_SPEED);
            const heading = lerp(state.heading, target.heading, TRANSITION_SPEED);
            const speed = lerp(state.speed, target.speed, TRANSITION_SPEED);
            
            // Update feature
            const coord = ol.proj.fromLonLat([lon, lat]);
            feature.getGeometry().setCoordinates(coord);
            feature.set('rotation', heading * Math.PI / 180);
            feature.set('alt', state.alt);
            feature.set('speed', speed);
            feature.set('heading', heading);
            
            // Update internal state
            aircraftState.set(hex, {
                lat, lon, heading, speed, 
                alt: state.alt,
                timestamp: now
            });
        });
        
        // Refresh layer to show updated positions
        olAircraftLayer.changed();
        
        // Continue animation loop
        animationFrameId = requestAnimationFrame(interpolateAircraftPositions);
    } catch (e) {
        console.error('[interpolateAircraftPositions] Error:', e);
        if (animationFrameId) {
            cancelAnimationFrame(animationFrameId);
            animationFrameId = null;
        }
    }
}

function updateOLAircraft(flights) {
    if (!olAircraftLayer || !map) {
        console.warn('[updateOLAircraft] Layer or map not ready', { olAircraftLayer, map });
        return;
    }
    if (pendingUpdate) return;
    pendingUpdate = true;

    // Fetch aircraft types in background
    const hexIds = flights.map(fl => (fl.hexid || fl.hex || '')).filter(h => h);
    fetchAircraftTypes(hexIds).then(() => {
        requestAnimationFrame(() => {
            try {
                const currentHexIds = new Set();
                const source = olAircraftLayer.getSource();
                let added = 0, updated = 0, removed = 0;

                flights.forEach(fl => {
                    fixLatLon(fl);
                    if (!fl.lat || !fl.lon) return;
                    const hex = fl.hexid || fl.hex;
                    if (!hex) return;
                    currentHexIds.add(hex);

                    const lat = parseFloat(fl.lat);
                    const lon = parseFloat(fl.lon);
                    const heading = parseFloat(fl.heading) || 0;
                    const speed = parseFloat(fl.speed) || 0;
                    const alt = parseFloat(fl.alt) || 0;
                    const typeCode = (aircraftDB[hex] && aircraftDB[hex].type) || fl.ac_type || '';
                    
                    // Store API target for smooth transition
                    aircraftTarget.set(hex, { lat, lon, heading, speed, timestamp: Date.now(), _lastExtrapolated: Date.now() });

                    const coord = ol.proj.fromLonLat([lon, lat]);
                    const rotation = heading * Math.PI / 180;

                    let feature = olFeatureCache[hex];

                    if (feature) {
                        // Update existing feature - don't jump position, let interpolation handle it
                        feature.set('alt', alt);
                        feature.set('speed', speed);
                        feature.set('rotation', rotation);
                        feature.set('origin', fl.origin || '');
                        feature.set('destination', fl.destination || '');
                        feature.set('typeCode', typeCode);
                        updated++;
                    } else {
                        feature = new ol.Feature({
                            geometry: new ol.geom.Point(coord),
                            hexid: hex,
                            callsign: fl.callsign || '',
                            reg: fl.reg || '',
                            type: fl.ac_type || '',
                            typeCode: typeCode,
                            alt: alt,
                            speed: speed,
                            rotation: rotation,
                            origin: fl.origin || '',
                            destination: fl.destination || ''
                        });
                        source.addFeature(feature);
                        olFeatureCache[hex] = feature;
                        
                        // Initialize state for new aircraft
                        aircraftState.set(hex, { lat, lon, heading, speed, alt, timestamp: Date.now() });
                        added++;
                    }
                });

                // Remove features for aircraft that are no longer present
                Object.keys(olFeatureCache).forEach(hex => {
                    if (!currentHexIds.has(hex)) {
                        const feature = olFeatureCache[hex];
                        source.removeFeature(feature);
                        delete olFeatureCache[hex];
                        aircraftState.delete(hex);
                        aircraftTarget.delete(hex);
                        removed++;
                    }
                });

                // Refresh styles with type info
                olAircraftLayer.changed();
                console.log(`[updateOLAircraft] added=${added}, updated=${updated}, removed=${removed}, total=${source.getFeatures().length}`);
                
                // Start animation loop if not already running
                if (!animationFrameId) {
                    animationFrameId = requestAnimationFrame(interpolateAircraftPositions);
                }
            } catch (e) {
                console.error('[updateOLAircraft] Error:', e);
            }
            pendingUpdate = false;
        });
    });
}

function showAircraftPopup(props, pixel) {
    const isFullscreen = radarFullscreen;
    const popup = document.getElementById(isFullscreen ? 'fs-radar-popup' : 'radar-popup');
    const targetMap = isFullscreen ? fullscreenMap : map;
    const headingDeg = Math.round((props.rotation || 0) * 180 / Math.PI);
    const color = getAltitudeColor(props.alt);
    popup.innerHTML = `
        <div class="flex items-center gap-2 mb-1">
            <span class="w-2 h-2 rounded-full" style="background:${color}"></span>
            <span class="font-bold text-cyan-400">${props.callsign || props.hexid}</span>
        </div>
        <div class="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
            <span class="text-gray-400">Altitude:</span><span>${(props.alt || 0).toLocaleString()} ft</span>
            <span class="text-gray-400">Speed:</span><span>${props.speed || 0} kts</span>
            <span class="text-gray-400">Heading:</span><span>${headingDeg}°</span>
            <span class="text-gray-400">Hex:</span><span class="font-mono">${props.hexid}</span>
            ${props.origin ? `<span class="text-gray-400">From:</span><span>${props.origin}</span>` : ''}
            ${props.destination ? `<span class="text-gray-400">To:</span><span>${props.destination}</span>` : ''}
        </div>
    `;
    const containerRect = targetMap.getTargetElement().getBoundingClientRect();
    let left = pixel[0] + 15;
    let top = pixel[1] + 15;
    if (left + 200 > containerRect.width) left = pixel[0] - 210;
    if (top + 120 > containerRect.height) top = pixel[1] - 130;
    popup.style.left = left + 'px';
    popup.style.top = top + 'px';
    popup.classList.remove('hidden');
}

function selectAircraftInList(hexid) {
    // Highlight in fullscreen list if open
    if (radarFullscreen) {
        highlightFullscreenAircraft(hexid);
    }
}

function refreshAll() {
    fetchATC();
    fetchOps();
    fetchExec();
    fetchSchedules();
}

// --- 3. CHART SETUP (🌟 Bulletproof Promise Loader) ---
let charts = {};

function createChart(id, type, options = {}) {
    const ctx = document.getElementById(id);
    if(!ctx) return null;
    charts[id] = new Chart(ctx, { type, data: { labels: [], datasets: [] }, options: { responsive: true, maintainAspectRatio: false, ...options } });
    return charts[id];
}

window.resetFleetZoom = function() { if(charts['fleetChart']) charts['fleetChart'].resetZoom(); };

function initCharts() {
    return new Promise((resolve) => {
        const checkChart = () => {
            if (typeof Chart !== 'undefined') {
                Chart.defaults.color = '#9ca3af';

                createChart('bandChart', 'bar', { 
                    indexAxis: 'y', plugins: { legend: { display: false } }, scales: { x: { display: false }, y: { grid: { display: false } } },
                    interaction: { mode: 'index', intersect: false }, onHover: (e, elements, chart) => { chart.canvas.style.cursor = 'pointer'; },
                    onClick: async (e, elements, chart) => {
                        let idx = elements.length > 0 ? elements[0].index : chart.scales.y.getValueForPixel(e.y);
                        if (idx !== undefined && idx >= 0 && idx < chart.data.labels.length) { await openDrillDownModal('altitude', chart.data.labels[idx], `Live Flights: ${chart.data.labels[idx]}`); }
                    }
                });

                createChart('runwayDemandChart', 'bar', { 
                    scales: { y: { beginAtZero: true, grid: { color: '#374151' } }, x: { grid: { display: false } } },
                    interaction: { mode: 'index', intersect: false }, onHover: (e, elements, chart) => { chart.canvas.style.cursor = 'pointer'; },
                    onClick: async (e, elements, chart) => {
                        let idx = elements.length > 0 ? elements[0].index : chart.scales.x.getValueForPixel(e.x);
                        if (idx !== undefined && idx >= 0 && idx < chart.data.labels.length) { await openDrillDownModal('demand', chart.data.labels[idx], `Arrivals at ${chart.data.labels[idx]}`); }
                    }
                });

                createChart('fleetChart', 'scatter', { 
                    plugins: { 
                        legend: { display: false },
                        tooltip: { callbacks: { label: function(context) { const d = context.raw; return ` Airframe ${d.hex}: ${d.realY} Hours Airborne (${d.realX} Flights)`; } } },
                        zoom: { zoom: { wheel: { enabled: true }, pinch: { enabled: true }, mode: 'xy' }, pan: { enabled: true, mode: 'xy' } }
                    }, 
                    interaction: { mode: 'nearest', intersect: false }, 
                    elements: { point: { radius: 5, hoverRadius: 8, hitRadius: 20, backgroundColor: 'rgba(16, 185, 129, 0.6)', borderColor: '#10b981', borderWidth: 1 } },
                    scales: { x: { title: { display: true, text: 'Flights' }, grid: { color: '#374151' } }, y: { title: { display: true, text: 'Hours Airborne' }, grid: { color: '#374151' } } },
                    onHover: (e, elements, chart) => { chart.canvas.style.cursor = elements.length ? 'pointer' : 'default'; },
                    onClick: async (e, elements, chart) => {
                        if (elements.length > 0) {
                            const idx = elements[0].index;
                            const d = chart.data.datasets[0].data[idx];
                            await openDrillDownModal('fleet', d.hex, `Airframe ${d.hex}`);
                        }
                    }
                });

                createChart('turnaroundChart', 'bar', { plugins: { legend: { display: false } }, scales: { y: { grid: { color: '#374151' } }, x: { grid: { display: false } } }, interaction: { mode: 'index', intersect: false }, onHover: (e, elements, chart) => { chart.canvas.style.cursor = 'pointer'; }, onClick: async (e, elements, chart) => { let idx = elements.length > 0 ? elements[0].index : chart.scales.x.getValueForPixel(e.x); if (idx !== undefined && idx >= 0 && idx < chart.data.labels.length) { await openDrillDownModal('turnaround', chart.data.rawCodes[idx], chart.data.labels[idx]); } } });
                createChart('safetyChart', 'line', { plugins: { legend: { display: false } }, tension: 0.4, scales: { x: { grid: { display: false } } }, interaction: { mode: 'index', intersect: false }, onHover: (e, elements, chart) => { chart.canvas.style.cursor = 'pointer'; }, onClick: async (e, elements, chart) => { let idx = elements.length > 0 ? elements[0].index : chart.scales.x.getValueForPixel(e.x); if (idx !== undefined && idx >= 0 && idx < chart.data.labels.length) { await openDrillDownModal('safety', chart.data.labels[idx], `Incidents on ${chart.data.labels[idx]}`); } } });
                createChart('cdoChart', 'bar', { plugins: { legend: { display: false } }, scales: { y: { grid: { color: '#374151' } }, x: { grid: { display: false } } }, interaction: { mode: 'index', intersect: false }, onHover: (e, elements, chart) => { chart.canvas.style.cursor = 'pointer'; }, onClick: async (e, elements, chart) => { let idx = elements.length > 0 ? elements[0].index : chart.scales.x.getValueForPixel(e.x); if (idx !== undefined && idx >= 0 && idx < chart.data.labels.length) { await openDrillDownModal('cdo', chart.data.rawCodes[idx], chart.data.labels[idx]); } } });
                createChart('otpChart', 'bar', { plugins: { legend: { display: false } }, scales: { y: { grid: { color: '#374151' } }, x: { grid: { display: false } } }, interaction: { mode: 'index', intersect: false }, onHover: (e, elements, chart) => { chart.canvas.style.cursor = 'pointer'; }, onClick: async (e, elements, chart) => { let idx = elements.length > 0 ? elements[0].index : chart.scales.x.getValueForPixel(e.x); if (idx !== undefined && idx >= 0 && idx < chart.data.labels.length) { await openDrillDownModal('otp', chart.data.rawCodes[idx], chart.data.labels[idx]); } } });

                resolve();
            } else {
                console.warn("Chart.js loading... waiting.");
                setTimeout(checkChart, 100);
            }
        };
        checkChart();
    });
}

// --- 4. DRILL-DOWN MODAL ---
async function openDrillDownModal(type, targetCode, targetDisplay) {
    const f = getFilters();
    document.getElementById('drilldown-modal').classList.remove('hidden');
    
    let titleText = 'Details';
    if (type === 'otp') titleText = 'Arrival Delays';
    else if (type === 'turnaround') titleText = 'Turnaround Records';
    else if (type === 'fleet') titleText = 'Flight Log';
    else if (type === 'safety') titleText = 'Safety Audit';
    else if (type === 'cdo') titleText = 'CDO Inefficiencies';
    else if (type === 'route') titleText = 'Corridor Traffic';
    else if (type === 'demand') titleText = 'APOC Hourly Landings';
    else if (type === 'altitude') titleText = 'Altitude Band';

    document.getElementById('modal-title').innerHTML = `<i class="fa-solid fa-list-check text-blue-400 mr-2"></i> ${titleText}: ${targetDisplay || targetCode}`;
    document.getElementById('modal-tbody').innerHTML = `<tr><td colspan="6" class="p-6 text-center text-gray-400"><i class="fa-solid fa-spinner fa-spin text-2xl mb-2"></i><br>Loading data...</td></tr>`;
    
    try {
        if (type === 'turnaround') {
            const res = await fetch(`/api/drilldown/turnaround?target_airline=${targetCode}&airport=${f.airport}`);
            const data = await res.json();
            document.getElementById('modal-thead').innerHTML = `<tr><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 0, 'string')">Hex ID <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 1, 'string')">Landing Flight <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 2, 'string')">TakeOff Flight <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 3, 'string')">Landing Time <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 4, 'string')">TakeOff Time <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 text-right cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 5, 'numeric')">Turnaround <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th></tr>`;
            let html = '';
            data.forEach(row => {
                let color = row.turnaround_mins > 90 ? 'text-red-400' : 'text-green-400';
                let clickAttr = row.hex_id ? `onclick="openForensicsModal('${row.hex_id}', '${row.landing_callsign}')" class="px-4 py-3 text-gray-400 text-xs font-mono uppercase cursor-pointer hover:text-white transition"` : `class="px-4 py-3 text-gray-400 text-xs font-mono uppercase"`;
                html += `<tr class="hover:bg-gray-800 transition"><td ${clickAttr}>${row.hex_id}</td><td class="px-4 py-3 text-white font-bold"><i class="fa-solid fa-plane-arrival text-blue-500 mr-1 text-xs"></i> ${row.landing_callsign || 'UNK'}</td><td class="px-4 py-3 text-white font-bold"><i class="fa-solid fa-plane-departure text-purple-500 mr-1 text-xs"></i> ${row.takeoff_callsign || 'UNK'}</td><td class="px-4 py-3 text-gray-300">${row.landing_time}</td><td class="px-4 py-3 text-gray-300">${row.takeoff_time}</td><td class="px-4 py-3 text-right font-bold ${color}">${row.turnaround_mins}m</td></tr>`;
            });
            document.getElementById('modal-tbody').innerHTML = html || `<tr><td colspan="6" class="p-6 text-center text-gray-500">No turnaround data found.</td></tr>`;
        
        } else if (type === 'otp') {
            const res = await fetch(`/api/drilldown/otp?target_airline=${targetCode}&airport=${f.airport}`);
            const data = await res.json();
            document.getElementById('modal-thead').innerHTML = `<tr><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 0, 'string')">Hex ID <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 1, 'string')">Flight <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 2, 'string')">Origin <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 3, 'string')">Sched Time <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 4, 'string')">Actual Time <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 text-right cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 5, 'numeric')">Delay <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th></tr>`;
            let html = '';
            data.forEach(row => {
                let color = row.delay_mins > 15 ? 'text-red-400' : (row.delay_mins > 0 ? 'text-yellow-400' : 'text-green-400');
                let clickAttr = row.hex_id ? `onclick="openForensicsModal('${row.hex_id}', '${row.callsign}')" class="px-4 py-3 text-gray-400 text-xs font-mono uppercase cursor-pointer hover:text-white transition"` : `class="px-4 py-3 text-gray-400 text-xs font-mono uppercase"`;
                html += `<tr class="hover:bg-gray-800 transition">
                    <td ${clickAttr}>${row.hex_id || 'UNK'}</td>
                    <td class="px-4 py-3 text-white font-bold"><i class="fa-solid fa-plane-arrival text-blue-500 mr-1 text-xs"></i> ${row.callsign || 'UNK'}</td>
                    <td class="px-4 py-3 text-gray-300">${row.route_airport_display || 'UNK'}</td>
                    <td class="px-4 py-3 text-gray-300">${row.sched_time}</td>
                    <td class="px-4 py-3 text-gray-300">${row.act_time}</td>
                    <td class="px-4 py-3 text-right font-bold ${color}">${row.delay_mins}m</td>
                </tr>`;
            });
            document.getElementById('modal-tbody').innerHTML = html || `<tr><td colspan="6" class="p-6 text-center text-gray-500">No arrival data found.</td></tr>`;

        } else if (type === 'fleet') {
            const res = await fetch(`/api/drilldown/fleet?hex_id=${targetCode}`);
            const data = await res.json();
            document.getElementById('modal-thead').innerHTML = `<tr><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 0, 'string')">Callsign <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 1, 'string')">Origin <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 2, 'string')">Destination <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 3, 'string')">Departure <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 4, 'string')">Arrival <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 text-right cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 5, 'numeric')">Air Time <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th></tr>`;
            let html = '';
            data.forEach(row => {
                let clickAttr = `onclick="openForensicsModal('${targetCode}', '${row.callsign}')" class="px-4 py-3 text-white font-bold cursor-pointer hover:text-blue-400 transition"`;
                html += `<tr class="hover:bg-gray-800 transition">
                    <td ${clickAttr}><i class="fa-solid fa-plane text-blue-500 mr-1 text-xs"></i> ${row.callsign || 'UNK'}</td>
                    <td class="px-4 py-3 text-gray-300">${row.origin_display || 'UNK'}</td>
                    <td class="px-4 py-3 text-gray-300">${row.dest_display || 'UNK'}</td>
                    <td class="px-4 py-3 text-gray-300 font-mono text-sm">${row.dep_time}</td>
                    <td class="px-4 py-3 text-gray-300 font-mono text-sm">${row.arr_time}</td>
                    <td class="px-4 py-3 text-right font-bold text-green-400">${row.duration_mins}m</td>
                </tr>`;
            });
            document.getElementById('modal-tbody').innerHTML = html || `<tr><td colspan="6" class="p-6 text-center text-gray-500">No flight legs found for this airframe in the last 7 days.</td></tr>`;
        
        } else if (type === 'safety') {
            const res = await fetch(`/api/drilldown/safety?target_date=${targetCode}`);
            const data = await res.json();
            document.getElementById('modal-thead').innerHTML = `<tr><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 0, 'string')">Time <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 1, 'string')">Flight <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 2, 'string')">Hex ID <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 3, 'string')">Airport <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 text-red-400 cursor-pointer hover:text-red-300 transition" onclick="sortTable('modal-tbody', 4, 'string')">Anomaly <i class="fa-solid fa-sort ml-1 sort-icon text-red-800"></i></th><th class="px-4 py-3 text-right">Details</th></tr>`;
            let html = '';
            data.forEach(row => {
                let clickAttr = row.hex_id ? `onclick="openForensicsModal('${row.hex_id}', '${row.callsign}')" class="px-4 py-3 text-gray-400 text-xs font-mono uppercase cursor-pointer hover:text-white transition"` : `class="px-4 py-3 text-gray-400 text-xs font-mono uppercase"`;
                html += `<tr class="hover:bg-gray-800 transition">
                    <td class="px-4 py-3 text-gray-300 font-mono text-sm">${row.time}</td>
                    <td class="px-4 py-3 text-white font-bold"><i class="fa-solid fa-triangle-exclamation text-red-500 mr-1 text-xs"></i> ${row.callsign || 'UNK'}</td>
                    <td ${clickAttr}>${row.hex_id || 'UNK'}</td>
                    <td class="px-4 py-3 text-gray-300">${row.airport || 'UNK'}</td>
                    <td class="px-4 py-3 font-bold text-red-400">${(row.anomaly_flag || '').replace('_', ' ')}</td>
                    <td class="px-4 py-3 text-right text-gray-400 text-xs">${row.details || '-'}</td>
                </tr>`;
            });
            document.getElementById('modal-tbody').innerHTML = html || `<tr><td colspan="6" class="p-6 text-center text-gray-500">No incidents recorded for this date.</td></tr>`;

        } else if (type === 'cdo') {
            const res = await fetch(`/api/drilldown/cdo?target_airline=${targetCode}`);
            const data = await res.json();
            document.getElementById('modal-thead').innerHTML = `<tr><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 0, 'string')">Hex ID <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 1, 'string')">Flight <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 2, 'string')">Airport <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 3, 'string')">Landing Time <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 text-right cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 4, 'numeric')">Approach Mins <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th></tr>`;
            let html = '';
            data.forEach(row => {
                let clickAttr = row.hex_id ? `onclick="openForensicsModal('${row.hex_id}', '${row.callsign}')" class="px-4 py-3 text-gray-400 text-xs font-mono uppercase cursor-pointer hover:text-white transition"` : `class="px-4 py-3 text-gray-400 text-xs font-mono uppercase"`;
                let color = row.approach_mins > 20 ? 'text-red-400' : (row.approach_mins > 10 ? 'text-yellow-400' : 'text-green-400');
                html += `<tr class="hover:bg-gray-800 transition">
                    <td ${clickAttr}>${row.hex_id || 'UNK'}</td>
                    <td class="px-4 py-3 text-white font-bold"><i class="fa-solid fa-plane-arrival text-green-500 mr-1 text-xs"></i> ${row.callsign || 'UNK'}</td>
                    <td class="px-4 py-3 text-gray-300">${row.airport_display || 'UNK'}</td>
                    <td class="px-4 py-3 text-gray-300 font-mono text-sm">${row.landing_time}</td>
                    <td class="px-4 py-3 text-right font-bold ${color}">${row.approach_mins}m</td>
                </tr>`;
            });
            document.getElementById('modal-tbody').innerHTML = html || `<tr><td colspan="5" class="p-6 text-center text-gray-500">No CDO data found for this airline.</td></tr>`;

        } else if (type === 'route') {
            const parts = targetCode.split('|');
            const res = await fetch(`/api/drilldown/route?origin=${parts[0]}&destination=${parts[1]}`);
            const data = await res.json();
            document.getElementById('modal-thead').innerHTML = `<tr><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 0, 'string')">Time <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 1, 'string')">Flight <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 2, 'string')">Hex ID <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 text-right cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 3, 'string')">Origin <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 text-right cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 4, 'string')">Destination <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th></tr>`;
            let html = '';
            data.forEach(row => {
                let clickAttr = row.hex_id ? `onclick="openForensicsModal('${row.hex_id}', '${row.callsign}')" class="px-4 py-3 text-gray-400 text-xs font-mono uppercase cursor-pointer hover:text-white transition"` : `class="px-4 py-3 text-gray-400 text-xs font-mono uppercase"`;
                html += `<tr class="hover:bg-gray-800 transition">
                    <td class="px-4 py-3 text-gray-300 font-mono text-sm">${row.time}</td>
                    <td class="px-4 py-3 text-white font-bold"><i class="fa-solid fa-plane text-blue-500 mr-1 text-xs"></i> ${row.callsign || 'UNK'}</td>
                    <td ${clickAttr}>${row.hex_id || 'UNK'}</td>
                    <td class="px-4 py-3 text-right font-bold text-gray-300">${parts[0]}</td>
                    <td class="px-4 py-3 text-right font-bold text-gray-300">${parts[1]}</td>
                </tr>`;
            });
            document.getElementById('modal-tbody').innerHTML = html || `<tr><td colspan="5" class="p-6 text-center text-gray-500">No recent flights found for this route.</td></tr>`;

        } else if (type === 'demand') {
            const res = await fetch(`/api/drilldown/demand?hour_bucket=${targetCode}&airport=${f.airport}`);
            const data = await res.json();
            document.getElementById('modal-thead').innerHTML = `<tr><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 0, 'string')">Time <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 1, 'string')">Flight <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 2, 'string')">Hex ID <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 3, 'string')">Origin <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 4, 'string')">Airport <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 text-right cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 5, 'string')">Runway <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th></tr>`;
            let html = '';
            data.forEach(row => {
                let clickAttr = row.hex_id ? `onclick="openForensicsModal('${row.hex_id}', '${row.callsign}')" class="px-4 py-3 text-gray-400 text-xs font-mono uppercase cursor-pointer hover:text-white transition"` : `class="px-4 py-3 text-gray-400 text-xs font-mono uppercase"`;
                html += `<tr class="hover:bg-gray-800 transition">
                    <td class="px-4 py-3 text-gray-300 font-mono text-sm">${row.time}</td>
                    <td class="px-4 py-3 text-white font-bold"><i class="fa-solid fa-plane-arrival text-green-500 mr-1 text-xs"></i> ${row.callsign || 'UNK'}</td>
                    <td ${clickAttr}>${row.hex_id || 'UNK'}</td>
                    <td class="px-4 py-3 text-gray-300">${row.origin_display || 'UNK'}</td>
                    <td class="px-4 py-3 text-gray-300">${row.airport_display || 'UNK'}</td>
                    <td class="px-4 py-3 text-right font-bold text-yellow-400">${row.runway || 'UNK'}</td>
                </tr>`;
            });
            document.getElementById('modal-tbody').innerHTML = html || `<tr><td colspan="6" class="p-6 text-center text-gray-500">No arrivals found for this hour.</td></tr>`;
        
        } else if (type === 'altitude') {
            const res = await fetch(`/api/drilldown/altitude?band=${encodeURIComponent(targetCode)}&airline=${f.airline}&airport=${f.airport}`);
            const data = await res.json();
            document.getElementById('modal-thead').innerHTML = `<tr><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 0, 'string')">Hex ID <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 1, 'string')">Flight <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 text-right cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 2, 'numeric')">Altitude <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 text-right cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 3, 'numeric')">Speed <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th><th class="px-4 py-3 text-right cursor-pointer hover:text-white transition" onclick="sortTable('modal-tbody', 4, 'numeric')">Heading <i class="fa-solid fa-sort ml-1 sort-icon text-gray-600"></i></th></tr>`;
            let html = '';
            data.forEach(row => {
                let clickAttr = row.hex_id ? `onclick="openForensicsModal('${row.hex_id}', '${row.callsign}')" class="px-4 py-3 text-gray-400 text-xs font-mono uppercase cursor-pointer hover:text-white transition"` : `class="px-4 py-3 text-gray-400 text-xs font-mono uppercase"`;
                let altColor = row.alt < 10000 ? 'text-orange-400' : (row.alt < 20000 ? 'text-blue-400' : 'text-purple-400');
                html += `<tr class="hover:bg-gray-800 transition">
                    <td ${clickAttr}>${row.hex_id || 'UNK'}</td>
                    <td class="px-4 py-3 text-white font-bold"><i class="fa-solid fa-plane text-blue-500 mr-1 text-xs"></i> ${row.callsign || 'UNK'}</td>
                    <td class="px-4 py-3 text-right font-mono font-bold ${altColor}">${Math.round(row.alt).toLocaleString()} ft</td>
                    <td class="px-4 py-3 text-right text-gray-300 font-mono">${Math.round(row.speed)} kts</td>
                    <td class="px-4 py-3 text-right text-gray-300 font-mono">${Math.round(row.heading)}°</td>
                </tr>`;
            });
            document.getElementById('modal-tbody').innerHTML = html || `<tr><td colspan="5" class="p-6 text-center text-gray-500">No active flights in this band.</td></tr>`;
        }
    } catch(e) { console.error("DrillDown Error:", e); }
}
function closeModal() { document.getElementById('drilldown-modal').classList.add('hidden'); }

// --- FLIGHT FORENSICS MODAL LOGIC ---
let fMap = null; let fPolyline = null; let fChartInstance = null;

async function openForensicsModal(hexId, callsign) {
    if (!hexId || hexId === 'null') return; 
    document.getElementById('forensics-modal').classList.remove('hidden');
    document.getElementById('forensics-title').innerHTML = `<i class="fa-solid fa-magnifying-glass-chart text-blue-500 mr-2"></i> Forensics: ${callsign} (Hex: <span class="text-blue-400 font-mono">${hexId}</span>)`;
    document.getElementById('forensics-chart-container').classList.add('hidden');
    document.getElementById('forensics-loading').classList.remove('hidden');
    document.getElementById('forensics-ai-audit').classList.add('hidden'); 
    document.getElementById('forensics-loading').innerHTML = `<i class="fa-solid fa-satellite-dish fa-spin text-4xl mb-3 text-blue-500"></i><p class="font-bold tracking-widest uppercase text-sm">Querying InfluxDB...</p>`;

    if (!fMap) {
        if (typeof L === 'undefined') return;
        fMap = L.map('forensics-map', { zoomControl: false }).setView([20.5937, 78.9629], 5);
        L.control.zoom({ position: 'topright' }).addTo(fMap);
        L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', { maxZoom: 19 }).addTo(fMap);
    }

    if (fPolyline) fMap.removeLayer(fPolyline);
    if (fChartInstance) fChartInstance.destroy();

    // 1. Fetch AI Audit Trail
    try {
        const aiRes = await fetch(`/api/ai/audit?hex_id=${hexId}&callsign=${callsign}`);
        const aiData = await aiRes.json();
        if (aiData && aiData.length > 0) {
            document.getElementById('forensics-ai-audit').classList.remove('hidden');
            let aiHtml = '';
            aiData.forEach(audit => {
                aiHtml += `
                <div class="mb-3 last:mb-0 pb-3 last:pb-0 border-b border-gray-800 last:border-0">
                    <div class="flex items-center gap-2 mb-1">
                        <span class="bg-gray-800 px-2 py-0.5 rounded text-[10px] text-purple-400 font-bold">${audit.time}</span>
                        <span class="text-gray-400 text-xs">Origin Gap Resolved</span>
                    </div>
                    <div class="text-xs mb-1">Replaced <span class="text-red-400 line-through bg-red-900/30 px-1 rounded">${audit.original_value || 'UNKNOWN'}</span> with <span class="text-emerald-400 font-bold bg-emerald-900/30 px-1 rounded">${audit.ai_inferred_value}</span></div>
                    <div class="text-[10px] text-gray-500 italic mt-1 pl-2 border-l-2 border-purple-500/50">" ${audit.ai_reasoning} " <br><span class="text-purple-500/70 mt-1 block">Confidence: ${(audit.confidence_score * 100).toFixed(0)}%</span></div>
                </div>`;
            });
            document.getElementById('forensics-ai-content').innerHTML = aiHtml;
        }
    } catch(e) { console.error("AI Audit Fetch Error", e); }

    // 2. Fetch Telemetry
    try {
        const res = await fetch(`/api/telemetry/track?hex_id=${hexId}`);
        const data = await res.json();
        if (data.length === 0) {
            document.getElementById('forensics-loading').innerHTML = `<i class="fa-solid fa-triangle-exclamation text-4xl mb-3 text-red-500"></i><p class="font-bold tracking-widest uppercase text-sm text-gray-400">No Telemetry Found in last 24h</p>`;
            return;
        }

        const latlngs = data.filter(d => d.lat !== null && d.lon !== null).map(d => [d.lat, d.lon]);
        if (latlngs.length > 0) {
            fPolyline = L.polyline(latlngs, { color: '#3b82f6', weight: 4, opacity: 0.8 }).addTo(fMap);
        }

        const times = data.map(d => d.time); const alts = data.map(d => d.alt || 0); const speeds = data.map(d => d.speed || 0);
        document.getElementById('forensics-loading').classList.add('hidden');
        
        document.getElementById('forensics-chart-container').classList.remove('hidden');
        setTimeout(() => {
            fMap.invalidateSize();
            if (fPolyline) fMap.fitBounds(fPolyline.getBounds(), { padding: [20, 20] });
        }, 100);

        const ctx = document.getElementById('forensicsChart');
        fChartInstance = new Chart(ctx, {
            type: 'line',
            data: { labels: times, datasets: [{ label: 'Altitude (ft)', data: alts, borderColor: '#3b82f6', yAxisID: 'y', pointRadius: 0, tension: 0.4 }, { label: 'Speed (kts)', data: speeds, borderColor: '#ef4444', yAxisID: 'y1', pointRadius: 0, tension: 0.4 }] },
            options: { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false }, plugins: { legend: { labels: { color: '#9ca3af' } } }, scales: { x: { grid: { display: false }, ticks: { color: '#6b7280' } }, y: { display: true, position: 'left', grid: { color: '#374151' }, ticks: { color: '#9ca3af' } }, y1: { display: true, position: 'right', grid: { drawOnChartArea: false }, ticks: { color: '#9ca3af' } } } }
        });
    } catch (e) { document.getElementById('forensics-loading').innerHTML = `<p class="text-red-500">Error loading telemetry.</p>`; }
}
function closeForensicsModal() { document.getElementById('forensics-modal').classList.add('hidden'); }

// --- 5. DATA FETCHERS ---
let showingHeatmap = false;
function toggleHeatmap() { showingHeatmap = document.getElementById('toggle-heatmap').checked; fetchATC(); }

async function fetchATC() {
    try {
        const f = getFilters();
        let flights = [];
        
        // Use WebSocket data if enabled
        if (WS_USE_FOR_ATC && wsFlightsData.length > 0) {
            // Filter flights from WebSocket data
            flights = wsFlightsData.filter(ac => {
                if (f.airline !== 'ALL' && (!ac.callsign || !ac.callsign.startsWith(f.airline))) return false;
                if (f.airport !== 'ALL' && (!ac.callsign || !ac.callsign.includes(f.airport))) return false;
                return ac.lat && ac.lon;
            });
        } else {
            // Use radar endpoint with dynamic center + radius based on map viewport
            try {
                const resMap = await fetch(`/api/aircraft/radar?lat=${mapCenter.lat}&lon=${mapCenter.lon}&radius=${mapRadius}`);
                flights = await resMap.json();
                if (!Array.isArray(flights)) flights = [];
                // The radar endpoint already fixes swapped lat/lon — no need for client-side fix
            } catch (e) {
                // Fallback to live endpoint if radar endpoint fails
                console.warn('[fetchATC] Radar endpoint failed, falling back to live:', e);
                const resLive = await fetch(`/api/atc/live?airline=${f.airline}&airport=${f.airport}`);
                const payload = await resLive.json();
                flights = payload.flights || [];
                flights.forEach(ac => {
                    const lat = parseFloat(ac.lat);
                    const lon = parseFloat(ac.lon);
                    if (!isNaN(lat) && !isNaN(lon) && Math.abs(lat) > 60 && Math.abs(lon) < 60) {
                        ac.lat = lon;
                        ac.lon = lat;
                    }
                });
            }
            // Apply client-side airline/airport filter (radar endpoint doesn't support them)
            if (f.airline !== 'ALL' || f.airport !== 'ALL') {
                flights = flights.filter(ac => {
                    if (f.airline !== 'ALL' && (!ac.callsign || !ac.callsign.startsWith(f.airline))) return false;
                    if (f.airport !== 'ALL' && (!ac.callsign || !ac.callsign.includes(f.airport))) return false;
                    return true;
                });
            }
            console.log(`[fetchATC] Received ${flights.length} flights from API`);
            if (flights.length > 0) {
                console.log('[fetchATC] Sample flight:', flights[0]);
            }
        }

        const active = flights.length;
        const spd = active > 0 ? flights.reduce((sum, fl) => sum + (fl.speed || 0), 0) / active : 0;
        const alt = active > 0 ? flights.reduce((sum, fl) => sum + (fl.alt || 0), 0) / active : 0;
        
        const countEl = document.getElementById('radar-count');
        const spdEl = document.getElementById('atc-spd');
        const altEl = document.getElementById('atc-alt');
        
        if (countEl) countEl.innerText = active || '0';
        if (spdEl) spdEl.innerText = Math.round(spd) + ' kts';
        if (altEl) altEl.innerText = Math.round(alt).toLocaleString() + ' ft';
        
        radarAircraft = {};
        flights.forEach(ac => {
            if (ac.lat && ac.lon) {
                const hex = ac.hexid || ac.hex || 'unknown';
                radarAircraft[hex] = {
                    hex: hex,
                    callsign: ac.callsign || '',
                    lat: parseFloat(ac.lat),
                    lon: parseFloat(ac.lon),
                    alt: parseFloat(ac.alt) || 0,
                    gs: parseFloat(ac.speed) || 0,
                    heading: parseFloat(ac.heading) || 0,
                    origin: ac.origin || '',
                    destination: ac.destination || ''
                };
            }
        });
        
        if (olMapInitialized && map && olAircraftLayer) {
            updateOLAircraft(flights);
        }
        
        // Update fullscreen if visible - always process, no delay
        if (fullscreenOlInitialized && fullscreenAircraftLayer && radarFullscreen) {
            requestAnimationFrame(() => updateFullscreenAircraft(flights));
        }
        
        try {
            const resBands = await fetch(`/api/atc/bands?airline=${f.airline}&airport=${f.airport}`);
            const bands = await resBands.json();
            if (charts['bandChart']) {
                charts['bandChart'].data.labels = bands.map(b => b.band);
                charts['bandChart'].data.datasets = [{ data: bands.map(b => b.count), backgroundColor: '#3b82f6', borderRadius: 4 }];
                charts['bandChart'].update();
            }
        } catch(e) { console.warn('Bands fetch failed:', e); }
        
        try {
            const resAnom = await fetch(`/api/atc/anomalies?airline=${f.airline}&airport=${f.airport}`);
            const anom = await resAnom.json();
            let aHtml = '';
            anom.forEach(a => {
                let color = a.anomaly_flag === 'GO_AROUND' ? 'text-yellow-400' : 'text-red-400';
                aHtml += `<tr onclick="openForensicsModal('${a.hex_id || ''}', '${a.callsign || ''}')" class="hover:bg-gray-800 cursor-pointer">
                    <td class="py-2 px-1">${a.timestamp ? new Date(a.timestamp).toLocaleTimeString() : '---'}</td>
                    <td class="py-2 px-1">${a.callsign || '---'}</td>
                    <td class="py-2 px-1 ${color}">${a.anomaly_flag || a.remark || '---'}</td>
                </tr>`;
            });
            document.getElementById('atc-anomalies').innerHTML = aHtml;
        } catch(e) { console.warn('Anomalies fetch failed:', e); }
        
    } catch(e) {
        console.error('fetchATC error:', e);
    }
}

async function fetchDelayPredictions() {
    try {
        // Get route OTP - show worst performing route
        const resRoute = await fetch('/api/delay/route_otp?limit=50');
        const routeData = await resRoute.json();
        
        if (routeData.routes && routeData.routes.length > 0) {
            // Find worst route (highest delay)
            const worstRoute = routeData.routes[routeData.routes.length - 1];
            const worstEl = document.getElementById('worst-route');
            if (worstEl && worstRoute.route) {
                const [orig, dest] = worstRoute.route.split('->');
                worstEl.textContent = `${orig}-${dest}` || worstRoute.route;
            }
            
            // Calculate average delay across all routes
            const totalDelay = routeData.routes.reduce((sum, r) => sum + (r.avg_delay_minutes || 0), 0);
            const avgDelay = Math.round(totalDelay / routeData.routes.length);
            const avgEl = document.getElementById('avg-delay');
            if (avgEl) avgEl.textContent = avgDelay + 'm';
        }
        
        // Get airport congestion
        const resAirport = await fetch('/api/delay/airports');
        const airportData = await resAirport.json();
        
        if (airportData.airports && airportData.airports.length > 0) {
            // Most congested airport
            const worstAirport = airportData.airports[0];
            const congestionEl = document.getElementById('airport-congestion');
            if (congestionEl) congestionEl.textContent = worstAirport.airport || '--';
        }
        
    } catch(e) {
        console.warn('Delay prediction fetch error:', e);
    }
}

function setTerminalMode(type, mode) {
    terminalModes[type] = mode;
    const btnBoard = document.getElementById(`btn-${type}-board`);
    const btnLog = document.getElementById(`btn-${type}-log`);
    
    if (mode === 'board') {
        btnBoard.className = "px-3 py-1.5 text-xs font-bold rounded-md bg-blue-600 text-white shadow transition";
        btnLog.className = "px-3 py-1.5 text-xs font-bold rounded-md text-gray-400 hover:text-white transition";
    } else {
        btnLog.className = "px-3 py-1.5 text-xs font-bold rounded-md bg-purple-600 text-white shadow transition";
        btnBoard.className = "px-3 py-1.5 text-xs font-bold rounded-md text-gray-400 hover:text-white transition";
    }
    fetchSchedules(); 
}

function syncDatesAndFetch(source) {
    let selectedDate = document.getElementById(`${source}-date-picker`).value;
    document.getElementById('arr-date-picker').value = selectedDate;
    document.getElementById('dep-date-picker').value = selectedDate;
    fetchSchedules();
}

function clearScheduleDate() {
    document.getElementById('arr-date-picker').value = '';
    document.getElementById('dep-date-picker').value = '';
    fetchSchedules();
}

function applyLocalFilters(type) {
    const flightQ = document.getElementById(`search-${type}-flight`).value.toLowerCase();
    const locQ = document.getElementById(`search-${type}-loc`).value.toLowerCase();
    
    const filtered = rawSchedules[type].filter(r => {
        const flightMatch = (r.callsign || r.flight_number || '').toLowerCase().includes(flightQ);
        const displayLoc = (r.route_airport_display || r.route_airport || '').toLowerCase();
        return flightMatch && displayLoc.includes(locQ);
    });
    
    renderScheduleRows(filtered, type === 'arr' ? 'sched-arrivals' : 'sched-departures', type);
}

function formatScheduleTime(dateStr) {
    if (!dateStr || dateStr === '---') return '---';
    const d = new Date(dateStr.replace(' ', 'T') + ':00');
    if (isNaN(d)) return dateStr;
    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const day = String(d.getDate()).padStart(2, '0');
    const mon = months[d.getMonth()];
    const hrs = String(d.getHours()).padStart(2, '0');
    const mins = String(d.getMinutes()).padStart(2, '0');
    return `${day} ${mon}, ${hrs}:${mins}`;
}

function renderScheduleRows(data, elementId, type) {
    const tbody = document.getElementById(elementId);
    const isArrival = (type === 'arr');
    
    if (!data || data.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" class="p-4 text-center text-gray-500 border-b border-gray-800">No flight data available</td></tr>`;
        return;
    }

    let html = '';
    const now = new Date();

    data.forEach(r => {
        const anomaly = r.anomaly_flag || r.remark;
        const isPhysicalLog = r.remark === 'PHYSICAL_LOG'; 
        const isAI = (anomaly === 'AI_ENRICHED');
        
        let aiBadge = isAI ? `<span class="ml-2 inline-flex items-center text-[9px] bg-purple-900/40 text-purple-300 px-1.5 py-0.5 rounded border border-purple-500/50 shadow-[0_0_8px_rgba(168,85,247,0.3)] whitespace-nowrap align-middle"><i class="fa-solid fa-wand-magic-sparkles ai-sparkle mr-1"></i>AI Inferred</span>` : '';
        const destOriginStr = (r.route_airport_display || r.route_airport || 'UNKNOWN');
        const destOriginHTML = `<span class="flex items-center">${destOriginStr} ${aiBadge}</span>`;

        let timeCol = '';
        let statusCol = '';

        const schedObj = (r.sched_time && r.sched_time !== '---') ? new Date(r.sched_time.replace(' ', 'T') + ':00') : null;
        const actObj = r.act_time ? new Date(r.act_time.replace(' ', 'T') + ':00') : null;

        if (isPhysicalLog) {
            timeCol = formatScheduleTime(r.act_time);
            let rwyText = r.runway && r.runway !== 'UNK' ? `RWY ${r.runway}` : '';
            let actionText = isArrival ? 'Landed' : 'Departed';
            let anomalyText = (anomaly && !isAI && anomaly !== 'SYSTEM_ERROR') ? `<span class="text-red-400 text-[10px] block mt-1">${anomaly.replace('_', ' ')}</span>` : '';

            statusCol = `
                <div class="flex flex-col items-end">
                    <span class="text-emerald-400 text-xs font-bold">${actionText} ${rwyText}</span>
                    ${anomalyText}
                </div>`;
                
        } else if (anomaly === 'UNSCHEDULED') {
            timeCol = formatScheduleTime(r.act_time);
            statusCol = `
                <div class="flex flex-col items-end">
                    <span class="bg-purple-900 text-purple-300 px-2 py-0.5 rounded text-[10px] font-bold tracking-wider uppercase mb-1">Unscheduled</span>
                    <span class="text-gray-400 text-xs">${isArrival ? 'Landed' : 'Departed'} ${formatScheduleTime(r.act_time).split(', ')[1]}</span>
                </div>`;
        } else if (actObj) {
            timeCol = formatScheduleTime(r.sched_time);
            const diffMins = Math.round((actObj - schedObj) / 60000);
            let diffText = ''; let colorClass = '';
            
            if (diffMins > 15) { diffText = `Delayed ${diffMins}m`; colorClass = 'text-red-400 font-bold'; } 
            else if (diffMins < -15) { diffText = `Early ${Math.abs(diffMins)}m`; colorClass = 'text-emerald-400 font-bold'; } 
            else { diffText = `On Time`; colorClass = 'text-emerald-500 font-semibold'; }

            statusCol = `
                <div class="flex flex-col items-end">
                    <span class="${colorClass} text-xs mb-1">${diffText}</span>
                    <span class="bg-gray-800 border border-gray-700 text-gray-300 px-2 py-0.5 rounded text-[10px] font-bold tracking-wider uppercase">${isArrival ? 'Landed' : 'Departed'} ${formatScheduleTime(r.act_time).split(', ')[1]}</span>
                </div>`;
        } else {
            timeCol = formatScheduleTime(r.sched_time);
            const diffNowMins = Math.round((now - schedObj) / 60000);
            
            if (anomaly === 'PRE_FLIGHT') {
                statusCol = `<span class="bg-blue-900/40 border border-blue-700 text-blue-400 px-2 py-1 rounded text-xs font-bold tracking-wide flex items-center justify-end w-fit ml-auto shadow-[0_0_10px_rgba(59,130,246,0.2)]"><span class="w-2 h-2 rounded-full bg-blue-500 animate-ping mr-2"></span> Aircraft Active</span>`;
            } else if (anomaly === 'ARRIVING_SHORTLY') {
                statusCol = `<span class="bg-orange-900/40 border border-orange-700 text-orange-400 px-2 py-1 rounded text-xs font-bold tracking-wide flex items-center justify-end w-fit ml-auto shadow-[0_0_10px_rgba(249,115,22,0.2)]"><span class="w-2 h-2 rounded-full bg-orange-500 animate-ping mr-2"></span> Arriving Shortly</span>`;
            } else if (diffNowMins > 15) { 
                statusCol = `<span class="text-red-400 font-bold text-xs tracking-wide flex items-center justify-end"><i class="fa-solid fa-circle-exclamation mr-1"></i> Delayed</span>`; 
            } else { 
                statusCol = `<span class="text-gray-500 font-medium text-xs tracking-wide">Scheduled</span>`; 
            }
        }

        // Setup click handlers to open forensics on both IATA and ICAO columns
        let flightClick = r.hex_id ? `onclick="openForensicsModal('${r.hex_id}', '${r.callsign || r.flight_number}')" class="py-3 px-2 sm:px-4 font-bold text-blue-400 cursor-pointer hover:text-white transition tracking-wide"` : `class="py-3 px-2 sm:px-4 font-bold text-blue-400 tracking-wide"`;
        let iataClick = r.hex_id ? `onclick="openForensicsModal('${r.hex_id}', '${r.callsign || r.flight_number}')" class="py-3 px-2 sm:px-4 font-bold text-gray-300 cursor-pointer hover:text-white transition tracking-wide"` : `class="py-3 px-2 sm:px-4 font-bold text-gray-300 tracking-wide"`;

        html += `
            <tr class="hover:bg-gray-800/60 transition-colors border-b border-gray-800/50">
                <td class="py-3 px-2 sm:px-4 font-mono text-gray-300 text-xs whitespace-nowrap">${timeCol}</td>
                <td ${iataClick}>${r.flight_number || '---'}</td>
                <td ${flightClick}>${r.callsign || '---'}</td>
                <td class="py-3 px-2 sm:px-4 text-gray-400 truncate max-w-[150px]" title="${destOriginStr}">${destOriginHTML}</td>
                <td class="py-3 px-2 sm:px-4 text-right align-middle">${statusCol}</td>
            </tr>
        `;
    });
    tbody.innerHTML = html;
}

let rawSchedules = { arr: [], dep: [] };

async function fetchSchedules() {
    const f = getFilters();
    const targetDate = document.getElementById('arr-date-picker').value;
    const dateParam = targetDate ? `&target_date=${targetDate}` : '';
    
    if (f.airport === 'ALL') {
        const msg = `<tr><td colspan="4" class="p-12 text-center text-gray-500"><i class="fa-solid fa-globe text-3xl mb-3 block"></i><br>Please select a specific airport from the top filter to view flight data.</td></tr>`;
        document.getElementById('sched-arrivals').innerHTML = msg;
        document.getElementById('sched-departures').innerHTML = msg;
        return;
    }
    
    try {
        const arrEndpoint = terminalModes.arr === 'board' ? 'schedules' : 'logs';
        const resArr = await fetch(`/api/ops/${arrEndpoint}?airport=${f.airport}&direction=ARRIVALS${dateParam}`);
        rawSchedules.arr = await resArr.json();
        applyLocalFilters('arr'); 

        const depEndpoint = terminalModes.dep === 'board' ? 'schedules' : 'logs';
        const resDep = await fetch(`/api/ops/${depEndpoint}?airport=${f.airport}&direction=DEPARTURES${dateParam}`);
        rawSchedules.dep = await resDep.json();
        applyLocalFilters('dep'); 
    } catch(e) { console.error("Schedules Fetch Error:", e); }
}

async function fetchAIOperations() {
    try {
        const resEnrich = await fetch('/api/ai/operations/enrichment');
        const enrichData = await resEnrich.json();
        let eHtml = '';
        enrichData.forEach(r => {
            let confBadge = r.confidence_score > 0.9 ? 'text-emerald-400' : 'text-yellow-400';
            eHtml += `<tr class="hover:bg-gray-800 transition">
                <td class="py-3 px-4 font-mono text-gray-400 whitespace-nowrap text-xs">${r.time}</td>
                <td class="py-3 px-4 font-bold text-white text-xs cursor-pointer hover:text-blue-400" onclick="openForensicsModal('${r.hex_id}', '${r.callsign}')">${r.callsign || r.hex_id}</td>
                <td class="py-3 px-4 text-gray-500 text-xs">Replaced <span class="text-red-400 line-through bg-red-900/30 px-1 rounded">${r.original_value || 'UNKNOWN'}</span></td>
                <td class="py-3 px-4 text-xs">
                    <span class="text-emerald-400 font-bold bg-emerald-900/30 px-1 rounded">${r.ai_inferred_value}</span> 
                    <i class="fa-solid fa-circle-info ${confBadge} ml-2 cursor-help" title="${r.ai_reasoning} (Conf: ${(r.confidence_score*100).toFixed(0)}%)"></i>
                </td>
            </tr>`;
        });
        document.getElementById('ai-ledger-tbody').innerHTML = eHtml || `<tr><td colspan="4" class="p-6 text-center text-gray-500">No AI data enrichments recorded recently.</td></tr>`;
        
        const resInsights = await fetch('/api/ai/operations/insights');
        const insightsData = await resInsights.json();
        let iHtml = '';
        insightsData.forEach(r => {
            let icon = r.insight_type === 'DAILY_BRIEFING' ? 'fa-file-lines text-blue-400' : 'fa-bell text-orange-400';
            iHtml += `
            <div class="bg-gray-800/40 border border-gray-700/50 rounded-lg p-4 shadow-sm hover:border-gray-600 transition relative overflow-hidden">
                <div class="absolute top-0 left-0 w-1 h-full bg-purple-500/50"></div>
                <div class="flex justify-between items-start mb-2">
                    <span class="text-xs font-bold text-gray-400 uppercase tracking-wider flex items-center"><i class="fa-solid ${icon} mr-2"></i> ${r.insight_type.replace('_', ' ')}</span>
                    <span class="text-[10px] text-gray-500 font-mono">${r.time}</span>
                </div>
                <p class="text-sm text-gray-200 leading-relaxed mb-3 whitespace-pre-line">${r.insight_text}</p>
                <div class="text-[10px] text-gray-400 bg-gray-900/80 p-2 rounded border border-gray-800 flex items-center font-mono">
                    <i class="fa-solid fa-bolt text-yellow-500 mr-2 opacity-70"></i> Trigger: ${r.trigger_event}
                </div>
            </div>`;
        });
        document.getElementById('ai-insights-feed').innerHTML = iHtml || `<div class="p-6 text-center text-gray-500 border border-dashed border-gray-800 rounded-lg">No proactive insights generated yet. Watchdog is standing by.</div>`;
    } catch(e) { console.error("AI Operations Fetch Error:", e); }
}

async function fetchOps() {
    if (!charts['turnaroundChart']) return;
    const f = getFilters();
    try {
        const resSq = await fetch(`/api/ops/squatters?airport=${f.airport}&airline=${f.airline}`);
        const sq = await resSq.json();
        let sqHtml = '';
        sq.forEach(s => {
            let c = s.mins > 90 ? 'text-red-400 font-bold' : 'text-yellow-400';
            let clickAttr = s.hex_id ? `onclick="openForensicsModal('${s.hex_id}', '${s.callsign}')" class="py-2 px-2 text-blue-400 font-bold cursor-pointer hover:text-white"` : `class="py-2 px-2 text-white"`;
            sqHtml += `<tr class="hover:bg-gray-800"><td ${clickAttr}>${s.callsign}</td><td class="py-2 px-2 text-gray-400">${s.airport_display}</td><td class="py-2 px-2 text-right ${c}">${s.mins}m</td></tr>`;
        });
        document.getElementById('ops-squatters').innerHTML = sqHtml || `<tr><td colspan="3" class="p-4 text-center text-gray-500">No squatters.</td></tr>`;

        const resTurn = await fetch(`/api/ops/turnarounds?airport=${f.airport}&airline=${f.airline}`);
        const tData = await resTurn.json();
        charts['turnaroundChart'].data.labels = tData.map(d => d.airline_display);
        charts['turnaroundChart'].data.rawCodes = tData.map(d => d.airline);
        charts['turnaroundChart'].data.datasets = [{ label: 'Avg Mins', data: tData.map(d => d.time), backgroundColor: '#a855f7', hoverBackgroundColor: '#d8b4fe', borderRadius: 4, minBarLength: 6 }];
        charts['turnaroundChart'].update();

        const resDem = await fetch(`/api/ops/runway_demand?airport=${f.airport}`);
        const demData = await resDem.json();
        charts['runwayDemandChart'].data.labels = demData.map(d => d.hour_bucket);
        charts['runwayDemandChart'].data.datasets = [{ type: 'line', label: 'Max Capacity', data: Array(demData.length).fill(40), borderColor: '#ef4444', borderDash: [5, 5], fill: false, pointRadius: 0 }, { type: 'bar', label: 'Arrivals', data: demData.map(d => d.arrivals), backgroundColor: '#3b82f6', borderRadius: 4 }];
        charts['runwayDemandChart'].update();

        const resFleet = await fetch(`/api/ops/fleet_utilization?airline=${f.airline}`);
        const fleetData = await resFleet.json();
        charts['fleetChart'].data.datasets = [{ 
            label: 'Airframes', 
            data: fleetData.map(d => ({ 
                x: d.flights + (Math.random() * 0.4 - 0.2), 
                y: d.hours + (Math.random() * 0.4 - 0.2), 
                realX: d.flights,
                realY: d.hours,
                hex: d.hex 
            })), 
            backgroundColor: 'rgba(16, 185, 129, 0.6)' 
        }];
        charts['fleetChart'].update();

        const resOtp = await fetch(`/api/ops/otp?airport=${f.airport}&airline=${f.airline}`);
        const otpData = await resOtp.json();
        charts['otpChart'].data.labels = otpData.map(d => d.airline_display);
        charts['otpChart'].data.rawCodes = otpData.map(d => d.airline); 
        charts['otpChart'].data.datasets = [{ label: 'Avg Delay (Mins)', data: otpData.map(d => d.delay), backgroundColor: otpData.map(d => d.delay > 15 ? '#ef4444' : (d.delay > 0 ? '#f59e0b' : '#10b981')), borderRadius: 4, minBarLength: 6 }];
        charts['otpChart'].update();
    } catch (e) {}
}

async function fetchExec() {
    if (!charts['safetyChart']) return;
    const f = getFilters();
    try {
        const resSafe = await fetch('/api/exec/safety');
        const sData = await resSafe.json();
        charts['safetyChart'].data.labels = sData.map(d => d.date);
        charts['safetyChart'].data.datasets = [{ data: sData.map(d => d.incidents), borderColor: '#ef4444', backgroundColor: 'rgba(239, 68, 68, 0.1)', fill: true, pointBackgroundColor: '#ef4444' }];
        charts['safetyChart'].update();

        const resRoutes = await fetch('/api/exec/routes');
        const routes = await resRoutes.json();
        let rHtml = '';
        routes.forEach(r => {
            let clickStr = `onclick="openDrillDownModal('route', '${r.origin}|${r.destination}', '${r.origin_display} ✈️ ${r.destination_display}')"`;
            rHtml += `<tr class="hover:bg-gray-800 cursor-pointer transition" ${clickStr}><td class="py-3 font-bold text-blue-400">${r.origin_display}</td><td class="py-3 font-bold text-blue-400">${r.destination_display}</td><td class="py-3 text-right text-white">${r.flights}</td></tr>`;
        });
        document.getElementById('exec-routes').innerHTML = rHtml;

        const resCdo = await fetch('/api/exec/approach_efficiency');
        const cdoData = await resCdo.json();
        charts['cdoChart'].data.labels = cdoData.map(d => d.airline_display);
        charts['cdoChart'].data.rawCodes = cdoData.map(d => d.airline);
        charts['cdoChart'].data.datasets = [{ label: 'Avg Mins', data: cdoData.map(d => d.time), backgroundColor: '#10b981', borderRadius: 4, minBarLength: 6 }];
        charts['cdoChart'].update();

        const resUns = await fetch(`/api/exec/unscheduled?airport=${f.airport}`);
        const unsData = await resUns.json();
        let uHtml = '';
        unsData.forEach(u => {
            let clickAttr = u.hex_id ? `onclick="openForensicsModal('${u.hex_id}', '${u.callsign}')" class="py-2 font-bold text-blue-400 cursor-pointer hover:text-white"` : `class="py-2 font-bold text-white"`;
            uHtml += `<tr><td class="py-2 text-gray-500">${u.time}</td><td ${clickAttr}>${u.callsign}</td><td class="py-2">${u.airport_display}</td></tr>`;
        });
        document.getElementById('exec-unscheduled').innerHTML = uHtml || `<tr><td colspan="3" class="p-4 text-center text-gray-500">No ghost flights.</td></tr>`;

        const resTrain = await fetch(`/api/exec/training?airport=${f.airport}`);
        const trData = await resTrain.json();
        let trHtml = '';
        trData.forEach(t => trHtml += `<tr class="hover:bg-gray-800"><td class="py-2 font-bold text-white px-2">${t.airport_display}</td><td class="py-2 text-right text-blue-400 px-2">${t.tg_count}</td></tr>`);
        document.getElementById('exec-training').innerHTML = trHtml || `<tr><td colspan="2" class="p-4 text-center text-gray-500">No training data recorded.</td></tr>`;
    } catch (e) {}
}

// --- INITIALIZE (🌟 Bulletproof Loading Sequence) ---
// Add resize handler for mobile
window.addEventListener('resize', () => {
    if (map) map.updateSize();
    if (fullscreenMap) fullscreenMap.updateSize();
});

// Force update map size when visibility changes
const observer = new MutationObserver((mutations) => {
    mutations.forEach((mutation) => {
        if (mutation.target.id === 'view-atc' && !mutation.target.classList.contains('hidden')) {
            setTimeout(() => {
                if (map) map.updateSize();
            }, 300);
        }
    });
});

function setTodayAsDefaultDate() {
    const today = new Date();
    const yyyy = today.getFullYear();
    const mm = String(today.getMonth() + 1).padStart(2, '0');
    const dd = String(today.getDate()).padStart(2, '0');
    const dateStr = `${yyyy}-${mm}-${dd}`;
    
    if (!document.getElementById('arr-date-picker').value) {
        document.getElementById('arr-date-picker').value = dateStr;
        document.getElementById('dep-date-picker').value = dateStr;
    }
}

async function init() {
    // Load frontend config first (before any polling starts)
    await loadFrontendConfig();

    // Load tar1090 aircraft type database
    loadAircraftDB();

    setTodayAsDefaultDate();

    // Wait for both Leaflet and Chart.js CDNs to download and build before fetching API data
    await Promise.all([initMainMap(), initCharts()]);

    await loadFilterOptions();
    applyFilters();

    // Initialize WebSocket for radar if enabled
    console.log('[WS] init() calling startRadarLoop()...');
    startRadarLoop();

    // Load delay predictions
    fetchDelayPredictions();
}

init();

// --- AUTO-REFRESH LOOP ---
let fetchATCTimer = null;
function startFetchATC() {
    // Only poll REST API if WebSocket is not enabled for ATC
    if (WS_USE_FOR_ATC && WS_ENABLED) {
        console.log('[WS] ATC using WebSocket - skipping REST polling');
        return;
    }
    if (fetchATCTimer) clearInterval(fetchATCTimer);
    fetchATCTimer = setInterval(fetchATC, FRONTEND_CONFIG.atc_poll_interval_ms);
}
startFetchATC();   

// Global polling removed - switchTab() handles per-tab polling
// Ops and Exec only poll when their tabs are active

/* --- AI auto-refresh (disabled by default) --- 
setInterval(() => {
    const arrView = document.getElementById('view-arr');
    const depView = document.getElementById('view-dep');
    const aiView = document.getElementById('view-ai');
    
    if ((arrView && !arrView.classList.contains('hidden')) || 
        (depView && !depView.classList.contains('hidden'))) {
        fetchSchedules();
    }
    
    if (aiView && !aiView.classList.contains('hidden')) {
        fetchAIOperations();
    }
}, 30000);
*/
// =====================================================================
// 🌟 NEW: FLOATING WEB CHAT ASSISTANT LOGIC
// =====================================================================
let chatOpen = false;

function toggleChat() {
    const chatWindow = document.getElementById('web-chat-window');
    const chatToggleBtn = document.getElementById('chat-toggle');
    chatOpen = !chatOpen;
    
    if (chatOpen) {
        chatWindow.classList.remove('hidden');
        chatToggleBtn.classList.add('scale-0'); // Shrink FAB
        setTimeout(() => chatToggleBtn.classList.add('hidden'), 200);
        document.getElementById('chat-input').focus();
        
        // Scroll to bottom when opened
        const messagesDiv = document.getElementById('chat-messages');
        messagesDiv.scrollTop = messagesDiv.scrollHeight;
    } else {
        chatWindow.classList.add('hidden');
        chatToggleBtn.classList.remove('hidden');
        setTimeout(() => chatToggleBtn.classList.remove('scale-0'), 10); // Grow FAB
    }
}

function handleChatEnter(event) {
    if (event.key === 'Enter') {
        sendChatMessage();
    }
}

async function sendChatMessage() {
    const inputEl = document.getElementById('chat-input');
    const text = inputEl.value.trim();
    if (!text) return;

    inputEl.value = '';
    
    const messagesDiv = document.getElementById('chat-messages');
    const loadingDiv = document.getElementById('chat-loading');

    // Append User Message to UI
    messagesDiv.innerHTML += `
        <div class="flex items-start gap-3 justify-end mt-4">
            <div class="bg-blue-600 text-white p-3 rounded-xl rounded-tr-sm shadow-sm max-w-[85%] text-sm">
                ${text}
            </div>
            <div class="w-8 h-8 rounded-full bg-gray-800 text-gray-400 flex items-center justify-center shrink-0 border border-gray-700">
                <i class="fa-solid fa-user"></i>
            </div>
        </div>
    `;
    messagesDiv.scrollTop = messagesDiv.scrollHeight;

    // Show Typing Indicator
    loadingDiv.classList.remove('hidden');
    messagesDiv.scrollTop = messagesDiv.scrollHeight;

    try {
        // Generate a random session ID if needed, or use a static one for the browser session
        let sessionId = sessionStorage.getItem('raga_chat_session');
        if (!sessionId) {
            sessionId = 'web_user_' + Math.random().toString(36).substring(2, 9);
            sessionStorage.setItem('raga_chat_session', sessionId);
        }

        // Call the new FastAPI Endpoint
        const res = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: text, session_id: sessionId })
        });
        
        const data = await res.json();
        const aiResponse = data.response || "⚠️ No response received.";

        // Hide Typing Indicator
        loadingDiv.classList.add('hidden');

        // Clean up basic Telegram HTML for the Web (Replace \n with <br>)
        let cleanHtml = aiResponse.replace(/\n/g, '<br>');

        // Append AI Message to UI
        messagesDiv.innerHTML += `
            <div class="flex items-start gap-3 mt-4">
                <div class="w-8 h-8 rounded-full bg-blue-900/50 text-blue-400 flex items-center justify-center shrink-0 border border-blue-500/30 shadow-[0_0_10px_rgba(59,130,246,0.3)]">
                    <i class="fa-solid fa-robot"></i>
                </div>
                <div class="bg-gray-800 text-gray-200 p-3 rounded-xl rounded-tl-sm border border-gray-700 shadow-sm text-sm chat-msg max-w-[85%]">
                    ${cleanHtml}
                </div>
            </div>
        `;
        messagesDiv.scrollTop = messagesDiv.scrollHeight;

    } catch (e) {
        console.error("Chat API Error:", e);
        loadingDiv.classList.add('hidden');
        messagesDiv.innerHTML += `
            <div class="flex items-start gap-3 mt-4">
                <div class="w-8 h-8 rounded-full bg-red-900/50 text-red-400 flex items-center justify-center shrink-0 border border-red-500/30">
                    <i class="fa-solid fa-triangle-exclamation"></i>
                </div>
                <div class="bg-red-900/20 text-red-400 p-3 rounded-xl rounded-tl-sm border border-red-800/50 shadow-sm text-sm">
                    ⚠️ Network error connecting to Core Engine.
                </div>
            </div>
        `;
        messagesDiv.scrollTop = messagesDiv.scrollHeight;
    }
}

// =====================================================================
// 🌟 NEW: NATIVE BROWSER PUSH NOTIFICATIONS (VAPID)
// =====================================================================

function urlB64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - base64String.length % 4) % 4);
    const base64 = (base64String + padding).replace(/\-/g, '+').replace(/_/g, '/');
    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; ++i) {
        outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
}

function addSystemChatMsg(text) {
    const messagesDiv = document.getElementById('chat-messages');
    messagesDiv.innerHTML += `
        <div class="flex items-start gap-3 mt-4">
            <div class="w-8 h-8 rounded-full bg-blue-900/50 text-blue-400 flex items-center justify-center shrink-0 border border-blue-500/30 shadow-[0_0_10px_rgba(59,130,246,0.3)]">
                <i class="fa-solid fa-robot"></i>
            </div>
            <div class="bg-gray-800 text-gray-200 p-3 rounded-xl rounded-tl-sm border border-gray-700 shadow-sm text-sm chat-msg max-w-[85%]">
                ${text}
            </div>
        </div>
    `;
    messagesDiv.scrollTop = messagesDiv.scrollHeight;
}

async function enableWebPush() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
        addSystemChatMsg("⚠️ Your browser does not support Web Push Notifications.");
        return;
    }

    try {
        const permission = await Notification.requestPermission();
        if (permission !== 'granted') {
            addSystemChatMsg("⚠️ You must allow notification permissions in your browser to receive alerts.");
            return;
        }

        // Register the background Service Worker
        const swReg = await navigator.serviceWorker.register('/sw/service-worker.js');
        
        // Fetch your server's VAPID Public Key
        const pkRes = await fetch('/api/push/public_key');
        const pkData = await pkRes.json();
        
        if (!pkData.public_key) {
            addSystemChatMsg("⚠️ Web Push alerts are currently disabled on the server backend.");
            return;
        }

        // Generate the Subscription Token
        const subscription = await swReg.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: urlB64ToUint8Array(pkData.public_key)
        });

        // Send the Token to your Enterprise Database
        let sessionId = sessionStorage.getItem('raga_chat_session');
        if (!sessionId) {
            sessionId = 'web_user_' + Math.random().toString(36).substring(2, 9);
            sessionStorage.setItem('raga_chat_session', sessionId);
        }

        const subRes = await fetch('/api/push/subscribe', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sessionId, sub_data: subscription.toJSON() })
        });

        if (subRes.ok) {
            addSystemChatMsg("✅ <b>Web Alerts Enabled!</b><br><br>You will now receive native desktop/mobile popups when you set flight alerts, even if you switch tabs!");
        } else {
            addSystemChatMsg("⚠️ Error saving subscription to database.");
        }
    } catch (e) {
        console.error("Web Push Error:", e);
        addSystemChatMsg("⚠️ Failed to setup Web Push: " + e.message);
    }
}

// ============================
// FULLSCREEN RADAR MODE
// ============================
let radarFullscreen = false;
let radarAircraft = {};
let radarSortCol = 'callsign';
let radarSortAsc = true;
let radarCtx = null;
let radarLoop = null;
let fullscreenMap = null;
let fullscreenAircraftLayer = null;
let fullscreenOlInitialized = false;
let fullscreenFeatureCache = {};

function toggleFullscreen() {
    radarFullscreen = !radarFullscreen;
    const radarDiv = document.getElementById('fullscreen-radar');

    if (radarFullscreen) {
        radarDiv.classList.remove('hidden');

        // Initialize panel state based on screen size
        fullscreenPanelOpen = window.innerWidth > 768;
        const panel = document.getElementById('fullscreen-panel');
        const mapContainer = document.getElementById('fullscreen-map-container');
        if (panel) {
            panel.style.transform = fullscreenPanelOpen ? 'translateX(0)' : 'translateX(-100%)';
        }
        if (mapContainer) {
            mapContainer.style.left = (fullscreenPanelOpen && window.innerWidth > 768) ? '320px' : '0';
        }

        // Initialize map if not done
        if (!fullscreenOlInitialized) {
            setTimeout(initFullscreenMap, 100);
        } else {
            setTimeout(() => fullscreenMap?.updateSize(), 200);
        }
    } else {
        radarDiv.classList.add('hidden');
        setTimeout(() => map?.updateSize(), 200);
    }
}

let fullscreenPanelOpen = window.innerWidth > 768;

function toggleFullscreenPanel() {
    const panel = document.getElementById('fullscreen-panel');
    const mapContainer = document.getElementById('fullscreen-map-container');
    const toggleBtn = document.getElementById('fs-panel-toggle');
    const isMobile = window.innerWidth <= 768;

    fullscreenPanelOpen = !fullscreenPanelOpen;

    if (fullscreenPanelOpen) {
        panel.style.transform = 'translateX(0)';
        if (mapContainer && !isMobile) mapContainer.style.left = '320px';
    } else {
        panel.style.transform = 'translateX(-100%)';
        if (mapContainer && !isMobile) mapContainer.style.left = '0';
    }
    if (toggleBtn) toggleBtn.innerHTML = '<i class="fa-solid fa-bars"></i>';
    setTimeout(() => fullscreenMap?.updateSize(), 300);
}

async function initFullscreenMap() {
    const container = document.getElementById('fullscreen-map-container');
    if (!container || typeof ol === 'undefined') {
        console.warn('Fullscreen container or OL not ready');
        setTimeout(initFullscreenMap, 500);
        return;
    }

    try {
        const view = new ol.View({
            center: ol.proj.fromLonLat([78.9629, 20.5937]),
            zoom: 5,
            minZoom: 2,
            maxZoom: 15
        });

        fullscreenMap = new ol.Map({
            target: container,
            view: view,
            controls: ol.control.defaults.defaults({ zoom: false, rotate: false }).extend([
                new ol.control.Zoom(),
                new ol.control.ScaleLine()
            ])
        });

        // Default dark layer for fullscreen
        const darkLayer = new ol.layer.Tile({
            source: new ol.source.XYZ({
                url: 'https://{a-d}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
                maxZoom: 19
            })
        });
        fullscreenMap.addLayer(darkLayer);

        fullscreenAircraftLayer = new ol.layer.Vector({
            source: new ol.source.Vector(),
            style: (feature) => {
                const alt = feature.get('alt') || 0;
                const rotation = feature.get('rotation') || 0;
                const zoom = fullscreenMap.getView().getZoom() || 5;
                const typeCode = feature.get('typeCode') || '';
                const hex = feature.get('hexid') || '';
                return getAircraftStyle(alt, rotation, zoom, feature.get('selected') || false, typeCode, hex);
            }
        });
        fullscreenMap.addLayer(fullscreenAircraftLayer);

        // Interactions
        fullscreenMap.on('pointermove', function(evt) {
            const features = fullscreenMap.getFeaturesAtPixel(evt.pixel);
            if (features && features.length > 0) {
                const f = features[0];
                const props = f.getProperties();
                showAircraftPopup(props, evt.pixel);
            } else {
                document.getElementById('fs-radar-popup').classList.add('hidden');
            }
        });

        fullscreenMap.on('click', function(evt) {
            const features = fullscreenMap.getFeaturesAtPixel(evt.pixel);
            if (features && features.length > 0) {
                const f = features[0];
                const props = f.getProperties();
                showAircraftPopup(props, evt.pixel);
                highlightFullscreenAircraft(props.hexid);
            }
        });

        fullscreenOlInitialized = true;
        console.log('Fullscreen OL map initialized (tar1090 style)');
    } catch(e) {
        console.error('Fullscreen map init failed:', e);
    }
}

function updateFullscreenAircraft(flights) {
    if (!fullscreenOlInitialized || !fullscreenAircraftLayer || !fullscreenMap) return;

    const source = fullscreenAircraftLayer.getSource();
    const currentHexIds = new Set();
    const hexIds = flights.map(fl => (fl.hexid || fl.hex || '')).filter(h => h);

    fetchAircraftTypes(hexIds).then(() => {
        flights.forEach(fl => {
            fixLatLon(fl);
            if (!fl.lat || !fl.lon) return;
            const hex = fl.hexid || fl.hex;
            if (!hex) return;
            currentHexIds.add(hex);

            const coord = ol.proj.fromLonLat([parseFloat(fl.lon), parseFloat(fl.lat)]);
            const rotation = (parseFloat(fl.heading) || 0) * Math.PI / 180;
            const alt = parseFloat(fl.alt) || 0;
            const speed = parseFloat(fl.speed) || 0;
            const typeCode = (aircraftDB[hex] && aircraftDB[hex].type) || fl.ac_type || '';

            let feature = fullscreenFeatureCache[hex];

            if (feature) {
                feature.getGeometry().setCoordinates(coord);
                feature.set('alt', alt);
                feature.set('speed', speed);
                feature.set('rotation', rotation);
                feature.set('origin', fl.origin || '');
                feature.set('destination', fl.destination || '');
                feature.set('typeCode', typeCode);
            } else {
                feature = new ol.Feature({
                    geometry: new ol.geom.Point(coord),
                    hexid: hex,
                    callsign: fl.callsign || '',
                    alt: alt,
                    speed: speed,
                    rotation: rotation,
                    typeCode: typeCode,
                    origin: fl.origin || '',
                    destination: fl.destination || ''
                });
                source.addFeature(feature);
                fullscreenFeatureCache[hex] = feature;
            }
        });

        Object.keys(fullscreenFeatureCache).forEach(hex => {
            if (!currentHexIds.has(hex)) {
                source.removeFeature(fullscreenFeatureCache[hex]);
                delete fullscreenFeatureCache[hex];
            }
        });

        fullscreenAircraftLayer.changed();
        const countEl = document.getElementById('fullscreen-radar-count');
        if (countEl) countEl.innerText = flights.length + ' aircraft';

        updateFullscreenAircraftList();
    });
}

// Fullscreen sidebar aircraft list
let fsSortCol = 'callsign';
let fsSortAsc = true;
let fsSelectedHex = null;

function updateFullscreenAircraftList() {
    const tbody = document.getElementById('fs-aircraft-list');
    if (!tbody) return;

    let list = Object.values(radarAircraft);
    const search = document.getElementById('fs-search')?.value?.toUpperCase() || '';
    if (search) {
        list = list.filter(ac => (ac.callsign || ac.hex).toUpperCase().includes(search));
    }

    list.sort((a, b) => {
        let valA = a[fsSortCol] || 0;
        let valB = b[fsSortCol] || 0;
        if (typeof valA === 'string') valA = valA.toUpperCase();
        if (typeof valB === 'string') valB = valB.toUpperCase();
        if (fsSortAsc) return valA > valB ? 1 : -1;
        return valA < valB ? 1 : -1;
    });

    tbody.innerHTML = list.map(ac => {
        const color = getAltitudeColor(ac.alt);
        const isSelected = ac.hex === fsSelectedHex;
        const dbEntry = aircraftDB[ac.hex];
        const typeBadge = dbEntry && dbEntry.type
            ? `<span class="ml-1 text-[9px] bg-gray-700 px-1 rounded text-gray-400">${dbEntry.type}</span>`
            : '';
        return `
            <tr class="hover:bg-gray-800 cursor-pointer transition ${isSelected ? 'bg-gray-800/80' : ''}" data-hex="${ac.hex}" onclick="highlightFullscreenAircraft('${ac.hex}')">
                <td class="py-1.5 px-2 font-bold text-xs" style="color:${color}">${ac.callsign || ac.hex}${typeBadge}</td>
                <td class="py-1.5 px-1 text-right text-xs font-mono">${(ac.alt || 0).toLocaleString()}</td>
                <td class="py-1.5 px-1 text-right text-xs font-mono">${ac.gs || 0}</td>
                <td class="py-1.5 px-1 text-right text-xs font-mono">${ac.heading || 0}°</td>
            </tr>
        `;
    }).join('');
}

function sortFullscreenTable(col) {
    if (fsSortCol === col) {
        fsSortAsc = !fsSortAsc;
    } else {
        fsSortCol = col;
        fsSortAsc = true;
    }
    updateFullscreenAircraftList();
}

function highlightFullscreenAircraft(hexid) {
    fsSelectedHex = hexid;
    const ac = radarAircraft[hexid];
    if (!ac) return;

    // Show details panel
    const details = document.getElementById('fs-selected-details');
    const callsignEl = document.getElementById('fs-detail-callsign');
    const hexEl = document.getElementById('fs-detail-hex');
    const altEl = document.getElementById('fs-detail-alt');
    const speedEl = document.getElementById('fs-detail-speed');
    const hdgEl = document.getElementById('fs-detail-hdg');
    const originEl = document.getElementById('fs-detail-origin');
    const destEl = document.getElementById('fs-detail-dest');

    const dbEntry = aircraftDB[hexid];
    const typeStr = dbEntry && dbEntry.type ? ` (${dbEntry.type})` : '';
    const regStr = dbEntry && dbEntry.reg ? ` [${dbEntry.reg}]` : '';

    if (details) details.classList.remove('hidden');
    if (callsignEl) callsignEl.textContent = (ac.callsign || ac.hex) + typeStr;
    if (hexEl) hexEl.textContent = ac.hex + regStr;
    if (altEl) altEl.textContent = (ac.alt || 0).toLocaleString() + ' ft';
    if (speedEl) speedEl.textContent = (ac.gs || 0) + ' kts';
    if (hdgEl) hdgEl.textContent = (ac.heading || 0) + '°';
    if (originEl) originEl.textContent = ac.origin || '---';
    if (destEl) destEl.textContent = ac.destination || '---';

    // Highlight row
    document.querySelectorAll('#fs-aircraft-list tr').forEach(tr => {
        tr.classList.toggle('bg-gray-800/80', tr.dataset.hex === hexid);
    });

    // Center map on aircraft
    if (fullscreenMap && ac.lat && ac.lon) {
        fullscreenMap.getView().animate({
            center: ol.proj.fromLonLat([ac.lon, ac.lat]),
            zoom: 10,
            duration: 500
        });
    }
}

function jumpToAircraft() {
    const hex = document.getElementById('fs-jump')?.value?.trim().toUpperCase();
    if (!hex) return;

    const ac = Object.values(radarAircraft).find(a =>
        (a.hex && a.hex.toUpperCase() === hex) ||
        (a.callsign && a.callsign.toUpperCase() === hex)
    );

    if (ac && fullscreenMap) {
        fullscreenMap.getView().animate({
            center: ol.proj.fromLonLat([ac.lon, ac.lat]),
            zoom: 11,
            duration: 500
        });
        highlightFullscreenAircraft(ac.hex);
    }
}

function initRadarCanvas() {
    // Find visible canvas - check view-atc first (default view), then fullscreen
    let canvas = document.querySelector('#view-atc #radar-canvas') || 
                 document.querySelector('#fullscreen-radar #radar-canvas') ||
                 document.getElementById('radar-canvas');
    
    if (!canvas) {
        // Canvas radar not used - radar uses OpenLayers map instead
        return false;
    }
    
    // Get the actual display size
    const rect = canvas.getBoundingClientRect();
    const width = rect.width || 800;
    const height = rect.height || 500;
    
    // Only resize if needed
    if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
    }
    
    radarCtx = canvas.getContext('2d');
    
    canvas.onclick = handleRadarClick;
    canvas.onmousemove = handleRadarHover;
    
    // Handle resize
    window.addEventListener('resize', () => {
        const rect = canvas.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) {
            canvas.width = rect.width;
            canvas.height = rect.height;
            if (Object.keys(radarAircraft).length > 0) {
                updateRadarCanvas();
            }
        }
    });
    
    console.log('Canvas initialized:', canvas.width, 'x', canvas.height);
    return !!radarCtx;
}

function startRadarLoop() {
    console.log('[WS] startRadarLoop called');
    
    // Initialize WebSocket if enabled - doesn't need canvas
    if (WS_ENABLED && WS_USE_FOR_RADAR) {
        initWebSocket();
    }
    
    // Only fetch via REST if WebSocket is disabled
    if (!WS_ENABLED) {
        fetchRadarData();
        radarLoop = setInterval(fetchRadarData, FRONTEND_CONFIG.radar_poll_interval_ms);
    } else {
        // WebSocket is enabled - no periodic REST fallback needed
        // Data comes via WebSocket in real-time
        console.log('[WS] Using WebSocket - no REST polling needed');
    }
}

function stopRadarLoop() {
    if (radarLoop) clearInterval(radarLoop);
}

async function fetchRadarData() {
    console.log('fetchRadarData called, radarCtx:', !!radarCtx);
    try {
        // Use radar endpoint with default India center + 1500 mile radius
        const res = await fetch('/api/aircraft/radar?lat=20.5937&lon=78.9629&radius=1500');
        
        if (!res.ok) {
            console.warn('Radar API failed:', res.status);
            return;
        }
        
        const flights = await res.json();
        if (!Array.isArray(flights)) {
            console.warn('Invalid radar data:', flights);
            return;
        }
        
        console.log('API response:', flights.length, 'flights');
        
        const newAircraft = {};
        flights.forEach(ac => {
            if (ac.lat && ac.lon) {
                newAircraft[ac.hexid] = {
                    hex: ac.hexid,
                    callsign: ac.callsign,
                    lat: ac.lat,
                    lon: ac.lon,
                    alt: ac.alt || 0,
                    gs: ac.speed || 0,
                    heading: ac.heading || 0,
                    origin: ac.origin || '',
                    destination: ac.destination || ''
                };
            }
        });
        
        console.log('Processed aircraft:', Object.keys(newAircraft).length);
        
        radarAircraft = newAircraft;
        
        const countEl = document.getElementById('radar-count');
        if (countEl) countEl.textContent = flights.length;
        
        // Initialize canvas if needed, then update
        if (!radarCtx) {
            initRadarCanvas();
        }
        if (radarCtx) {
            updateRadarCanvas();
        }
        // Only update list if the element exists
        const radarListEl = document.getElementById('radar-list');
        if (radarListEl) {
            updateRadarList();
        }
    } catch(e) {
        console.error('fetchRadarData error:', e);
    }
}

function updateRadarCanvas() {
    if (!radarCtx) {
        console.warn('updateRadarCanvas: radarCtx is null');
        return;
    }
    
    const canvas = radarCtx.canvas;
    const aircraftCount = Object.keys(radarAircraft).length;
    
    // Dark map background like tar1090
    radarCtx.fillStyle = '#1a1a2e';
    radarCtx.fillRect(0, 0, canvas.width, canvas.height);
    
    // Subtle grid
    radarCtx.strokeStyle = '#333355';
    radarCtx.lineWidth = 1;
    
    // Draw grid
    for (let x = 0; x < canvas.width; x += 50) {
        radarCtx.beginPath(); radarCtx.moveTo(x, 0); radarCtx.lineTo(x, canvas.height); radarCtx.stroke();
    }
    for (let y = 0; y < canvas.height; y += 50) {
        radarCtx.beginPath(); radarCtx.moveTo(0, y); radarCtx.lineTo(canvas.width, y); radarCtx.stroke();
    }
    
    // Draw each aircraft
    Object.values(radarAircraft).forEach(ac => {
        if (!ac.lat || !ac.lon) return;
        
        // Convert lat/lon to canvas coordinates
        // India roughly: lat 6-37, lon 68-98
        const minLat = 6, maxLat = 37, minLon = 68, maxLon = 98;
        
        const x = ((ac.lon - minLon) / (maxLon - minLon)) * canvas.width;
        const y = ((maxLat - ac.lat) / (maxLat - minLat)) * canvas.height;
        
        if (x < 0 || x > canvas.width || y < 0 || y > canvas.height) return;
        
        const color = getAltitudeColor(ac.alt || 0);
        const size = 10;
        const heading = (ac.heading || 0) * Math.PI / 180;
        
        // Draw triangle
        radarCtx.save();
        radarCtx.translate(x, y);
        radarCtx.rotate(heading - Math.PI/2);
        
        radarCtx.fillStyle = color;
        radarCtx.beginPath();
        radarCtx.moveTo(0, -size);
        radarCtx.lineTo(size * 0.6, size);
        radarCtx.lineTo(-size * 0.6, size);
        radarCtx.closePath();
        radarCtx.fill();
        
        radarCtx.restore();
        
        // Draw callsign
        if (ac.callsign) {
            radarCtx.fillStyle = '#00ff00';
            radarCtx.font = 'bold 11px monospace';
            radarCtx.fillText(ac.callsign, x + 12, y + 4);
        }
    });
}

function updateRadarList() {
    const tbody = document.getElementById('radar-aircraft-list');
    if (!tbody) return;  // Element doesn't exist
    
    let list = Object.values(radarAircraft);
    
    // Filter
    const search = document.getElementById('radar-search')?.value?.toUpperCase() || '';
    if (search) {
        list = list.filter(ac => (ac.callsign || '').includes(search));
    }
    
    // Sort
    list.sort((a, b) => {
        let valA = a[radarSortCol] || 0;
        let valB = b[radarSortCol] || 0;
        if (typeof valA === 'string') valA = valA.toUpperCase();
        if (typeof valB === 'string') valB = valB.toUpperCase();
        if (radarSortAsc) return valA > valB ? 1 : -1;
        return valA < valB ? 1 : -1;
    });
    
    tbody.innerHTML = list.map(ac => `
        <tr class="hover:bg-gray-800 cursor-pointer" data-hex="${ac.hex}">
            <td class="py-1 px-2">${ac.callsign || ac.hex}</td>
            <td class="py-1 px-1 text-right">${ac.alt || 0}</td>
            <td class="py-1 px-1 text-right">${ac.gs || 0}</td>
        </tr>
    `).join('');
    
    tbody.querySelectorAll('tr').forEach(row => {
        row.onclick = () => highlightAircraft(row.dataset.hex);
    });
}

function sortRadar(col) {
    if (radarSortCol === col) {
        radarSortAsc = !radarSortAsc;
    } else {
        radarSortCol = col;
        radarSortAsc = true;
    }
    updateRadarList();
}

function filterRadarList() {
    updateRadarList();
}

function handleRadarClick(e) {
    const rect = e.target.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    
    // Find clicked aircraft
    Object.values(radarAircraft).forEach(ac => {
        if (!ac.lat || !ac.lon) return;
        const acx = (ac.lon + 180) * (radarCtx.canvas.width / 360);
        const acy = (90 - ac.lat) * (radarCtx.canvas.height / 180);
        if (Math.abs(acx - x) < 15 && Math.abs(acy - y) < 15) {
            showRadarPopup(ac, acx, acy);
        }
    });
}

function handleRadarHover(e) {
    const rect = e.target.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    
    let found = null;
    Object.values(radarAircraft).forEach(ac => {
        if (!ac.lat || !ac.lon) return;
        const acx = (ac.lon + 180) * (radarCtx.canvas.width / 360);
        const acy = (90 - ac.lat) * (radarCtx.canvas.height / 180);
        if (Math.abs(acx - x) < 15 && Math.abs(acy - y) < 15) {
            found = { ac, acx, acy };
        }
    });
    
    if (found) {
        showRadarPopup(found.ac, found.acx, found.acy);
    } else {
        document.getElementById('radar-popup').classList.add('hidden');
    }
}

function showRadarPopup(ac, x, y) {
    const popup = document.getElementById('radar-popup');
    popup.innerHTML = `
        <div class="font-bold text-cyan-400 mb-1">${ac.callsign || ac.hex}</div>
        <div class="grid grid-cols-2 gap-1">
            <span class="text-gray-500">Hex:</span><span>${ac.hex}</span>
            <span class="text-gray-500">Alt:</span><span>${ac.alt || 0} ft</span>
            <span class="text-gray-500">GS:</span><span>${ac.gs || 0} kts</span>
            <span class="text-gray-500">HDG:</span><span>${ac.heading || 0}°</span>
            <span class="text-gray-500">From:</span><span>${ac.origin || '---'}</span>
            <span class="text-gray-500">To:</span><span>${ac.destination || '---'}</span>
        </div>
    `;
    popup.style.left = (x + 10) + 'px';
    popup.style.top = (y + 10) + 'px';
    popup.classList.remove('hidden');
}

function highlightAircraft(hex) {
    // Could scroll to aircraft in list or highlight on canvas
    console.log('Highlight:', hex);
}

function toggleRadarLayer() {
    // Toggle between map layers (future feature)
}