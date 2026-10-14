import { useState } from 'react'
import { createFileRoute } from '@tanstack/react-router'
import { useForm, type AnyFieldApi } from '@tanstack/react-form'
import {
  Sun,
  Zap,
  Activity,
  Calendar,
  Sliders,
  CheckCircle2,
  AlertCircle,
  Clock,
  TrendingUp,
} from 'lucide-react'
import { getForecastServerFn } from '../lib/api-client'
import {
  type ArrayParameters,
  type ForecastResponse,
  VALID_TECHNOLOGIES,
  VALID_TRACKING,
  calculateSpecificYield,
  isDaytimeHour,
  validateArrayParameters,
} from '../lib/forecast-utils'

export const Route = createFileRoute('/')({
  component: PVForecastPage,
})

function FieldError({ field }: { field: AnyFieldApi }) {
  if (!field.state.meta.isTouched || field.state.meta.isValid) {
    return null
  }
  return (
    <p role="alert" className="mt-1 text-[11px] font-medium text-rose-600 dark:text-rose-400">
      {field.state.meta.errors.join(', ')}
    </p>
  )
}

function PVForecastPage() {
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<ForecastResponse | null>(null)

  const form = useForm({
    defaultValues: {
      kwp: 10.0,
      technology: 'mono-Si',
      tracking: 'fixed',
      tilt_deg: 20.0,
      surface_azimuth_pvlib: 0.0,
      predict_date: new Date().toISOString().split('T')[0],
    } satisfies ArrayParameters,
    onSubmit: async ({ value }) => {
      setError(null)
      const validation = validateArrayParameters(value)
      if (!validation.valid) {
        setError(validation.errors.join(' '))
        return
      }

      try {
        const data = await getForecastServerFn({
          data: value,
        })
        setResult(data)
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : 'Error executing forecast')
      }
    },
  })

  const maxKwh = result ? Math.max(...result.hourly_breakdown.map((h) => h.kwh), 1) : 1

  return (
    <main className="page-wrap px-4 pb-16 pt-8">
      {/* Hero Header */}
      <section className="island-shell rise-in relative overflow-hidden rounded-[2rem] px-6 py-8 sm:px-10 sm:py-10">
        <div className="pointer-events-none absolute -left-20 -top-24 h-56 w-56 rounded-full bg-[radial-gradient(circle,rgba(245,158,11,0.2),transparent_66%)]" />
        <div className="pointer-events-none absolute -bottom-20 -right-20 h-56 w-56 rounded-full bg-[radial-gradient(circle,rgba(59,130,246,0.15),transparent_66%)]" />

        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <div className="inline-flex items-center gap-2 rounded-full border border-amber-500/30 bg-amber-500/10 px-3 py-1 text-xs font-semibold text-amber-700 dark:text-amber-300">
              <Sun className="h-3.5 w-3.5" />
              COS40007 MLOps Platform — Day-Ahead PV Forecasting
            </div>
            <h1 className="mt-3 text-3xl font-bold tracking-tight text-[var(--sea-ink)] sm:text-5xl">
              Day-Ahead Solar Energy Forecast (D+1)
            </h1>
            <p className="mt-2 max-w-2xl text-sm text-[var(--sea-ink-soft)] sm:text-base">
              Enter your PV array specifications below. Our deep LSTM fleet model couples Open-Meteo day-ahead weather forecasts with clear-sky solar geometry to predict tomorrow's 24-hour generation.
            </p>
          </div>

          <div className="flex items-center gap-2 text-xs text-[var(--sea-ink-soft)]">
            <span className="inline-block h-2.5 w-2.5 rounded-full bg-emerald-500 animate-pulse" />
            Champion Model Ready
          </div>
        </div>
      </section>

      {/* Main Grid: Form + Results */}
      <div className="mt-8 grid grid-cols-1 gap-8 lg:grid-cols-12">
        {/* Left Column: Form (TanStack Form) */}
        <section className="island-shell rounded-2xl p-6 lg:col-span-4">
          <h2 className="flex items-center gap-2 text-lg font-bold text-[var(--sea-ink)]">
            <Sliders className="h-5 w-5 text-amber-500" />
            Array Parameters
          </h2>
          <p className="mt-1 text-xs text-[var(--sea-ink-soft)]">
            Configure system capacity and mounting geometry.
          </p>

          <form
            onSubmit={(e) => {
              e.preventDefault()
              e.stopPropagation()
              form.handleSubmit()
            }}
            className="mt-6 space-y-4"
          >
            {/* Capacity in kWp */}
            <form.Field
              name="kwp"
              validators={{
                onChange: ({ value }) =>
                  typeof value !== 'number' || isNaN(value) || value <= 0
                    ? 'Peak capacity (kWp) must be greater than 0.'
                    : value > 5000
                      ? 'Peak capacity (kWp) exceeds maximum allowable fleet size of 5000 kWp.'
                      : undefined,
              }}
              children={(field) => (
                <div>
                  <label
                    htmlFor={field.name}
                    className="block text-xs font-semibold uppercase tracking-wider text-[var(--sea-ink-soft)]"
                  >
                    Rated Peak Capacity (kWp)
                  </label>
                  <div className="relative mt-1">
                    <input
                      id={field.name}
                      name={field.name}
                      type="number"
                      step="0.1"
                      min="0.5"
                      max="1000"
                      required
                      value={field.state.value}
                      onBlur={field.handleBlur}
                      onChange={(e) => field.handleChange(parseFloat(e.target.value) || 0)}
                      className="w-full rounded-xl border border-[var(--line)] bg-[var(--input-bg)] px-4 py-2 text-sm font-semibold text-[var(--sea-ink)] focus:border-amber-500 focus:outline-none"
                    />
                    <span className="absolute right-3 top-2.5 text-xs text-[var(--sea-ink-soft)]">
                      kWp
                    </span>
                  </div>
                  <FieldError field={field} />
                </div>
              )}
            />

            {/* Cell Technology */}
            <form.Field
              name="technology"
              validators={{
                onChange: ({ value }) =>
                  !VALID_TECHNOLOGIES.includes(value as (typeof VALID_TECHNOLOGIES)[number])
                    ? `Invalid technology: ${value}.`
                    : undefined,
              }}
              children={(field) => (
                <div>
                  <label
                    htmlFor={field.name}
                    className="block text-xs font-semibold uppercase tracking-wider text-[var(--sea-ink-soft)]"
                  >
                    Cell Technology
                  </label>
                  <div className="relative mt-1">
                    <select
                      id={field.name}
                      name={field.name}
                      value={field.state.value}
                      onBlur={field.handleBlur}
                      onChange={(e) => field.handleChange(e.target.value)}
                      className="w-full rounded-xl border border-[var(--line)] bg-[var(--input-bg)] px-4 py-2 text-sm font-semibold text-[var(--sea-ink)] focus:border-amber-500 focus:outline-none"
                    >
                      <option value="mono-Si">Monocrystalline (mono-Si)</option>
                      <option value="poly-Si">Polycrystalline (poly-Si)</option>
                      <option value="CdTe">Cadmium Telluride (CdTe)</option>
                      <option value="CIGS">CIGS Thin-Film</option>
                      <option value="HIT">Heterojunction (HIT)</option>
                      <option value="a-Si">Amorphous Silicon (a-Si)</option>
                      <option value="CPV">Concentrator PV (CPV)</option>
                    </select>
                  </div>
                  <FieldError field={field} />
                </div>
              )}
            />

            {/* Tracking Type */}
            <form.Field
              name="tracking"
              validators={{
                onChange: ({ value }) =>
                  !VALID_TRACKING.includes(value as (typeof VALID_TRACKING)[number])
                    ? `Invalid tracking: ${value}.`
                    : undefined,
              }}
              children={(field) => (
                <div>
                  <label
                    htmlFor={field.name}
                    className="block text-xs font-semibold uppercase tracking-wider text-[var(--sea-ink-soft)]"
                  >
                    Tracking System
                  </label>
                  <div className="relative mt-1">
                    <select
                      id={field.name}
                      name={field.name}
                      value={field.state.value}
                      onBlur={field.handleBlur}
                      onChange={(e) => field.handleChange(e.target.value)}
                      className="w-full rounded-xl border border-[var(--line)] bg-[var(--input-bg)] px-4 py-2 text-sm font-semibold text-[var(--sea-ink)] focus:border-amber-500 focus:outline-none"
                    >
                      <option value="fixed">Fixed Mount</option>
                      <option value="single_axis">Single-Axis Tracker</option>
                      <option value="dual_axis">Dual-Axis Tracker</option>
                    </select>
                  </div>
                  <FieldError field={field} />
                </div>
              )}
            />

            {/* Tilt & Azimuth Grid */}
            <div className="grid grid-cols-2 gap-3">
              <form.Field
                name="tilt_deg"
                validators={{
                  onChange: ({ value }) =>
                    typeof value !== 'number' || isNaN(value) || value < 0 || value > 90
                      ? 'Tilt angle must be between 0° and 90°.'
                      : undefined,
                }}
                children={(field) => (
                  <div>
                    <label
                      htmlFor={field.name}
                      className="block text-xs font-semibold uppercase tracking-wider text-[var(--sea-ink-soft)]"
                    >
                      Tilt Angle (°)
                    </label>
                    <input
                      id={field.name}
                      name={field.name}
                      type="number"
                      step="1"
                      min="0"
                      max="90"
                      value={field.state.value}
                      onBlur={field.handleBlur}
                      onChange={(e) => field.handleChange(parseFloat(e.target.value) || 0)}
                      className="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--input-bg)] px-3 py-2 text-sm font-semibold text-[var(--sea-ink)] focus:border-amber-500 focus:outline-none"
                    />
                    <FieldError field={field} />
                  </div>
                )}
              />

              <form.Field
                name="surface_azimuth_pvlib"
                validators={{
                  onChange: ({ value }) =>
                    typeof value !== 'number' || isNaN(value) || value < -180 || value > 360
                      ? 'Azimuth must be between -180° and 360°.'
                      : undefined,
                }}
                children={(field) => (
                  <div>
                    <label
                      htmlFor={field.name}
                      className="block text-xs font-semibold uppercase tracking-wider text-[var(--sea-ink-soft)]"
                    >
                      Azimuth (°)
                    </label>
                    <input
                      id={field.name}
                      name={field.name}
                      type="number"
                      step="1"
                      min="-180"
                      max="360"
                      value={field.state.value}
                      onBlur={field.handleBlur}
                      onChange={(e) => field.handleChange(parseFloat(e.target.value) || 0)}
                      className="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--input-bg)] px-3 py-2 text-sm font-semibold text-[var(--sea-ink)] focus:border-amber-500 focus:outline-none"
                    />
                    <FieldError field={field} />
                  </div>
                )}
              />
            </div>

            {/* Prediction Date */}
            <form.Field
              name="predict_date"
              validators={{
                onChange: ({ value }) =>
                  value && !/^\d{4}-\d{2}-\d{2}$/.test(value)
                    ? 'Predict date must follow YYYY-MM-DD format.'
                    : undefined,
              }}
              children={(field) => (
                <div>
                  <label
                    htmlFor={field.name}
                    className="block text-xs font-semibold uppercase tracking-wider text-[var(--sea-ink-soft)]"
                  >
                    Request Date (Predicting Tomorrow D+1)
                  </label>
                  <input
                    id={field.name}
                    name={field.name}
                    type="date"
                    value={field.state.value}
                    onBlur={field.handleBlur}
                    onChange={(e) => field.handleChange(e.target.value)}
                    className="mt-1 w-full rounded-xl border border-[var(--line)] bg-[var(--input-bg)] px-3 py-2 text-sm font-semibold text-[var(--sea-ink)] focus:border-amber-500 focus:outline-none"
                  />
                  <FieldError field={field} />
                </div>
              )}
            />

            <form.Subscribe
              selector={(state) => [state.canSubmit, state.isSubmitting]}
              children={([canSubmit, isSubmitting]) => (
                <button
                  type="submit"
                  disabled={!canSubmit || isSubmitting}
                  className="mt-4 flex w-full items-center justify-center gap-2 rounded-xl bg-amber-500 px-4 py-3 font-semibold text-white shadow-md transition hover:bg-amber-600 disabled:opacity-50"
                >
                  {isSubmitting ? (
                    <>
                      <span className="h-4 w-4 animate-spin rounded-full border-2 border-white border-t-transparent" />
                      Predicting via LSTM...
                    </>
                  ) : (
                    <>
                      <Zap className="h-4 w-4" />
                      Generate D+1 Forecast
                    </>
                  )}
                </button>
              )}
            />
          </form>

          {error && (
            <div
              role="alert"
              className="mt-4 flex items-start gap-2 rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-xs text-rose-600 dark:text-rose-400"
            >
              <AlertCircle className="h-4 w-4 flex-shrink-0" />
              <span>{error}</span>
            </div>
          )}
        </section>

        {/* Right Column: Results & Visualization */}
        <section className="space-y-6 lg:col-span-8">
          {result ? (
            <>
              {result.is_ood && (
                <div className="flex items-start gap-2 rounded-xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-xs text-amber-800 dark:text-amber-300">
                  <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0 text-amber-500" />
                  <span>
                    <b>Out-of-Distribution (OOD) Input Flagged:</b> Array capacity or tilt angle falls outside the DKASC reference training range (1–100 kWp, 0°–45° tilt). Prediction is extrapolated via capacity normalization and logged for OOD monitoring.
                  </span>
                </div>
              )}

              {/* KPI Cards */}
              <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                <div className="island-shell rounded-2xl p-4">
                  <div className="flex items-center justify-between text-xs text-[var(--sea-ink-soft)]">
                    <span>Total Generation</span>
                    <Zap className="h-4 w-4 text-amber-500" />
                  </div>
                  <div className="mt-2 text-2xl font-bold text-[var(--sea-ink)] sm:text-3xl">
                    {result.total_daily_kwh}
                    <span className="text-sm font-normal text-[var(--sea-ink-soft)] ml-1">
                      kWh
                    </span>
                  </div>
                  <div className="mt-1 text-xs text-emerald-600 dark:text-emerald-400 font-medium">
                    Day-ahead D+1 Total
                  </div>
                </div>

                <div className="island-shell rounded-2xl p-4">
                  <div className="flex items-center justify-between text-xs text-[var(--sea-ink-soft)]">
                    <span>Peak Power</span>
                    <TrendingUp className="h-4 w-4 text-blue-500" />
                  </div>
                  <div className="mt-2 text-2xl font-bold text-[var(--sea-ink)] sm:text-3xl">
                    {result.peak_kwh}
                    <span className="text-sm font-normal text-[var(--sea-ink-soft)] ml-1">
                      kWh
                    </span>
                  </div>
                  <div className="mt-1 text-xs text-[var(--sea-ink-soft)]">
                    At {result.peak_hour}
                  </div>
                </div>

                <div className="island-shell rounded-2xl p-4">
                  <div className="flex items-center justify-between text-xs text-[var(--sea-ink-soft)]">
                    <span>Specific Yield</span>
                    <Activity className="h-4 w-4 text-emerald-500" />
                  </div>
                  <div className="mt-2 text-2xl font-bold text-[var(--sea-ink)] sm:text-3xl">
                    {calculateSpecificYield(result.total_daily_kwh, result.kwp)}
                    <span className="text-sm font-normal text-[var(--sea-ink-soft)] ml-1">
                      kWh/kWp
                    </span>
                  </div>
                  <div className="mt-1 text-xs text-[var(--sea-ink-soft)]">
                    Productivity index
                  </div>
                </div>

                <div className="island-shell rounded-2xl p-4">
                  <div className="flex items-center justify-between text-xs text-[var(--sea-ink-soft)]">
                    <span>Target Date</span>
                    <Calendar className="h-4 w-4 text-purple-500" />
                  </div>
                  <div className="mt-2 text-base font-bold text-[var(--sea-ink)] truncate sm:text-lg">
                    {result.target_date}
                  </div>
                  <div className="mt-1 text-xs text-[var(--sea-ink-soft)]">
                    Model: {result.model_version}
                  </div>
                </div>
              </div>

              {/* 24-Hour Solar Production Chart */}
              <div className="island-shell rounded-2xl p-6">
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--line)] pb-4">
                  <div>
                    <h3 className="text-base font-bold text-[var(--sea-ink)]">
                      24-Hour Production Curve ({result.target_date})
                    </h3>
                    <p className="text-xs text-[var(--sea-ink-soft)]">
                      Predicted hourly generation profile based on Open-Meteo numerical weather forecasts.
                    </p>
                  </div>
                  <div className="flex items-center gap-3 text-xs">
                    <span className="flex items-center gap-1.5 text-[var(--sea-ink)]">
                      <span className="h-3 w-3 rounded bg-amber-500 inline-block" />
                      Daytime Output
                    </span>
                    <span className="flex items-center gap-1.5 text-[var(--sea-ink-soft)]">
                      <span className="h-3 w-3 rounded bg-slate-400/40 dark:bg-slate-600/60 inline-block" />
                      Night
                    </span>
                  </div>
                </div>

                {/* Bar Graph */}
                <div className="mt-6 flex h-48 items-end gap-1.5 pt-6 sm:gap-2">
                  {result.hourly_breakdown.map((h, i) => {
                    const heightPercent = maxKwh > 0 ? (h.kwh / maxKwh) * 100 : 0
                    const isDay = isDaytimeHour(i)
                    return (
                      <div
                        key={h.hour}
                        className="group relative flex h-full flex-1 flex-col items-center justify-end"
                      >
                        {/* Hover Tooltip */}
                        <div className="pointer-events-none absolute -top-12 z-20 hidden rounded-lg border border-[var(--line)] bg-[var(--surface-strong)] px-2.5 py-1 text-[10px] font-semibold text-[var(--sea-ink)] shadow-lg group-hover:flex flex-col items-center whitespace-nowrap">
                          <span>{h.hour}: {h.kwh} kWh</span>
                          <span className="text-[var(--sea-ink-soft)]">{h.ghi_fc} W/m²</span>
                        </div>

                        {/* Bar */}
                        <div
                          style={{ height: `${Math.max(heightPercent, 2)}%` }}
                          className={`w-full rounded-t-sm transition-all duration-300 ${
                            isDay
                              ? h.kwh === result.peak_kwh
                                ? 'bg-amber-400 ring-2 ring-amber-500'
                                : 'bg-amber-500 hover:bg-amber-400'
                              : 'bg-slate-400/40 dark:bg-slate-600/60'
                          }`}
                        />
                        <span className="mt-2 text-[9px] text-[var(--sea-ink-soft)]">
                          {i % 3 === 0 ? h.hour.split(':')[0] : ''}
                        </span>
                      </div>
                    )
                  })}
                </div>
              </div>

              {/* Hourly Data Table */}
              <div className="island-shell overflow-hidden rounded-2xl">
                <div className="border-b border-[var(--line)] p-4 sm:px-6">
                  <h3 className="text-sm font-bold text-[var(--sea-ink)]">
                    Detailed Hourly Breakdown (Tomorrow)
                  </h3>
                </div>
                <div className="max-h-80 overflow-y-auto">
                  <table className="w-full text-left text-xs">
                    <thead className="sticky top-0 bg-[var(--header-bg)] text-[var(--sea-ink-soft)]">
                      <tr className="border-b border-[var(--line)]">
                        <th className="px-4 py-2 font-semibold">Hour</th>
                        <th className="px-4 py-2 font-semibold">GHI Irradiance</th>
                        <th className="px-4 py-2 font-semibold">Normalized Output</th>
                        <th className="px-4 py-2 font-semibold text-right">Predicted Energy</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[var(--line)] text-[var(--sea-ink)]">
                      {result.hourly_breakdown.map((row) => (
                        <tr
                          key={row.hour}
                          className={
                            row.kwh === result.peak_kwh
                              ? 'bg-amber-500/10 font-semibold'
                              : 'hover:bg-slate-500/5'
                          }
                        >
                          <td className="px-4 py-2 flex items-center gap-1.5">
                            <Clock className="h-3 w-3 text-[var(--sea-ink-soft)]" />
                            {row.hour}
                          </td>
                          <td className="px-4 py-2">{row.ghi_fc} W/m²</td>
                          <td className="px-4 py-2">{row.target_norm} kW/kWp</td>
                          <td className="px-4 py-2 text-right font-medium">
                            {row.kwh} kWh
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* Provenance Footer */}
              <div className="flex items-center justify-between rounded-xl border border-emerald-500/20 bg-emerald-500/5 px-4 py-3 text-xs text-emerald-700 dark:text-emerald-300">
                <span className="flex items-center gap-2">
                  <CheckCircle2 className="h-4 w-4 text-emerald-500" />
                  Logged to Supabase <code>prediction_logs</code> for future nMAE drift evaluation.
                </span>
                <span className="font-mono text-[11px] opacity-75">
                  Model: {result.model_version}
                </span>
              </div>
            </>
          ) : (
            /* Empty State */
            <div className="island-shell flex h-96 flex-col items-center justify-center rounded-2xl p-8 text-center">
              <Sun className="h-12 w-12 text-amber-500/40 animate-pulse" />
              <h3 className="mt-4 text-base font-bold text-[var(--sea-ink)]">
                Ready for Forecasting
              </h3>
              <p className="mt-2 max-w-md text-xs text-[var(--sea-ink-soft)]">
                Fill in your solar array parameters on the left and click <b>Generate D+1 Forecast</b> to compute tomorrow's energy production profile.
              </p>
            </div>
          )}
        </section>
      </div>
    </main>
  )
}
