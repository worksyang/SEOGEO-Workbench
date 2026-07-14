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
  return body as T
}
