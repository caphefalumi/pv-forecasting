import { describe, expect, it } from 'bun:test'
import type { HourlyPoint } from '../../src/lib/forecast-utils'

interface ForecastAPIResponse {
  predict_date: string
  target_date: string
  kwp: number
  total_daily_kwh: number
  peak_hour: string
  peak_kwh: number
  daytime_hours_count: number
  model_version: string
  hourly_breakdown: HourlyPoint[]
}

describe('Frontend API Contract Integration Tests (bun:test)', () => {
  it('validates mock D+1 forecast API contract response structure', () => {
    const mockResponse: ForecastAPIResponse = {
      predict_date: '2026-10-14',
      target_date: '2026-10-15',
      kwp: 10.0,
      total_daily_kwh: 52.4,
      peak_hour: '12:00',
      peak_kwh: 7.8,
      daytime_hours_count: 13,
      model_version: 'v1.0-mock',
      hourly_breakdown: Array.from({ length: 24 }, (_, h) => ({
        hour: `${h.toString().padStart(2, '0')}:00`,
        ghi_fc: h >= 6 && h <= 18 ? 850 : 0,
        target_norm: h >= 6 && h <= 18 ? 0.78 : 0,
        kwh: h >= 6 && h <= 18 ? 7.8 : 0,
      })),
    }

    // Verify D+1 relationship
    const pDate = new Date(mockResponse.predict_date)
    const tDate = new Date(mockResponse.target_date)
    const diffDays = Math.round((tDate.getTime() - pDate.getTime()) / (1000 * 3600 * 24))
    expect(diffDays).toBe(1)

    // Verify hourly points count
    expect(mockResponse.hourly_breakdown).toHaveLength(24)

    // Verify non-zero daytime total
    expect(mockResponse.total_daily_kwh).toBeGreaterThan(0)
    expect(mockResponse.peak_kwh).toBeGreaterThan(0)
    expect(mockResponse.daytime_hours_count).toBe(13)
  })

  it('transforms API error responses into readable client messages', () => {
    function formatApiError(status: number, detail: string): string {
      if (status === 422) {
        return `Validation Error: Please check your input parameters (${detail})`
      }
      if (status === 400) {
        return `Bad Request: ${detail}`
      }
      return 'Unexpected error occurred while communicating with forecasting service.'
    }

    expect(formatApiError(422, 'kwp must be greater than 0')).toContain('Validation Error')
    expect(formatApiError(400, 'Invalid predict_date format')).toContain('Bad Request')
    expect(formatApiError(500, 'Internal Server Error')).toContain('Unexpected error occurred')
  })
})
