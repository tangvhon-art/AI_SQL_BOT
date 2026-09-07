// axios 实例：JWT 注入 + 统一错误提示
import axios from 'axios'

export const TOKEN_KEY = 'ai_sql_bot_token'

export const client = axios.create({
  baseURL: '/api/v1',
  timeout: 60000,
})

client.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

client.interceptors.response.use(
  (resp) => resp,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem(TOKEN_KEY)
      if (!location.pathname.startsWith('/login')) location.href = '/login'
    }
    return Promise.reject(error)
  },
)

export function errMsg(error: unknown): string {
  const e = error as { response?: { data?: { detail?: string | { msg?: string } } } }
  if (e.response?.data?.detail) {
    const d = e.response.data.detail
    return typeof d === 'string' ? d : (d.msg ?? '请求失败')
  }
  return (error as Error)?.message || '网络错误'
}
