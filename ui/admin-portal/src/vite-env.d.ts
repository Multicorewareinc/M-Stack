/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_APP_NAME?: string;
  /** Dev-only bridge to docker-compose.rbac.yml's shared bearer key — see MockAuthProvider. */
  readonly VITE_DEV_API_KEY?: string;
  /** Set to 'true' to mount the real AuthProvider (login/refresh flow) in dev instead of MockAuthProvider. */
  readonly VITE_REAL_AUTH?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
