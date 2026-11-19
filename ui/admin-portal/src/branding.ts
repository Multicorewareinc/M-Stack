/**
 * THIS is the file to edit to rebrand this portal — no component changes needed anywhere else.
 *
 * Simplest path: just replace the literal strings below ('Multistack Admin', etc.) with your own
 * and you're done — no env vars required.
 *
 * Optional: if you'd rather set branding per-deployment without touching code (e.g. different
 * values in CI/Docker builds), set VITE_APP_NAME / VITE_LOGO_URL as env vars (see .env.example)
 * — they take priority over the literals here when present, which stay as the fallback default.
 *
 * One thing NOT configured here: the browser tab icon. Just replace public/favicon.svg with your
 * own file — no code change, no env var (see ../BRANDING.md).
 */
export const BRANDING = {
  appName: import.meta.env.VITE_APP_NAME || "Multistack Admin",
  /** Path/URL to a logo image (e.g. '/logo.svg', dropped into public/). Leave undefined for a
   * text-only wordmark — see @multistack/ui's BrandLogo. */
  logoUrl: import.meta.env.VITE_LOGO_URL || undefined,
  /** One hex color (e.g. '#7c3aed') — applied at startup (main.tsx) via @multistack/ui's
   * applyAccentColor, which derives the full shade set for both dark and light mode from it.
   * Drives every "selected/highlighted" surface: the active Sidebar item, solid buttons/tabs,
   * focus rings, and selected text. Leave unset to keep the shipped default blue.
   * Setting any of these three colors via env vars in .env.local? Quote the value, e.g.
   * VITE_ACCENT_COLOR="#7c3aed" — an unquoted # starts a comment in .env files and silently drops
   * everything after it. */
  accentColor: import.meta.env.VITE_ACCENT_COLOR || undefined,
  /** One hex color for the page content backdrop (applied via applyBackgroundColor). Does NOT
   * reach Header/Sidebar/cards — see surfaceColor below for those. Leave unset to keep the shipped
   * default. */
  backgroundColor: import.meta.env.VITE_BACKGROUND_COLOR || undefined,
  /** One hex color for the surface Header/Sidebar/cards/panels all share (applied via
   * applySurfaceColor). Independent of backgroundColor, so the chrome around the content can be a
   * different color from the content area itself. Header and Sidebar always match this — there's
   * no separate setting for either. Leave unset to keep the shipped default. */
  surfaceColor: import.meta.env.VITE_SURFACE_COLOR || undefined,
  /** One hex color for the base body text (applied via applyTextColor). Only primary text
   * changes — secondary/caption text stays as-is. Leave unset to keep the shipped default. */
  textColor: import.meta.env.VITE_TEXT_COLOR || undefined,
} as const;
