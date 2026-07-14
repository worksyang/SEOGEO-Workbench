export interface ApiErrorBody {
  error?: {
    code?: string
    message?: string
    request_id?: string
  }
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
  ) {
    super(message)
  }
}

function assertApiOk(body: unknown, status: number): void {
  if (body && typeof body === 'object' && (body as {ok?: unknown}).ok === false) {
    const error = (body as ApiErrorBody).error
    throw new ApiError(status, error?.code ?? 'API_ERROR', error?.message ?? '服务返回失败。')
  }
}

export async function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    method: 'GET',
    headers: {'Accept': 'application/json'},
    signal,
  })
  const body = (await response.json().catch(() => null)) as T | ApiErrorBody | null
  if (!response.ok) {
    const error = body as ApiErrorBody | null
    throw new ApiError(
      response.status,
      error?.error?.code ?? 'HTTP_ERROR',
      error?.error?.message ?? `请求失败（${response.status}）`,
    )
  }
  assertApiOk(body, response.status)
  return body as T
}

export async function apiRequest<T>(
  path: string,
  method: 'POST' | 'PATCH',
  payload?: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
    body: JSON.stringify(payload ?? {}),
    signal,
  })
  const body = (await response.json().catch(() => null)) as T | ApiErrorBody | null
  if (!response.ok) {
    const error = body as ApiErrorBody | null
    throw new ApiError(
      response.status,
      error?.error?.code ?? 'HTTP_ERROR',
      error?.error?.message ?? `请求失败（${response.status}）`,
    )
  }
  assertApiOk(body, response.status)
  return body as T
}
