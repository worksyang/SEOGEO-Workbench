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

export type MpSourceStatus = 'healthy' | 'degraded' | 'offline' | 'unknown' | string

export interface MpBootstrapData {
  source_status?: {
    status?: MpSourceStatus
    inconsistent?: boolean
    logged_in?: boolean | null
    display_status?: string | null
    message?: string | null
    errors?: Record<string, unknown>
  } | null
  summary?: {
    account_count?: number
    category_count?: number
    job_count?: number
    imported_article_count?: number
  } | null
  accounts?: unknown[] | null
  categories?: unknown[] | null
  jobs?: unknown[] | null
  hub_articles?: unknown[] | null
  auth?: Record<string, unknown> | null
  [key: string]: unknown
}

export interface MpBootstrapResponse { ok?: boolean; data?: MpBootstrapData | null }
export interface MpArticlesResponse { ok?: boolean; data?: Record<string, unknown> | null }

export type XhsStatus = 'healthy' | 'degraded' | 'offline' | 'unknown' | string
export type JsonRecord = Record<string, unknown>

export interface XhsSourceStatus {
  status?: XhsStatus
  source?: string
  error?: string | null
}

export interface XhsCounts {
  keywords: number
  accounts: number
  snapshots: number
  ranking_hits: number
  articles: number
  snapshot_terms: number
}

export interface XhsKeyword {
  keyword_id?: string
  keyword?: string
  status?: string
  topic?: string | null
  keyword_bucket?: string | null
  payload?: JsonRecord
  payload_json?: string
  [key: string]: unknown
}

export interface XhsSnapshot {
  snapshot_id?: string
  keyword_id?: string
  keyword?: string
  captured_at?: string
  features?: JsonRecord
  payload?: JsonRecord
  hits?: XhsHit[]
  [key: string]: unknown
}

export interface XhsRun {
  id?: string
  captured_at?: string
  terms?: {
    suggestions?: unknown[]
    related?: unknown[]
  }
  articles?: XhsArticle[]
  [key: string]: unknown
}

export interface XhsHit {
  hit_id?: string
  snapshot_id?: string
  rank?: number
  content_id?: string | null
  title_raw?: string | null
  url_raw?: string | null
  creator_name_raw?: string | null
  payload?: JsonRecord
  [key: string]: unknown
}

export interface XhsArticle {
  content_id?: string
  article_id?: string
  title?: string | null
  canonical_url?: string | null
  creator_id?: string | null
  author_name?: string | null
  published_at?: string | null
  payload?: JsonRecord
  hits?: XhsHit[]
  observations?: XhsObservation[]
  [key: string]: unknown
}

export interface XhsObservation {
  observation_id?: string
  subject_id?: string
  metric_key?: string
  observed_at?: string
  numeric_value?: number | null
  payload?: JsonRecord
  [key: string]: unknown
}

export interface XhsBootstrapData {
  source_status?: XhsSourceStatus
  counts?: XhsCounts
  keywords?: XhsKeyword[]
  accounts?: JsonRecord[]
  snapshots?: XhsSnapshot[]
  runs?: XhsRun[]
  ranking_hits?: XhsHit[]
  articles?: XhsArticle[]
  snapshot_terms?: JsonRecord[]
}

export interface XhsApiEnvelope<T> {
  ok?: boolean
  data?: T
}

export type GeoSourceStatus = 'healthy' | 'ready' | 'online' | 'degraded' | 'partial' | 'offline' | 'unavailable' | 'unknown' | string

export interface GeoSnapshot {
  id: string
  status: string
  captured_at: string | null
  markdown_available: boolean
  relation_count: number
  source_count: number
  platform_count: number
  creator_count: number
  relation_type_counts: Record<string, number>
  [key: string]: unknown
}

export interface GeoQuestion {
  question_id: string
  question: string
  answer_count: number
  first_captured_at: string | null
  latest_captured_at: string | null
  latest_answer_id: string | null
  status_counts: Record<string, number>
  answers: GeoSnapshot[]
  [key: string]: unknown
}

export interface GeoBootstrapData {
  source_status?: GeoSourceStatus | {status?: GeoSourceStatus; message?: string; [key: string]: unknown} | null
  counts?: Record<string, number>
  hub?: Record<string, unknown> | null
  redfox?: Record<string, unknown> | null
  refresh?: Record<string, unknown> | null
  [key: string]: unknown
}

export interface GeoQuestionDetail {
  summary?: Record<string, unknown> | null
  snapshots?: GeoSnapshot[]
  citation_matrix?: {
    columns?: Array<{
      answer_id?: string
      captured_at?: string | null
      status?: string | null
      [key: string]: unknown
    }>
    rows?: Array<{label?: string; name?: string; ranks: Array<number | null>; [key: string]: unknown}>
  } | null
  totals?: Record<string, number> | null
  [key: string]: unknown
}

export interface GeoAnswer {
  id: string
  question_id?: string | null
  snapshot_id?: string | null
  status?: string | null
  captured_at?: string | null
  markdown?: string | {exists?: boolean; content?: string | null; path?: string | null} | null
  tools_nested?: unknown
  citations?: unknown
  citation_type_counts?: Record<string, number> | null
  platform_summary?: unknown
  creator_summary?: unknown
  metrics_observed_at?: string | null
  [key: string]: unknown
}

export interface GeoSourceOverview {
  totals?: Record<string, number>
  platforms?: unknown[]
  creators?: unknown[]
  [key: string]: unknown
}

export interface GeoRefreshResult {
  available?: boolean
  blocked_reason?: string | null
  message?: string | null
  [key: string]: unknown
}
