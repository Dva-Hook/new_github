# V5 Desktop UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the existing standalone V5 prototype with a compact Minecloud-inspired desktop interface that supports three product pages, an overlay navigation drawer, selected-task details, and persistent light/dark themes.

**Architecture:** Keep the prototype framework-free and isolated in `v5_desktop_ui`. Semantic HTML defines the product shell and views, CSS tokens drive light/dark themes and responsive layouts, and one small JavaScript state controller manages navigation, selected batches, drawers, dialogs, and local persistence.

**Tech Stack:** HTML5, CSS custom properties, vanilla JavaScript, Lucide UMD icons, Node syntax checks, Playwright browser verification.

---

## File Responsibilities

- `v5_desktop_ui/index.html`: semantic application shell, three product pages, Registration Center views, task detail panel, dialogs, and accessible controls.
- `v5_desktop_ui/styles.css`: design tokens, light/dark themes, compact rail and overlay drawer, central workspace, detail panel, tables, forms, empty states, and responsive behavior.
- `v5_desktop_ui/app.js`: local prototype state, navigation, drawer behavior, theme persistence, preset selection, batch selection, detail rendering, and mock feedback.
- `v5_desktop_ui/README.md`: scope, direct-open instructions, supported interactions, and explicit backend isolation.
- `v5_desktop_ui/screenshots/*.png`: browser-verified desktop, tablet, mobile, dark-theme, and drawer previews.

### Task 1: Rebuild the Application Shell

**Files:**
- Modify: `v5_desktop_ui/index.html`

- [ ] **Step 1: Define the three-product top navigation**

Create buttons with `data-product="registration"`, `data-product="ban-lookup"`, and `data-product="phone-binding"`. Add accessible labels for search, notification, theme, profile, and menu controls.

- [ ] **Step 2: Define the compact navigation rail and overlay drawer labels**

Use one navigation element with items carrying `data-view` values: `overview`, `new-run`, `runs`, `resources`, `results`, `logs`, and `settings`. Keep text labels in the DOM so expansion does not require rebuilding navigation.

- [ ] **Step 3: Build the Registration Center overview**

Add Quick Start preset buttons, a batch table with selectable rows, and a selected-task detail panel. Batch rows must carry stable `data-batch-id` values consumed by `app.js`.

- [ ] **Step 4: Preserve secondary Registration Center views**

Add New Task, Runs, Resources, Results, Logs, and Settings panels with `data-view-panel` attributes. New Task includes solver, browser, network, country, count, and concurrency mock controls.

- [ ] **Step 5: Add intentional empty product states**

Ban Lookup and Phone Binding each receive a product icon, title, concise reserved-state message, and no fake commands.

- [ ] **Step 6: Add the confirmation dialog and toast region**

Use native `<dialog>` for task confirmation and `aria-live="polite"` for prototype notifications.

- [ ] **Step 7: Validate document structure**

Run:

```powershell
node --check .\v5_desktop_ui\app.js
```

Expected: exit code `0`; temporary script incompatibilities are fixed in Task 3 before final verification.

### Task 2: Replace the Visual System

**Files:**
- Modify: `v5_desktop_ui/styles.css`

- [ ] **Step 1: Define theme tokens**

Create semantic variables for application background, surfaces, text, muted text, borders, primary blue, selection blue, success, warning, danger, shadows, and radii. Define matching values for `[data-theme="dark"]` without direct inversion.

- [ ] **Step 2: Implement the compact shell**

Use a fixed 58-pixel top bar, a 60-pixel default navigation rail, a fluid central workspace, and a 292-pixel selected-task detail column on wide desktop.

- [ ] **Step 3: Implement overlay navigation expansion**

`.side-rail.is-expanded` expands to 220 pixels above the workspace. The workspace grid columns remain unchanged. Add a scrim and close affordance without shifting content.

- [ ] **Step 4: Match the reference visual language**

Use white or graphite panels, pale-blue selected rows, thin cool-gray borders, restrained shadows, 8-pixel maximum panel radii, Lucide icons, compact typography, and one blue primary action per view.

- [ ] **Step 5: Implement responsive detail behavior**

Below the wide-desktop breakpoint, convert `.task-detail` into a right overlay drawer. On mobile, make it full height and ensure tables scroll only within `.table-scroll`.

- [ ] **Step 6: Add accessible interaction states**

Provide visible keyboard focus, hover, pressed, selected, disabled, and reduced-motion states. Touch layouts use at least 44-pixel hit areas.

- [ ] **Step 7: Scan prohibited visual patterns**

Run:

```powershell
rg -n "gradient\(|letter-spacing:\s*-|font-size:\s*[^;]*(vw|dvw|svw|lvw)" .\v5_desktop_ui\styles.css
```

Expected: no output.

### Task 3: Rebuild Local UI State and Interactions

**Files:**
- Modify: `v5_desktop_ui/app.js`

- [ ] **Step 1: Define one explicit state object**

Use these stable keys:

```javascript
const state = {
  product: "registration",
  view: "overview",
  theme: "light",
  railExpanded: false,
  detailOpen: true,
  selectedPreset: "balanced",
  selectedBatchId: "30263221670",
};
```

- [ ] **Step 2: Implement product and view switching**

Product switching updates top tabs, product panels, title text, and URL hash. Registration subview switching updates the rail selection without rebuilding DOM nodes.

- [ ] **Step 3: Implement overlay rail behavior**

The menu button toggles expansion. Scrim click, navigation selection, and Escape close the rail. Update `aria-expanded` and hidden state on every transition.

- [ ] **Step 4: Implement theme initialization and persistence**

Use saved `localStorage` preference first, then `prefers-color-scheme`. The theme button updates `data-theme`, icon, tooltip, and saved preference.

- [ ] **Step 5: Implement presets and selected task details**

Preset selection updates selected styling and the summary card. Batch selection reads a local batch data map, highlights one row, and updates progress, metrics, configuration, and timeline in the right panel.

- [ ] **Step 6: Implement responsive detail controls**

The detail button opens the task detail drawer on compact layouts. Close button, scrim, and Escape dismiss it while maintaining the selected batch.

- [ ] **Step 7: Preserve mock form and feedback interactions**

Keep solver/browser/network conditional fields, count/concurrency steppers, native confirmation dialog, toast dismissal, filters, and log clearing as local-only UI behavior.

- [ ] **Step 8: Verify JavaScript syntax**

Run:

```powershell
node --check .\v5_desktop_ui\app.js
```

Expected: exit code `0` with no output.

### Task 4: Update Documentation

**Files:**
- Modify: `v5_desktop_ui/README.md`

- [ ] **Step 1: Document direct opening**

State that `index.html` can be opened directly and requires no build or local server.

- [ ] **Step 2: Document prototype scope**

List product navigation, compact rail expansion, theme switching, quick presets, batch selection, detail panel, secondary views, and responsive drawers.

- [ ] **Step 3: Document backend isolation**

Explicitly state that no workflow, registration, solver, browser, proxy, or account operation is executed.

### Task 5: Browser Verification and Screenshots

**Files:**
- Replace: `v5_desktop_ui/screenshots/desktop-1440x900.png`
- Replace: `v5_desktop_ui/screenshots/tablet-1024x768.png`
- Replace: `v5_desktop_ui/screenshots/mobile-390x844.png`
- Create: `v5_desktop_ui/screenshots/dark-1440x900.png`
- Create: `v5_desktop_ui/screenshots/navigation-drawer-1440x900.png`

- [ ] **Step 1: Run Playwright console and overflow audit**

For viewports `1440x900`, `1024x768`, and `390x844`, assert:

```javascript
document.documentElement.scrollWidth === window.innerWidth
```

Collect `pageerror`, console error, and failed-request events. Expected: no errors and no page-level horizontal overflow.

- [ ] **Step 2: Test navigation interactions**

Verify all three product tabs, all seven Registration Center subviews, rail expansion/closing, batch selection, detail drawer controls, theme switching, and dialog open/close.

- [ ] **Step 3: Capture screenshots**

Capture the five files listed above after fonts and Lucide icons finish rendering.

- [ ] **Step 4: Inspect screenshots**

Confirm no clipping, overlapping text, hidden primary commands, nested-card appearance, or unintended content shifts when the rail expands.

- [ ] **Step 5: Verify repository scope**

Run:

```powershell
git status --short
```

Expected: only the redesign prototype, screenshots, and Superpowers documentation are changed; V5 workflow and backend files remain untouched.
