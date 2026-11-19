# Customizing branding (Admin & Organization portals)

Both portals are built to be rebranded for your own organization without touching component
code. Each portal has its own config file:

```
admin-portal/src/branding.ts
organization-portal/src/branding.ts
```

**Open that file and edit the literal strings directly.** That's it — no env vars, no build
config, nothing else to touch:

```ts
export const BRANDING = {
  appName: import.meta.env.VITE_APP_NAME || "Acme Platform", // <- edit this
  logoUrl: import.meta.env.VITE_LOGO_URL || "/logo.svg", // <- and/or this
  accentColor: import.meta.env.VITE_ACCENT_COLOR || "#7c3aed", // <- and/or this
  backgroundColor: import.meta.env.VITE_BACKGROUND_COLOR || undefined, // <- and/or this
  surfaceColor: import.meta.env.VITE_SURFACE_COLOR || undefined, // <- and/or this
  textColor: import.meta.env.VITE_TEXT_COLOR || undefined, // <- and/or this
} as const;
```

Each portal is a separate build, so they can have different names/logos if you want.

## Logo image

Drop your logo file into that portal's `public/` directory (e.g. `public/logo.svg`), then set
`logoUrl` in `branding.ts` to point at it (as above). Leave it `undefined` for a clean text-only
wordmark using `appName` alone — no image is required.

## Favicon

Just replace `public/favicon.svg` with your own icon file in each portal. No config change at
all — the browser picks up whatever file is at that path.

## Colors: accent, background, surface, and text

Four independent, optional one-color knobs in `branding.ts` — set any subset of them:

- **`accentColor`** — repaints every "selected/highlighted" surface: the active Sidebar item,
  solid buttons/tabs, focus rings, and selected text.
- **`backgroundColor`** — repaints the page content backdrop only, behind Header/Sidebar/cards.
- **`surfaceColor`** — repaints the default card/panel surface color, including the Header and
  Sidebar — they always match this, there's no separate setting for either. Independent of
  `backgroundColor`, so the chrome around the content can be a different color from the content
  area itself.
- **`textColor`** — repaints the base body text only. Secondary/caption text is left alone, so the
  type hierarchy isn't flattened to one color.

Each takes a single hex value and derives its own dark-mode and light-mode variant from it — you
never pick four values for one setting. Dark and light mode are treated asymmetrically for the
background-family colors: dark mode stays low on both lightness and saturation so it reads as an
actual dark theme first (the hue shows as a subtle tint, not a dominant color), while light mode
stays clearly saturated and visible rather than washing out to a barely-there pastel. Leave any
setting unset to keep that piece of the shipped default exactly as-is.

## Optional: per-deployment overrides via env vars

`branding.ts`'s literals are read as _fallback defaults_ — `VITE_APP_NAME` / `VITE_LOGO_URL` /
`VITE_ACCENT_COLOR` / `VITE_BACKGROUND_COLOR` / `VITE_SURFACE_COLOR` / `VITE_TEXT_COLOR` env vars
(see `.env.example`) take priority when set. This only matters if you
want different branding per environment (e.g. staging vs. production) without maintaining separate
code branches. If you just want to rebrand the app once for your fork, ignore env vars entirely
and edit the literals in `branding.ts` — that's the primary, simplest path. Quote color values in
`.env.local` (e.g. `VITE_ACCENT_COLOR="#7c3aed"`) — an unquoted `#` starts a comment in `.env`
files and silently drops everything after it.

## What's _not_ branding

The organization name shown under the wordmark in the Organization Portal's sidebar is the live,
authenticated tenant's own name (`organizationContext.name`) — it comes from your data, not from
this config, and isn't something you'd change here.

## Where this lives in code

- `packages/ui`'s `BrandLogo` component — presentation only, takes `name`/`logoUrl` as props, no
  env access itself (the shared design system stays business-agnostic).
- `packages/ui`'s `accentColor.ts` (`applyAccentColor`/`deriveAccentShades`) and `themeSurface.ts`
  (`applyBackgroundColor`/`applySurfaceColor`/`applyTextColor` and their `derive*` helpers) — the
  color math and runtime CSS-variable overrides behind the four color settings. Header/Sidebar are
  built on `--color-neutral-0`, the variable `surfaceColor` sets, independent of `backgroundColor`
  (`--canvas-bg`).
- Each portal's own `src/branding.ts` — the actual config file described above.
- Each portal's `src/main.tsx` calls
  `applyAccentColor`/`applyBackgroundColor`/`applySurfaceColor`/`applyTextColor` once at startup.
