# Wingbits Flight Tracking Map — Architecture Analysis

> **Date**: 2026-05-09
> **URL**: https://wingbits.com/map?lat=18.52110&lon=73.85020&zoom=4.0
> **Purpose**: Reference for upgrading our BharatRadar maps (cortex/map) with similar features

---

## 1. Tech Stack

| Component | Technology | Notes |
|-----------|-----------|-------|
| Framework | **Next.js** (RSC — React Server Components) | i18n routing via `[locale]`, client-side rendering for map |
| Map Library | **MapLibre GL JS** | WebGL canvas-based, not Leaflet DOM-based |
| Auth | **Firebase** | `firebase.googleapis.com`, `firebaseRemoteConfig` |
| Real-time Data | **WebSocket** | Actual endpoint masked in bundled JS (`ecs-api.wingbits.com`) |
| Analytics | **PostHog** + **Google Analytics** (G-WM2P6S7YY0) | PostHog self-hosted on `eu.i.posthog.com` |
| Cookies/Tracking | **Termly** | Cookie consent banner |
| Styling | **Tailwind CSS** + custom CSS | Utility-first approach |
| Chat | **Intercom** | Messenger widget |

---

## 2. Page Layout

```
┌──────────────────────────────────────────────────────────┐
│  [Logo]  _MAP  _BUY DATA  _JOIN US  _LEARN  _MAINNET  EN│  ← Header navbar
│                                         [SIGN IN]  [☰]  │
├──────────────────────────────────────────────────────────┤
│                                                          │
│    [−]  [⊕]  [⟲]  [🗏]  [⛶]  [📍]  [3D]  [ℹ]     │  ← Right toolbar
│                                                          │
│                    MAPLIBRE GL CANVAS                    │
│                    (WebGL rendered)                      │
│                                                          │
│           ✈  IGO305Y (clicked)                           │
│              ┌──────────────┐                            │
│              │ IGO305Y      │                            │  ← Popup overlay
│              │ 36,000 ft    │                            │
│              │ 335°         │                            │
│              └──────────────┘                            │
│                                                          │
│    [Bottom dock — hidden at certain zooms/states]        │
├──────────────────────────────────────────────────────────┤
│  ♟ Map data © OpenStreetMap                 [Attribution]│
└──────────────────────────────────────────────────────────┘
```

---

## 3. Map Rendering (MapLibre GL JS)

### 3.1 Architecture
- **Renders to a single WebGL canvas** (`<canvas class="maplibregl-canvas">`)
- Canvas dimensions: 1200×888px (full viewport after header)
- No individual DOM elements for aircraft (unlike Leaflet markers)
- Map instance stored in React component state (not accessible from `window`)

### 3.2 DOM Structure
```
div.maplibregl-map
├── div.maplibregl-canvas-container  (interactive)
│   └── canvas.maplibregl-canvas     (WebGL rendering)
├── div.maplibregl-control-container
│   ├── div.maplibregl-ctrl-top-left
│   ├── div.maplibregl-ctrl-top-right
│   ├── div.maplibregl-ctrl-bottom-left
│   └── div.maplibregl-ctrl-bottom-right
├── div (3D overlay — custom, placed over canvas)
└── div.maplibregl-popup (appears on aircraft click)
    ├── div.maplibregl-popup-tip
    └── div.maplibregl-popup-content
```

### 3.3 Map Controls
A custom right-side toolbar with 8 icon buttons (48×48px each), top-to-bottom:

| # | Icon | Action | Notes |
|---|------|--------|-------|
| 0 | `−` | Zoom out | SVG minus icon |
| 1 | `⊕` | Zoom in | SVG plus icon |
| 2 | `⟲` | Compass / Reset north | SVG compass icon |
| 3 | `🗏` | Layer / Style switcher | Has green indicator dot |
| 4 | `🤖` | **AI** | Opens AI assistant panel |
| 5 | `⚙` | **Preferences** | Opens settings panel (gear icon) |
| 6 | `🏔` | **3D toggle** | "Switch to 3D view" title |
| 7 | `ℹ` | Info / About | SVG info icon |

These are NOT the default MapLibre controls — they are custom React components that call the MapLibre API internally.

### 3.4 Preferences Panel
Opened by clicking the gear icon (index 5). Panel slides in from the left (380px wide, full height minus header):

```
Preferences
├── Color by             [None] [Altitude] [Category]
│   └── (radio-style toggle buttons)
├── Altitude filter (ft)
│   └── [range slider]
└── Show aircraft type
    ├── [Reset] [See all]   (action buttons)
    ├── Light Aircraft      ─ toggle chip
    ├── Small Aircraft      ─ toggle chip
    ├── Large Aircraft      ─ toggle chip
    ├── High Vortex Large   ─ toggle chip
    ├── Heavy Aircraft      ─ toggle chip
    ├── Fighter/Aerobatic   ─ toggle chip
    ├── Helicopters         ─ toggle chip
    ├── Gliders             ─ toggle chip
    ├── Balloons            ─ toggle chip
    ├── UAV/Drones          ─ toggle chip
    └── Emergency Vehicle   ─ toggle chip
```

- Toggle chips filter which aircraft types are shown on the map
- `Color by` determines the coloring scheme for aircraft markers on the map
- `Altitude filter` uses a numeric range slider
- Settings persist via localStorage (observed keys: `map.showFlights`, `map.showStationCoverage`, etc.)

---

## 4. Aircraft Rendering

### 4.1 On the Map
- Aircraft are rendered as **MapLibre vector layers** on the WebGL canvas
- No HTML/CSS markers — everything is drawn via WebGL
- This enables smooth rendering of thousands of aircraft without DOM overhead

### 4.2 Click Popup (HTML Overlay)
When an aircraft is clicked, a **MapLibre popup** is rendered as HTML over the canvas:

```html
<div class="maplibregl-popup maplibregl-popup-anchor-top">
  <div class="maplibregl-popup-tip"></div>
  <div class="maplibregl-popup-content">
    <div>
      <div class="bg-[#1E1B1B]/95 text-white rounded-md shadow-lg 
                  border border-white/10 backdrop-blur-sm 
                  pointer-events-none select-none">
        <div class="px-3 py-2">
          <div class="flex items-center gap-2 mb-1">
            <span class="text-[15px] font-semibold text-white 
                         tracking-wide font-mono">IGO305Y</span>
          </div>
          <div class="flex items-baseline gap-4 text-[12px] font-mono">
            <div class="flex items-baseline gap-1">
              <span class="text-white/90">36,000</span>
              <span class="text-white/50 text-[10px]">ft</span>
            </div>
            <div class="flex items-baseline gap-1">
              <span class="text-white/90">335°</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>
```

**Popup styling:**
- Background: `bg-[#1E1B1B]/95` (near-black, 95% opacity)
- Text: white, monospace font
- Border: white/10 with rounded corners
- Backdrop blur effect
- Callsign: 15px semibold
- Altitude/heading: 12px, smaller units at 10px with `text-white/50`

### 4.3 Aircraft Data Format
Not directly observable, but from the popup:
- Callsign (`IGO305Y`)
- Altitude in feet (`36,000`)
- Heading in degrees (`335°`)

---

## 5. Smooth Aircraft Movement

### 5.1 Mechanism
The smooth animation is achieved through **MapLibre GL JS's native interpolation**:

- Aircraft positions are updated via `source.setData()` (likely GeoJSON)
- MapLibre interpolates between old and new positions using its **render loop**
- When a source's data changes, MapLibre re-renders the next frame with interpolated positions
- This is GPU-accelerated via WebGL

### 5.2 Key Differences from Our Current Approach

| Aspect | Wingbits (MapLibre) | Our Radar (Current) |
|--------|--------------------|--------------------|
| Rendering | WebGL canvas | DOM elements (divs) |
| Aircraft | Vector layer on canvas | Individual `<div>` blips |
| Position update | `source.setData(geojson)` → auto-interpolate | `requestAnimationFrame` + manual alpha decay |
| Smooth movement | Built-in GPU interpolation | Manual frame-based interpolation |
| Scale | Handles thousands easily | DOM-bound (~hundreds max) |
| Label rendering | Canvas text (performant) | CSS text (slow with many) |

### 5.3 3D View
The 3D toggle button switches to MapLibre's 3D terrain/pitch view:
- `map.setPitch(60)` or similar
- `map.setBearing(...)` for rotation
- Aircraft still rendered on the 3D surface

---

## 6. Data Flow

### 6.1 API Endpoints
| Endpoint | Purpose |
|----------|---------|
| `ecs-api.wingbits.com/v1/auth/me` | Auth status check |
| `ecs-api.wingbits.com/v1/auth/user-metadata` | User profile/metadata |
| `firebase.googleapis.com/v1alpha/.../webConfig` | Firebase config |
| `firebaseremoteconfig.googleapis.com/...` | Firebase remote config |

### 6.2 Real-time Aircraft Data
- Uses **WebSocket** for live aircraft position streaming
- Not directly visible (bundled in Next.js chunks)
- Likely pushes GeoJSON FeatureCollection updates over WebSocket
- The WebSocket connection is to `ecs-api.wingbits.com` or a separate subdomain

### 6.3 Authentication
- **Firebase Authentication** (Google, email/password)
- Session check via `ecs-api.wingbits.com/v1/auth/me`
- User metadata fetched separately

---

## 7. Settings / Preferences

### 7.1 Storage
All settings are stored in **localStorage** with a `map.` prefix:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `map.showFlights` | boolean | `true` | Toggle aircraft visibility |
| `map.showStationCoverage` | boolean | `true` | Show/hide coverage circles |
| `map.showCustomMarker` | JSON | `{show:false,...}` | Custom map marker |
| `map.isDockVisible` | boolean | `true` | Bottom info dock |
| `map.isFullscreen` | boolean | `false` | Fullscreen mode |
| `map.isSalesFunnelClosed` | boolean | `true` | Sales modal dismissed |
| `map.colorBy` | string | `"none"` | Color scheme: `"none"`, `"altitude"`, or `"category"` |
| `map.altitudeRange` | array | `[0,60000]` | Altitude filter range `[min, max]` in ft |
| `map.hiddenAircraftTypes` | array | `[]` | Array of hidden aircraft type strings |

### 7.2 Preferences Panel
Opened via the gear icon (index 5) in the right toolbar. Panel content:

- **Color by** — `None` / `Altitude` / `Category` radio-style toggle buttons
- **Altitude filter (ft)** — range slider (min-max)
- **Show aircraft type** — toggle chips for each category (Light Aircraft, Small Aircraft, Large Aircraft, High Vortex Large, Heavy Aircraft, Fighter/Aerobatic, Helicopters, Gliders, Balloons, UAV/Drones, Emergency Vehicle), with `[Reset]` and `[See all]` action buttons

### 7.3 UI Controls
Settings are accessed through:
1. **Hamburger menu** (☰) — opens navigation sidebar with links
2. **Layer button** (🗏 — index 3 in right toolbar) — opens a layer/style switcher
3. **Preferences panel** (⚙ — index 5 in right toolbar) — dedicated settings panel for color, altitude, and aircraft type filters

### 7.3 Hamburger Menu Structure
```
☰ → Navigation Sidebar
├── en (Language selector)
├── _Map
├── _Buy Data
│   ├── Pricing
│   ├── Live flight data
│   ├── Historical flight data
│   ├── TCAS Alerts
│   ├── GPS Jamming Map
│   └── Data samples
├── _Join us and earn
│   ├── Get Started
│   ├── Supported Hardware
│   └── Rewards hub
└── _Learn
    └── Docs
```

---

## 8. Frontend Bundle Architecture

### 8.1 Key JS Chunks (from Next.js)
| Chunk | Size/Type | Content |
|-------|-----------|---------|
| `main-app-e4dfb133640b0095.js` | Core app | Map component, navigation, layout |
| `app/layout-6a6923bd16a02d9f.js` | Layout | Root layout with i18n |
| `app/[locale]/(public)/layout-...` | Layout | Public pages layout |
| `app/[locale]/(users)/layout-...` | Layout | Authenticated users layout |
| `webpack-1995dfa193effa8c.js` | Build tool | Webpack runtime |
| Many `chunks/XXX-*.js` | Pages & components | Route-based code splitting |

### 8.2 External Services Loaded
- `challenges.cloudflare.com` — Cloudflare Turnstile (captcha)
- `www.googletagmanager.com` — GTM
- `eu.i.posthog.com` — PostHog analytics
- `app.termly.io` — Cookie consent
- `widget.intercom.io` — Chat widget

---

## 9. Key Insights for Our Implementation

### 9.1 What to Adopt
1. **MapLibre GL over Leaflet** — If we want smooth rendering at scale, switch from Leaflet DOM markers to MapLibre canvas rendering. This allows thousands of aircraft without DOM overhead.
2. **MapLibre Popup for aircraft info** — The popup design (dark semi-transparent, blur backdrop, monospace text) is clean and informative. We can replicate this in Leaflet with custom popups.
3. **localStorage settings** — Simple and effective pattern for UI state persistence. We already use this pattern but can extend it.
4. **Layer switcher pattern** — Control visibility of flights, coverage, custom markers through a layer panel rather than separate controls.
5. **WebSocket + GeoJSON** for real-time data — We already do this partially (Redis → API), but a direct WebSocket push would reduce latency.

### 9.2 What to Keep
1. **Our PPI radar** is specialized for the homepage and serves a different purpose. Keep it as-is for the radar scope visualization.
2. **Leaflet for the main map** (map.bharatradar.com) is fine for our current scale. Only consider MapLibre if we hit performance issues with many aircraft simultaneously visible.

### 9.3 Priority Features for Our Map
1. **Bottom dock** — Show aircraft count, stats, recent alerts in a persistent bottom bar
2. **3D toggle** — MapLibre supports this natively
3. **Station coverage overlay** — Show coverage circles for each feeder
4. **GPS jamming overlay** — Wingbits has a dedicated GPS jamming map layer
5. **Layer controls** — Toggle flights, coverage, markers from a single panel

---

## 10. Appendix: CSS Class Reference

### 10.1 Map Container
```
.maplibregl-map              → Main map container
.maplibregl-canvas-container → Interactive canvas wrapper
.maplibregl-canvas           → WebGL canvas
.maplibregl-control-container → Controls container
.maplibregl-popup            → Aircraft info popup
.maplibregl-popup-content    → Popup inner content
```

### 10.2 Custom UI Components
```
.hamburger-btn               → Hamburger menu button
.hamburger-border            → Hamburger button wrapper
.w-48.h-48.min-w-48         → Map control buttons (48×48px)
.bg-black.flex-1.relative    → Main content area
.content-[text]              → Typography component
.font-secondary              → Secondary font
.text-small / .text-tiny     → Font size variants
.cta-btn                     → Call-to-action button
```

### 10.3 Aircraft Popup
```
.bg-[#1E1B1B]/95            → Near-black 95% opacity background
.backdrop-blur-sm            → Blur effect
.text-[15px].font-semibold   → Callsign text
.text-white/90               → Altitude/heading value text
.text-white/50               → Unit text ("ft", "°")
.font-mono                   → Monospace font for data
