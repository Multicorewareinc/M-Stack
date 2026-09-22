# agentic-console

A browser console for the MultiStack **agentic layer** — describe
infrastructure in plain English, get back a validated SDK script. It
replaces the CLI (`agentic/backend/src/examples/chat.py`) for demos.

It reuses `@multistack/ui` (via a `file:` dependency on `ui/packages/ui`
-- see package.json), so it inherits the portals' design system rather
than re-styling anything, without itself being a member of the `ui/`
npm workspace.

## What it can and cannot do

The agentic service registers **no** deploy/execute/provision tool, so
this console cannot deploy either. The script panel has a copy button and
deliberately no "run" button.

## Running it

**1. The agentic service needs an API key.** Every `/v1` route is guarded
by a shared `X-API-Key`, and `SERVICE_API_KEY` is currently empty in
`agentic/backend/.env` — the service fails closed and returns a 500 until
it is set. Generate one:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put it in `agentic/backend/.env` as `SERVICE_API_KEY=...`, then start the
service:

```bash
cd agentic/backend/src && uvicorn main:create_app --factory --port 8000
```

**2. Point the console at it.** Copy `.env.example` to `.env` and set the
same key:

```bash
cp .env.example .env         # then set AGENTIC_API_KEY to the same value
```

**3. Run the console.** This app is standalone -- not a member of the
`ui/` npm workspace -- so install and run it from here directly:

```bash
npm install                  # from agentic/frontend/
npm run dev
```

Open http://localhost:5175.

## Why the key lives in the dev proxy

`vite.config.ts` attaches `X-API-Key` server-side when proxying
`/agentic/*` to the service. The browser never receives the shared
service key, and the SPA sends no credential at all. Proxying also
sidesteps CORS — the agentic app registers no CORS middleware, so a
direct cross-origin call from `:5175` would be blocked.

For a deployment rather than a demo, put the console behind the same
ingress as the service, or give it its own per-user auth; do not ship the
shared key to a browser.

## Layout

```
src/
  api/agentic.ts              the three endpoints, one response shape
  components/layout/          Header + Sidebar, matching the portals
  features/plan/PlanPage.tsx  conversation + generated-script panel
  features/plan/CapabilitiesPage.tsx   static roster of the 17 capabilities
```
