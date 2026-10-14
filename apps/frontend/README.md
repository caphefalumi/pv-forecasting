# PV Solar Forecast Frontend (`apps/frontend`)

Client-facing web application for Day-Ahead (D+1) 24-hour photovoltaic solar energy forecasting, built with **TanStack Start**, **React 19**, **TypeScript**, and **Tailwind CSS v4**, deployed on **Vercel**.

---

## 1. Architecture & Features

- **Framework:** TanStack Start + TanStack Router (file-based SSR routing) + React 19 + Vite 8.
- **Server Functions (`src/lib/api-client.ts`):** Type-safe `createServerFn` RPC bridge (`getForecastServerFn`) forwarding validated user array specifications to the FastAPI backend (`BACKEND_API_URL` / AWS Application Load Balancer) with a 10-second timeout and `X-Request-ID` correlation tracing.
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
├── package.json
├── tsconfig.json
├── vercel.json
├── vite.config.ts
├── src/
│   ├── components/
│   │   ├── Header.tsx             # Primary navigation & theme toggle
│   │   ├── Footer.tsx             # Attribution footer
│   │   └── ThemeToggle.tsx        # Light / dark / system mode switch
│   ├── integrations/
│   │   └── tanstack-query/        # QueryClient provider & devtools
│   ├── lib/
│   │   ├── api-client.ts          # TanStack Start server function -> FastAPI
│   │   └── forecast-utils.ts      # Pure domain validation & PV yield math
│   ├── routes/
│   │   ├── __root.tsx             # HTML shell, SEO meta, 404 boundary
│   │   ├── index.tsx              # Day-Ahead (D+1) Forecast dashboard
│   │   └── about.tsx              # System Architecture & MLOps overview
│   ├── router.tsx                 # Router initialization
│   ├── routeTree.gen.ts           # Auto-generated route tree
│   └── styles.css                 # Tailwind CSS v4 design tokens
└── tests/
    ├── unit/
    │   └── forecast-utils.test.ts # Domain validation & calculation unit tests
    └── integration/
        └── api-contract.test.ts   # D+1 API contract & error formatting tests
```

---

## 3. Local Development & Testing

```bash
cd apps/frontend

# Start development server on http://localhost:3000
bun run dev

# Run unit and integration tests (bun:test)
bun run test

# Type-check TypeScript strictly
bun x tsc --noEmit

# Build production SSR & client bundles
bun run build
```

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `BACKEND_API_URL` | `http://127.0.0.1:8000` | Base URL of the FastAPI service (local or AWS ALB DNS) |
