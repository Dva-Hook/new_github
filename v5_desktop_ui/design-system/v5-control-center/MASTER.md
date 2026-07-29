# V5 Suite Desktop Design System

## Direction

Compact desktop operations software inspired by the supplied Minecloud UI:
light application chrome, blue product accents, a compact icon rail, a dense
central workspace, and a selected-item detail panel.

The interface must feel like software rather than a marketing dashboard. Avoid
large hero headings, gradients, decorative illustrations, floating blobs, and
deeply nested cards.

## Layout

- Top bar: `64px` desktop, `58px` mobile
- Compact navigation rail: `60px`
- Expanded overlay drawer: `220px`
- Wide-desktop task detail panel: `292px`
- Workspace padding: `18px` desktop, `12px` mobile
- Panel gap: `18px`
- Maximum panel radius: `8px`

The expanded navigation drawer overlays the workspace. It never changes the
workspace's x-position or width.

## Light Theme

| Token | Value | Usage |
| --- | --- | --- |
| Application background | `#E9EAED` | Main canvas |
| Top bar | `#F4F4F6` | Application chrome |
| Surface | `#FFFFFF` | Panels and controls |
| Soft surface | `#F7F8FA` | Table headers and grouped fields |
| Selected surface | `#E7F2FF` | Selected presets and rows |
| Primary | `#2447F9` | Main action and progress |
| Primary hover | `#1738DC` | Main action hover |
| Text | `#171923` | Headings and primary content |
| Secondary text | `#5F6675` | Supporting content |
| Border | `#DFE1E7` | Panels and separators |

## Dark Theme

| Token | Value | Usage |
| --- | --- | --- |
| Application background | `#17191E` | Main canvas |
| Top bar | `#202329` | Application chrome |
| Surface | `#24272E` | Panels and controls |
| Soft surface | `#292D35` | Table headers and grouped fields |
| Selected surface | `#243653` | Selected presets and rows |
| Primary | `#6F88FF` | Main action and progress |
| Text | `#F2F4F8` | Headings and primary content |
| Secondary text | `#B5BBC7` | Supporting content |
| Border | `#383D47` | Panels and separators |

Dark mode uses graphite surfaces rather than pure black and is not a direct
color inversion.

## Typography

- Font family: `Inter`, `Segoe UI`, `Microsoft YaHei UI`, sans-serif
- Data font: `JetBrains Mono`, `Cascadia Code`, `Consolas`, monospace
- Page title: `22px / 700`
- Panel title: `16px / 680`
- Body and controls: `11–14px`
- Metadata: `8–10px`
- Use tabular figures for progress, traffic, percentages, IDs, and timestamps.

## Navigation

Top product tabs:

1. Registration Center
2. Ban Lookup
3. Phone Binding

Registration Center rail items:

- Overview
- New Task
- Runs
- Resources
- Results
- Logs
- Settings

Collapsed rail icons always have accessible names and tooltips. The drawer
closes on outside click, selection, repeated menu click, or Escape.

## Components

### Buttons

- One blue primary button per view
- Secondary buttons use white/graphite surfaces and thin borders
- Icon-only controls are square and always have `aria-label`
- Use 40px desktop and 44px touch hit targets where practical

### Panels

- One border, restrained shadow, `8px` radius or less
- Do not place decorative cards inside cards
- Group related form fields with a soft surface and one border

### Tables

- Compact rows and muted headers
- Selected row uses pale blue, not a heavy outline
- Tables may scroll inside a bounded container on narrow screens
- The page itself never scrolls horizontally

### Status

- Blue: running or active
- Green: complete or healthy
- Amber: retry or warning
- Red: failed or destructive
- Status color is always paired with text or an icon

## Responsive Rules

- Above `1180px`: persistent task detail panel
- At or below `1180px`: task details become a right overlay drawer
- At or below `720px`: navigation rail becomes drawer-only
- Product tabs collapse to icons on mobile
- Preset and resource grids stack progressively
- Dialogs stay within `100vw - 28px`

## Motion and Accessibility

- Interaction transitions: `180–220ms`
- Animate only transform, opacity, color, and shadow
- Respect `prefers-reduced-motion`
- Maintain visible keyboard focus
- Use semantic headings, labels, tables, dialogs, and live regions
- Text and controls must meet WCAG AA contrast
