export interface HourlyPoint {
  hour: string
  ghi_fc: number
  target_norm: number
  kwh: number
}

export const VALID_TECHNOLOGIES = [
  'mono-Si',
  'poly-Si',
  'CdTe',
  'CIGS',
  'HIT',
  'a-Si',
  'CPV',
] as const

export const VALID_TRACKING = ['fixed', 'single_axis', 'dual_axis'] as const

export type Technology = (typeof VALID_TECHNOLOGIES)[number]
export type Tracking = (typeof VALID_TRACKING)[number]

export interface ArrayParameters {
  kwp: number
  technology: string
  tracking: string
  tilt_deg: number
  surface_azimuth_pvlib: number
  predict_date?: string
}

export interface ForecastResponse {
  predict_date: string
  target_date: string
  kwp: number
  total_daily_kwh: number
  peak_hour: string
  peak_kwh: number
  daytime_hours_count: number
  model_version: string
  is_ood?: boolean
  hourly_breakdown: HourlyPoint[]
}

export function calculateSpecificYield(totalDailyKwh: number, kwp: number): number {
  if (!kwp || kwp <= 0) return 0
  if (!totalDailyKwh || totalDailyKwh < 0) return 0
  return Number((totalDailyKwh / kwp).toFixed(2))
}

export function validateArrayParameters(params: ArrayParameters): {
  valid: boolean
  errors: string[]
} {
  const errors: string[] = []

  if (typeof params.kwp !== 'number' || Number.isNaN(params.kwp) || params.kwp <= 0) {
    errors.push('Peak capacity (kWp) must be greater than 0.')
  } else if (params.kwp > 5000) {
    errors.push('Peak capacity (kWp) exceeds maximum allowable fleet size of 5000 kWp.')
  }

  if (
    typeof params.tilt_deg !== 'number' ||
    Number.isNaN(params.tilt_deg) ||
    params.tilt_deg < 0 ||
    params.tilt_deg > 90
  ) {
    errors.push('Tilt angle must be between 0° and 90°.')
  }

  if (
    typeof params.surface_azimuth_pvlib !== 'number' ||
    Number.isNaN(params.surface_azimuth_pvlib) ||
    params.surface_azimuth_pvlib < -180 ||
    params.surface_azimuth_pvlib > 360
  ) {
    errors.push('Azimuth must be between -180° and 360°.')
  }

  if (!(VALID_TECHNOLOGIES as readonly string[]).includes(params.technology)) {
    errors.push(`Invalid technology: ${params.technology}. Valid: ${VALID_TECHNOLOGIES.join(', ')}`)
  }

  if (!(VALID_TRACKING as readonly string[]).includes(params.tracking)) {
    errors.push(`Invalid tracking: ${params.tracking}. Valid: ${VALID_TRACKING.join(', ')}`)
  }

  if (params.predict_date) {
    const dateRegex = /^\d{4}-\d{2}-\d{2}$/
    if (!dateRegex.test(params.predict_date)) {
      errors.push('Predict date must follow YYYY-MM-DD format.')
    }
  }

  return {
    valid: errors.length === 0,
    errors,
  }
}

export function isDaytimeHour(hourIndex: number): boolean {
  return hourIndex >= 6 && hourIndex <= 18
}

export function findPeakGeneration(hourlyBreakdown: HourlyPoint[]): {
  peakKwh: number
  peakHour: string
} {
  if (!hourlyBreakdown || hourlyBreakdown.length === 0) {
    return { peakKwh: 0, peakHour: '00:00' }
  }

  let maxKwh = -1
  let peakHour = hourlyBreakdown[0].hour

  for (const p of hourlyBreakdown) {
    if (p.kwh > maxKwh) {
      maxKwh = p.kwh
      peakHour = p.hour
    }
  }

  return {
    peakKwh: Math.max(0, maxKwh),
    peakHour,
  }
}

export function sumDailyKwh(hourlyBreakdown: HourlyPoint[]): number {
  if (!hourlyBreakdown || hourlyBreakdown.length === 0) return 0
  const sum = hourlyBreakdown.reduce((acc, curr) => acc + (curr.kwh || 0), 0)
  return Number(sum.toFixed(2))
}

export function formatApiError(status: number, detail: string): string {
  if (status === 422) {
    return `Validation Error: Please check your input parameters (${detail})`
  }
  if (status === 400) {
    return `Bad Request: ${detail}`
  }
  return 'Unexpected error occurred while communicating with forecasting service.'
}
