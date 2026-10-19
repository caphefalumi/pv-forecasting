# PV Solar Forecast Frontend (`apps/frontend`)

Client-facing web application for Day-Ahead (D+1) 24-hour photovoltaic solar energy forecasting, built with **React 19**, **TanStack Router / Query / Form**, **TypeScript**, and **Tailwind CSS v4**, deployed on **Vercel** as a static SPA.

---

## 1. Architecture & Features

- **Stack:** Vite + React 19 SPA, TanStack Router (file-based), TanStack Query, TanStack Form, Tailwind CSS v4.
- **API client (`src/lib/api-client.ts`):** Axios `getForecast` posts validated array specs to FastAPI (`/api/v1/forecast`) with a 10-second timeout and `X-Request-ID` correlation tracing. Local Vite proxies `/api` → `http://127.0.0.1:8000`; production uses `VITE_BACKEND_API_URL` (ALB).
- **Domain Validation & Calculations (`src/lib/forecast-utils.ts`):** Pure TypeScript domain functions enforcing physical boundaries (`kwp`, `tilt_deg`, `surface_azimuth_pvlib`, `technology`, `tracking`) and computing specific yield ($\text{kWh}/\text{kWp}$), peak hour, and daily totals.
- **Interactive Forecast Dashboard (`src/routes/index.tsx`):**
  - Configurable PV array form (capacity in kWp, 7 cell technologies, 3 tracking modes, tilt, azimuth, request date).
  - Out-of-Distribution (`is_ood`) warning banner when user inputs fall outside the DKASC reference training range.
  - KPI summary cards (Total Daily Generation, Peak Hour Power, Specific Yield, Model Version).
  - 24-hour solar generation curve visualization and hourly breakdown table.

---

## 2. Directory Structure

```text
apps/frontend/
├── index.html
├── package.json
├── tsconfig.json
├── vercel.json
├── vite.config.ts
├── src/
│   ├── main.tsx                 # SPA entry (RouterProvider + QueryClient)
│   ├── components/
│   │   ├── Header.tsx
│   │   ├── Footer.tsx
│   │   └── ThemeToggle.tsx
│   ├── integrations/
│   │   └── tanstack-query/      # QueryClient factory
│   ├── lib/
│   │   ├── api-client.ts        # Browser → FastAPI axios client
│   │   └── forecast-utils.ts
│   ├── routes/
│   │   ├── __root.tsx
│   │   ├── index.tsx
│   │   └── about.tsx
│   ├── router.tsx
│   ├── routeTree.gen.ts
│   └── styles.css
└── tests/
    ├── unit/
    └── integration/
```

---

## 3. Local Development & Testing

```bash
cd apps/frontend

# Start Vite on http://localhost:3000 (/api proxied to FastAPI :8000)
bun run dev

# Run unit and integration tests (bun:test)
bun run test

# Type-check
bun x tsc --noEmit

# Production static build → dist/
bun run build
```

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `VITE_BACKEND_API_URL` | _(empty; Vite proxy)_ | FastAPI base URL for production (AWS ALB). Leave unset locally. |
