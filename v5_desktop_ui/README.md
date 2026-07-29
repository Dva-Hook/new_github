# V5 Suite Desktop UI

Standalone desktop-first UI prototype for the V5 toolset. The visual structure
is inspired by the supplied Minecloud reference and is intentionally isolated
from all registration and account-management code.

Open `index.html` directly. No build command or local web server is required.

## Product Pages

- Registration Center: complete prototype workspace
- Ban Lookup: reserved empty page
- Phone Binding: reserved empty page

## Prototype Interactions

- compact icon navigation rail
- overlay drawer expansion without resizing the workspace
- Registration Center subview navigation
- quick-start configuration selection
- batch selection with synchronized task details
- responsive right-side task detail drawer
- New Task configuration controls and confirmation dialog
- persistent light/dark theme switching
- local filters, toasts, log clearing, and settings states

## Files

- `index.html` - application shell, views, tables, forms, and dialogs
- `styles.css` - themes, layout, components, and responsive behavior
- `app.js` - local-only UI state and interactions
- `design-system/v5-control-center/MASTER.md` - current layout and theme guidance
- `screenshots/` - verified desktop, tablet, mobile, theme, and drawer previews

## Scope

This prototype does not execute GitHub Actions, registration, captcha solving,
browser automation, proxy operations, email verification, phone binding, ban
lookup, or any account action. All displayed values and interactions are local
demonstration data.
