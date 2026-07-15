const API_BASE = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:9090').replace(/\/$/, '')
const TOKEN_KEY = 'authToken'

export function hasAuthToken() {
  return Boolean(localStorage.getItem(TOKEN_KEY))
}

export function setAuthToken(token) {
  if (!token) throw new Error('登录接口未返回有效令牌')
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearAuthToken() {
  localStorage.removeItem(TOKEN_KEY)
}

export async function apiRequest(path, options = {}) {
  const { timeoutMs = 0, ...fetchOptions } = options
  const headers = new Headers(fetchOptions.headers || {})
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) headers.set('Authorization', `Bearer ${token}`)

  const controller = timeoutMs > 0 && !fetchOptions.signal ? new AbortController() : null
  const timeout = controller ? setTimeout(() => controller.abort(), timeoutMs) : null
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...fetchOptions,
      headers,
      signal: controller?.signal || fetchOptions.signal
    })
    if (response.status === 401 && !path.startsWith('/user/')) {
      clearAuthToken()
      window.dispatchEvent(new Event('auth-expired'))
    }
    return response
  } catch (error) {
    if (controller?.signal.aborted) throw new Error(`请求超过 ${Math.ceil(timeoutMs / 1000)} 秒未响应`)
    throw error
  } finally {
    if (timeout) clearTimeout(timeout)
  }
}
