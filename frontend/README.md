# AstraBox — web console (frontend)

The AstraBox web console: a React + TypeScript single-page app (Vite) that
drives the FastAPI agent-platform API (`/api/v1/…`) and renders live agent turns
streamed over HTTP SSE using AI SDK UI message frames. The local no-auth
deployment has no login step; authenticated deployments use `/login` and the
server's identity configuration. Requests include browser credentials.

## Run it

From the repository root, install the dependencies and start the maintained
development stack:

```bash
make install
make dev
```

To start only the frontend against an already running backend:

```bash
python3 scripts/node-toolchain.py npm --prefix frontend ci
python3 scripts/node-toolchain.py npm --prefix frontend run dev
```

`npm run dev` proxies `/api → http://127.0.0.1:8000` (see `vite.config.ts`), so
the browser hits the real backend with no CORS setup. Open
<http://localhost:5173>.

| script | does |
|---|---|
| `npm run dev` | Vite dev server (HMR) on `:5173`, `/api` proxied to `:8000`. |
| `npm run build` | `tsc --noEmit` typecheck **then** `vite build` → `dist/`. |
| `npm run preview` | Serve the built `dist/` locally. |
| `npm run test` | Vitest unit tests. |
| `npm run e2e` | Playwright browser e2e (uses `../e2e/playwright.config.ts`). |

There is no separate `typecheck` script — the type check runs inside `build`
(`tsc --noEmit`). From the repo root, `make build-web` runs the same build and
`make dev` boots the backend and this console together. To point the client at a
backend without the dev proxy, set `VITE_API_BASE` to an absolute origin (default
empty = use the proxy / same origin).

Use the pinned Node.js toolchain (`scripts/node-toolchain.py`) rather than an
ambient installation: `make build-web` runs the typecheck and the production
build, `make test-web` adds the console policy checks and the unit run. See
[CONTRIBUTING.md](../CONTRIBUTING.md) for which check a change needs.

## Layout

One HTML entry point, built by Vite (`index.html`). Everything — the app, the
operator console and the public share view — is routed from it:

```
src/
  main.tsx        mounts the main SPA; BrowserRouter routes:
                    /login        → LoginPage
                    /manage/*     → manage/ManageApp  (operator console)
                    /share/:token → SharePage         (public share view)
                    /*            → App               (the primary console)
  App.tsx         primary console shell: sidebar + agents/assistant tabs,
                  session list, and session detail
  api.ts          typed, envelope-aware fetch client for every /api/v1 route
                  (honours VITE_API_BASE)
  types.ts        shared API types
  session/        the live-turn conversation surface (SessionPage) and its
                  session-state machinery (polling, outbox / tool / pending-
                  interaction state, auto-resume)
  manage/         operator "manage" console (ManageApp): agents, environments,
                  deployments, sessions, assistants  (console/ is its UI kit)
  assistant/      assistants feature (page, api, types)
  components/     shared components:
                    ui/            shadcn/ui primitives
                    ai-elements/   Vercel AI Elements (chat / agent UI)
                    form/
  lib/utils.ts    the `cn` class-name helper
  hooks/  utils/  shared hooks and helpers
  i18n/           i18next; English + Chinese (locales/en, locales/zh)
  styles.css      Tailwind v4 (@import "tailwindcss" + @theme) + design tokens
```

Styling is Tailwind v4 (CSS-first `@theme` in `styles.css`, plus
`shadcn/tailwind.css`); icons are `lucide-react`.
