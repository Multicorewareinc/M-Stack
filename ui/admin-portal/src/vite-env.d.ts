/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  /** Public URL of the Organization Portal (e.g. https://org.example.com). Invitation links are accepted
   * there, not in this portal, so the Invite drawer builds the link from it. Omit when the Org CP's
   * ORG_PORTAL_BASE_URL is set (it then returns an absolute link) or both portals share one origin. */
  readonly VITE_ORG_PORTAL_URL?: string;
  readonly VITE_APP_NAME?: string;
  /** Optional logo image path/URL for a fork's own branding (see branding.ts). Drop the file in
   * public/ and point this at it, e.g. VITE_LOGO_URL=/logo.svg. Omit for a text-only wordmark. */
  readonly VITE_LOGO_URL?: string;
  /** Optional accent/brand color for a fork's own theme (see branding.ts), e.g. '#7c3aed'. Drives
   * every selected/highlighted UI surface in both dark and light mode. Omit to keep the default. */
  readonly VITE_ACCENT_COLOR?: string;
  /** Optional page content backdrop color for a fork's own theme (see branding.ts), e.g.
   * '#0f172a'. Does NOT reach Header/Sidebar/cards — see VITE_SURFACE_COLOR for those. Omit to
   * keep the default. */
  readonly VITE_BACKGROUND_COLOR?: string;
  /** Optional Header/Sidebar/card surface color for a fork's own theme (see branding.ts), e.g.
   * '#1e293b'. Independent of VITE_BACKGROUND_COLOR. Omit to keep the default. */
  readonly VITE_SURFACE_COLOR?: string;
  /** Optional base text color for a fork's own theme (see branding.ts), e.g. '#111827'. Only
   * primary body text changes — secondary/caption text is untouched. Omit to keep the default. */
  readonly VITE_TEXT_COLOR?: string;
  /** Dev-only bridge to docker-compose.rbac.yml's shared bearer key — see MockAuthProvider. */
  readonly VITE_DEV_API_KEY?: string;
  /** Set to 'true' to mount the real AuthProvider (login/refresh flow) in dev instead of MockAuthProvider. */
  readonly VITE_REAL_AUTH?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
