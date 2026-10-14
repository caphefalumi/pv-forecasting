import { createServerFn } from '@tanstack/react-start'
import {
  type ArrayParameters,
  type ForecastResponse,
  formatApiError,
  validateArrayParameters,
} from './forecast-utils'

/**
 * Server Function: invokes the FastAPI /api/v1/forecast endpoint running
 * locally or behind the AWS Application Load Balancer.
 */
export const getForecastServerFn = createServerFn({ method: 'POST' })
  .validator((data: ArrayParameters) => {
    const check = validateArrayParameters(data)
    if (!check.valid) {
      throw new Error(check.errors.join(' '))
    }
    return data
  })
  .handler(async ({ data }): Promise<ForecastResponse> => {
    const backendUrl = (process.env.BACKEND_API_URL || 'http://127.0.0.1:8000').replace(/\/$/, '')
    const requestId = crypto.randomUUID()

    try {
      const res = await fetch(`${backendUrl}/api/v1/forecast`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Request-ID': requestId,
        },
        body: JSON.stringify(data),
        signal: AbortSignal.timeout(10000),
      })

      if (!res.ok) {
        let detail = await res.text()
        try {
          const parsed = JSON.parse(detail)
          if (parsed?.detail) {
            detail = typeof parsed.detail === 'string' ? parsed.detail : JSON.stringify(parsed.detail)
          }
        } catch {
          // keep raw text
        }
        throw new Error(formatApiError(res.status, detail))
      }

      return (await res.json()) as ForecastResponse
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Unable to connect to FastAPI forecasting backend'
      throw new Error(message)
    }
  })
