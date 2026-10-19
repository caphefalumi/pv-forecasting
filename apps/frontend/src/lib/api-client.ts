import axios, { AxiosError } from 'axios'
import {
  type ArrayParameters,
  type ForecastResponse,
  formatApiError,
  validateArrayParameters,
} from './forecast-utils'

export const apiClient = axios.create({
  // Empty in local Vite (proxy /api → FastAPI). Set VITE_BACKEND_API_URL for prod ALB.
  baseURL: (import.meta.env.VITE_BACKEND_API_URL || '').replace(/\/$/, ''),
  timeout: 10000,
  headers: {
    'Content-Type': 'application/json',
  },
})

apiClient.interceptors.request.use((config) => {
  config.headers.set('X-Request-ID', config.headers.get('X-Request-ID') || crypto.randomUUID())
  return config
})

apiClient.interceptors.response.use(
  (response) => response,
  (error: unknown) => {
    if (axios.isAxiosError(error)) {
      const axiosErr = error as AxiosError<{ detail?: unknown } | string>
      if (axiosErr.response) {
        const { status, data } = axiosErr.response
        let detail = ''
        if (typeof data === 'string') {
          detail = data
        } else if (data && typeof data === 'object' && 'detail' in data && data.detail !== undefined) {
          detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)
        } else if (data !== undefined) {
          detail = JSON.stringify(data)
        }
        return Promise.reject(new Error(formatApiError(status, detail)))
      }
      return Promise.reject(
        new Error(axiosErr.message || 'Unable to connect to FastAPI forecasting backend'),
      )
    }
    return Promise.reject(
      error instanceof Error ? error : new Error('Unable to connect to FastAPI forecasting backend'),
    )
  },
)

export async function getForecast(params: ArrayParameters): Promise<ForecastResponse> {
  const check = validateArrayParameters(params)
  if (!check.valid) {
    throw new Error(check.errors.join(' '))
  }
  const res = await apiClient.post<ForecastResponse>('/api/v1/forecast', params)
  return res.data
}
