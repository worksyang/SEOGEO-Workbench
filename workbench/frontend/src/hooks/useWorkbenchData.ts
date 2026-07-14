import {useEffect, useState} from 'react'
import {apiGet} from '../api/client'
import type {OverviewResponse, StatusResponse} from '../types'

interface WorkbenchData {
  overview: OverviewResponse['data'] | null
  status: StatusResponse['data'] | null
  loading: boolean
  error: string | null
  reload: () => void
}

export function useWorkbenchData(): WorkbenchData {
  const [overview, setOverview] = useState<OverviewResponse['data'] | null>(null)
  const [status, setStatus] = useState<StatusResponse['data'] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    Promise.all([
      apiGet<OverviewResponse>('/api/v1/overview', controller.signal),
      apiGet<StatusResponse>('/api/v1/system/status', controller.signal),
    ])
      .then(([overviewResult, statusResult]) => {
        setOverview(overviewResult.data)
        setStatus(statusResult.data)
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === 'AbortError') return
        setError(reason instanceof Error ? reason.message : '无法读取工作台状态')
      })
      .finally(() => setLoading(false))
    return () => controller.abort()
  }, [revision])

  return {
    overview,
    status,
    loading,
    error,
    reload: () => setRevision((current) => current + 1),
  }
}
