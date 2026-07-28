# V5 Desktop UI Redesign

## Objective

Redesign the standalone V5 desktop UI prototype around the visual language of
the supplied Minecloud reference: a light application frame, blue accent,
horizontal product navigation, compact left navigation, a central task
workspace, and a persistent selected-task detail panel.

This phase remains a frontend prototype. It does not call registration,
captcha, proxy, browser, GitHub Actions, or account-management code.

## Product Structure

The application shell reserves three top-level product pages:

1. Registration Center
2. Ban Lookup
3. Phone Binding

Registration Center is the only populated product page in this phase. Ban
Lookup and Phone Binding render deliberate empty states inside the same shell,
so future features can be added without restructuring navigation.

## Application Shell

### Top Bar

- Brand mark and `V5 Suite` product name on the left.
- Horizontal product tabs for Registration Center, Ban Lookup, and Phone
  Binding.
- Search field, notification button, theme toggle, and operator profile on the
  right.
- The active product tab uses a white surface, blue icon/text, and subtle
  elevation.

### Compact Navigation Rail

The default left navigation is a compact icon rail approximately 56 pixels
wide. It contains:

- Overview
- New Task
- Runs
- Resources
- Results
- Logs
- Settings

Selecting the menu control expands the rail into an approximately 220-pixel
drawer that overlays the workspace. Expansion must not resize, shift, or
compress the central content or right detail panel.

The drawer closes when the user:

- clicks the menu control again;
- clicks outside the drawer;
- selects a navigation item; or
- presses Escape.

Icons require accessible labels and tooltips while the rail is collapsed.

### Central Workspace

The Registration Center landing view contains two main bands:

1. Quick Start
2. Batch List

Quick Start shows three or four recently used configurations. Each preset
displays solver, browser, network mode, country, and concurrency. Selecting a
preset loads it into the mock configuration state. A single blue `New Task`
button is the primary action.

The batch list is a dense operational table containing:

- batch identifier;
- status;
- progress;
- success rate;
- solver and browser;
- traffic usage; and
- start time.

Selecting a row applies a pale-blue highlight and updates the right detail
panel. Tables remain horizontally scrollable only inside their own bounded
container on narrow screens; the page itself must not overflow horizontally.

### Selected Task Detail Panel

The right panel remains visible on wide desktop viewports and reflects the
currently selected batch. It contains:

- batch state and identifier;
- progress and primary controls;
- successful, failed, retrying, and remaining counts;
- proxy and direct traffic totals;
- solver, browser, network, country, and email source;
- recent event timeline.

On narrower viewports, the panel becomes an on-demand right-side drawer rather
than reducing the central workspace below a usable width.

## Secondary Views

The Registration Center retains separate prototype views for New Task, Runs,
Resources, Results, Logs, and Settings. They reuse the same shell and visual
tokens.

Ban Lookup and Phone Binding display a restrained empty state with a product
icon, title, short status message, and no fake controls or fabricated data.

## Theme System

The UI supports light and dark themes through a top-bar icon button.

### Light Theme

- neutral gray application background;
- white navigation and content surfaces;
- blue primary actions and selection states;
- near-black primary text;
- thin cool-gray borders and restrained shadows.

### Dark Theme

- charcoal application background rather than pure black;
- raised graphite surfaces;
- a lighter blue accent that retains contrast;
- desaturated borders and secondary text;
- no direct color inversion.

Theme selection persists in local storage. The implementation respects
`prefers-color-scheme` for the initial default when no explicit preference has
been saved. Theme transitions are limited to color and opacity and must respect
`prefers-reduced-motion`.

## Visual Rules

- Use the reference image's blue-white, desktop-software character.
- Use Lucide icons with one consistent stroke weight.
- Keep panel and card radii at 8 pixels or less.
- Use one primary blue action per view.
- Avoid gradients, decorative blobs, oversized headings, marketing layouts,
  and nested cards.
- Use tabular figures for progress, timing, percentages, and traffic values.
- Maintain visible focus states and WCAG AA contrast for text and controls.
- Interactive targets are at least 40 pixels on desktop and 44 pixels on touch
  layouts.

## Responsive Behavior

### Wide Desktop

- compact left rail;
- central workspace;
- persistent right detail panel.

### Standard Desktop and Tablet

- compact left rail;
- central workspace occupies remaining width;
- right details open as a drawer when space is insufficient.

### Mobile

- top product tabs become a compact menu or horizontally bounded selector;
- left navigation is drawer-only;
- cards stack vertically;
- batch tables use an internal scroll container;
- selected task details open as a full-height sheet.

No viewport may produce page-level horizontal overflow or clipped command
buttons.

## Prototype Interactions

The frontend-only prototype implements:

- switching among the three product pages;
- expanding and closing the left navigation drawer;
- switching Registration Center subviews;
- selecting quick-start presets;
- selecting batch rows and updating task details;
- opening the New Task configuration view;
- opening task confirmation dialogs;
- switching and persisting light/dark themes;
- opening and closing responsive detail drawers;
- mock toasts, filters, and log controls.

All actions remain local UI state and must clearly avoid claiming that a real
registration or account operation occurred.

## Verification

The redesigned prototype is complete when:

- all product and Registration Center navigation states work;
- the navigation rail expands as an overlay without shifting the workspace;
- the selected batch updates the right detail panel;
- light and dark themes render correctly and persist;
- Ban Lookup and Phone Binding show intentional empty states;
- desktop, tablet, and mobile screenshots show no overlap or page-level
  horizontal overflow;
- browser console and page error checks are clean; and
- existing V5 workflow and backend files remain untouched.
