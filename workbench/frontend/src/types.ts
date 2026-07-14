export interface SystemConnection {
  system_key: string
  display_name: string
  status: 'healthy' | 'degraded' | 'offline' | 'blocked' | 'unknown'
  last_checked_at: string | null
}

export interface OverviewResponse {
  ok: boolean
  data: {
    counts: {
      contents: number
      creators: number
      snapshots: number
      observations: number
      geo_answers: number
      signals: number
      jobs: number
    }
    systems: SystemConnection[]
    data_state: 'empty' | 'ready'
  }
}

export interface StatusResponse {
  ok: boolean
  data: {
    service: {
      name: string
      version: string
      bind: string
      frontend_built: boolean
    }
    database: {
      status: 'healthy' | 'degraded' | 'offline'
      integrity?: string
      schema_version?: number
      missing_core_tables?: string[]
      error?: string
    }
    connections: SystemConnection[]
    readonly_contract: {
      source: boolean
      demo: boolean
    }
  }
}

export type WechatSourceStatus = 'healthy' | 'degraded' | 'offline' | 'unknown' | string

export interface WechatBootstrapResponse {
  ok?: boolean
  data?: {
    source_status?: WechatSourceStatus | null
    summary?: Record<string, unknown> | null
    keywords?: unknown[] | null
    updated_at?: string | null
    [key: string]: unknown
  } | null
}

export interface WechatKeywordResponse {
  ok?: boolean
  data?: Record<string, unknown> | null
}

export interface WechatArticleResponse {
  ok?: boolean
  data?: Record<string, unknown> | null
}
