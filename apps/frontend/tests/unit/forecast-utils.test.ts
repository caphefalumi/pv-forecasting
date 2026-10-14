import { describe, expect, it } from 'bun:test'
import {
  calculateSpecificYield,
  validateArrayParameters,
  isDaytimeHour,
  findPeakGeneration,
  sumDailyKwh,
  type HourlyPoint,
  type ArrayParameters,
} from '../../src/lib/forecast-utils'

describe('PV Forecasting Domain Logic (bun:test)', () => {
  describe('calculateSpecificYield', () => {
    it('computes specific yield accurately for standard array sizes', () => {
      expect(calculateSpecificYield(55.5, 10.0)).toBe(5.55)
      expect(calculateSpecificYield(120.0, 30.0)).toBe(4.0)
    })

    it('returns 0 when kwp is zero or negative', () => {
      expect(calculateSpecificYield(50, 0)).toBe(0)
      expect(calculateSpecificYield(50, -5)).toBe(0)
    })

    it('returns 0 when total daily kWh is zero or negative', () => {
      expect(calculateSpecificYield(0, 10)).toBe(0)
      expect(calculateSpecificYield(-10, 10)).toBe(0)
    })

    it('rounds to 2 decimal places', () => {
      expect(calculateSpecificYield(10, 3)).toBe(3.33)
    })
  })

  describe('validateArrayParameters', () => {
    const validParams: ArrayParameters = {
      kwp: 15.0,
      technology: 'mono-Si',
      tracking: 'fixed',
      tilt_deg: 20.0,
      surface_azimuth_pvlib: 0.0,
      predict_date: '2026-10-15',
    }

    it('passes for standard valid parameters', () => {
      const result = validateArrayParameters(validParams)
      expect(result.valid).toBe(true)
      expect(result.errors).toHaveLength(0)
    })

    it('accepts all 7 backend-supported PV technologies and 3 tracking modes', () => {
      const techs = ['mono-Si', 'poly-Si', 'CdTe', 'CIGS', 'HIT', 'a-Si', 'CPV']
      for (const tech of techs) {
        expect(validateArrayParameters({ ...validParams, technology: tech }).valid).toBe(true)
      }
      const trackings = ['fixed', 'single_axis', 'dual_axis']
      for (const tr of trackings) {
        expect(validateArrayParameters({ ...validParams, tracking: tr }).valid).toBe(true)
      }
    })

    it('flags non-positive or missing kwp', () => {
      const resZero = validateArrayParameters({ ...validParams, kwp: 0 })
      expect(resZero.valid).toBe(false)
      expect(resZero.errors.some((e) => e.includes('greater than 0'))).toBe(true)

      const resNeg = validateArrayParameters({ ...validParams, kwp: -10 })
      expect(resNeg.valid).toBe(false)
    })

    it('flags capacity exceeding max allowable fleet limit', () => {
      const res = validateArrayParameters({ ...validParams, kwp: 10000 })
      expect(res.valid).toBe(false)
      expect(res.errors.some((e) => e.includes('5000'))).toBe(true)
    })

    it('validates tilt angle boundary constraints [0, 90]', () => {
      expect(validateArrayParameters({ ...validParams, tilt_deg: 0 }).valid).toBe(true)
      expect(validateArrayParameters({ ...validParams, tilt_deg: 90 }).valid).toBe(true)
      expect(validateArrayParameters({ ...validParams, tilt_deg: -5 }).valid).toBe(false)
      expect(validateArrayParameters({ ...validParams, tilt_deg: 95 }).valid).toBe(false)
    })

    it('validates surface azimuth boundary constraints [-180, 360]', () => {
      expect(validateArrayParameters({ ...validParams, surface_azimuth_pvlib: -180 }).valid).toBe(true)
      expect(validateArrayParameters({ ...validParams, surface_azimuth_pvlib: 360 }).valid).toBe(true)
      expect(validateArrayParameters({ ...validParams, surface_azimuth_pvlib: -200 }).valid).toBe(false)
      expect(validateArrayParameters({ ...validParams, surface_azimuth_pvlib: 400 }).valid).toBe(false)
    })

    it('flags invalid PV cell technology', () => {
      const res = validateArrayParameters({ ...validParams, technology: 'Perovskite-Lab' })
      expect(res.valid).toBe(false)
      expect(res.errors.some((e) => e.includes('Invalid technology'))).toBe(true)
    })

    it('flags invalid tracking system', () => {
      const res = validateArrayParameters({ ...validParams, tracking: 'tri-axial' })
      expect(res.valid).toBe(false)
      expect(res.errors.some((e) => e.includes('Invalid tracking'))).toBe(true)
    })

    it('validates predict_date format (YYYY-MM-DD)', () => {
      expect(validateArrayParameters({ ...validParams, predict_date: '2026-10-25' }).valid).toBe(true)
      expect(validateArrayParameters({ ...validParams, predict_date: '10/25/2026' }).valid).toBe(false)
      expect(validateArrayParameters({ ...validParams, predict_date: 'invalid-date' }).valid).toBe(false)
    })
  })

  describe('isDaytimeHour', () => {
    it('correctly classifies day hours between 06:00 and 18:00', () => {
      expect(isDaytimeHour(6)).toBe(true)
      expect(isDaytimeHour(12)).toBe(true)
      expect(isDaytimeHour(18)).toBe(true)
    })

    it('correctly classifies night hours outside 06:00-18:00', () => {
      expect(isDaytimeHour(0)).toBe(false)
      expect(isDaytimeHour(3)).toBe(false)
      expect(isDaytimeHour(5)).toBe(false)
      expect(isDaytimeHour(19)).toBe(false)
      expect(isDaytimeHour(23)).toBe(false)
    })
  })

  describe('findPeakGeneration & sumDailyKwh', () => {
    const mockHourly: HourlyPoint[] = [
      { hour: '06:00', ghi_fc: 50, target_norm: 0.05, kwh: 0.5 },
      { hour: '12:00', ghi_fc: 950, target_norm: 0.85, kwh: 8.5 },
      { hour: '13:00', ghi_fc: 980, target_norm: 0.90, kwh: 9.0 },
      { hour: '18:00', ghi_fc: 100, target_norm: 0.10, kwh: 1.0 },
    ]

    it('identifies the peak hour and power', () => {
      const peak = findPeakGeneration(mockHourly)
      expect(peak.peakKwh).toBe(9.0)
      expect(peak.peakHour).toBe('13:00')
    })

    it('sums daily kWh correctly', () => {
      const total = sumDailyKwh(mockHourly)
      expect(total).toBe(19.0)
    })

    it('handles empty hourly arrays gracefully', () => {
      expect(findPeakGeneration([])).toEqual({ peakKwh: 0, peakHour: '00:00' })
      expect(sumDailyKwh([])).toBe(0)
    })
  })
})
