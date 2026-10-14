import { createFileRoute } from '@tanstack/react-router'
import { Cpu, Database, Gauge, ShieldCheck } from 'lucide-react'

export const Route = createFileRoute('/about')({
  component: About,
})

function About() {
  return (
    <main className="page-wrap px-4 py-12">
      <section className="island-shell rounded-2xl p-6 sm:p-10">
        <p className="island-kicker mb-2">System Architecture</p>
        <h1 className="display-title mb-3 text-3xl font-bold text-[var(--sea-ink)] sm:text-5xl">
          End-to-End MLOps for Day-Ahead PV Forecasting
        </h1>
        <p className="m-0 max-w-3xl text-sm leading-7 text-[var(--sea-ink-soft)] sm:text-base">
          This platform forecasts the 24-hour solar photovoltaic energy generation (kWh) for the next day (D+1) and aggregates the daily total. It integrates automated data ingestion, physics-informed feature engineering, deep sequence modeling, containerized cloud serving, and multi-layer drift observability.
        </p>

        <div className="mt-8 grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div className="rounded-xl border border-[var(--line)] bg-[var(--input-bg)] p-5">
            <div className="flex items-center gap-2 text-sm font-bold text-[var(--sea-ink)]">
              <Database className="h-4 w-4 text-amber-500" />
              1. Leakage-Free Data & Physics Pipeline
            </div>
            <p className="mt-2 text-xs leading-6 text-[var(--sea-ink-soft)]">
              Combines 5-minute ground-truth power records from 31 DKASC Alice Springs arrays with Open-Meteo Previous Runs day-ahead weather forecasts. Computes 23 features using <code>pvlib</code> (Ineichen clear-sky, Hay-Davies POA irradiance, SAPM cell temperature) and versions snapshots with DVC on S3.
            </p>
          </div>

          <div className="rounded-xl border border-[var(--line)] bg-[var(--input-bg)] p-5">
            <div className="flex items-center gap-2 text-sm font-bold text-[var(--sea-ink)]">
              <Cpu className="h-4 w-4 text-blue-500" />
              2. Fleet LSTM & Champion/Challenger Lifecycle
            </div>
            <p className="mt-2 text-xs leading-6 text-[var(--sea-ink-soft)]">
              Normalizes output by installed capacity (kWp) so a single 2-layer PyTorch LSTM serves arbitrary array sizes, orientations, and cell technologies. Challengers are evaluated on a 14-day holdout and a 100-record behavioural golden set before human-approved promotion in MLflow Registry.
            </p>
          </div>

          <div className="rounded-xl border border-[var(--line)] bg-[var(--input-bg)] p-5">
            <div className="flex items-center gap-2 text-sm font-bold text-[var(--sea-ink)]">
              <ShieldCheck className="h-4 w-4 text-emerald-500" />
              3. Zero-Downtime Cloud Serving
            </div>
            <p className="mt-2 text-xs leading-6 text-[var(--sea-ink-soft)]">
              Requests flow from this Vercel-hosted TanStack Start UI through an AWS Application Load Balancer in the public subnet to the FastAPI inference service on ECS Fargate in the private subnet. Every prediction is logged to Supabase with model lineage.
            </p>
          </div>

          <div className="rounded-xl border border-[var(--line)] bg-[var(--input-bg)] p-5">
            <div className="flex items-center gap-2 text-sm font-bold text-[var(--sea-ink)]">
              <Gauge className="h-4 w-4 text-purple-500" />
              4. Four-Layer Monitoring & Drift Attribution
            </div>
            <p className="mt-2 text-xs leading-6 text-[var(--sea-ink-soft)]">
              Tracks infrastructure health in CloudWatch Container Insights, service p95 latency and out-of-distribution (OOD) traffic in Prometheus/Grafana, daily reference-site nMAE (including 6 held-out cold-start arrays), and KS/PSI covariate drift reports in S3.
            </p>
          </div>
        </div>
      </section>
    </main>
  )
}
