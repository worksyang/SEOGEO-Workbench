import {useEffect, useMemo, useRef, useState} from 'react'
import {apiGet, apiRequest, ApiError} from '../../api/client'

type TreeNode = {
  bucket: string
  name: string
  path: string
  relative_path: string
  files: WikiEntry[]
  sub_dirs: TreeNode[]
}
type WikiEntry = {
  content_id: string
  title: string
  excerpt: string
  path: string
  relative_path: string
  bucket: string
  category: string
  word_count: number
  has_image: boolean
  updated_at: string
}

type PageState = 'loading' | 'ready' | 'empty' | 'offline' | 'error'

function record(value: unknown, fallback: Record<string, unknown> = {}): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : fallback
}
function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : fallback
}
function dateText(value: unknown): string {
  const raw = text(value)
  if (!raw) return '—'
  const dt = new Date(raw)
  if (Number.isNaN(dt.getTime())) return raw
  return new Intl.DateTimeFormat('zh-CN', {dateStyle: 'short', timeStyle: 'short'}).format(dt)
}

function flatten(nodes: TreeNode[]): WikiEntry[] {
  const out: WikiEntry[] = []
  const stack = [...nodes]
  while (stack.length) {
    const node = stack.pop()
    if (!node) continue
    out.push(...(node.files || []))
    for (const sub of node.sub_dirs || []) stack.push(sub)
  }
  return out
}

export default function WikiPage() {
  const [state, setState] = useState<PageState>('loading')
  const [error, setError] = useState('')
  const [tree, setTree] = useState<TreeNode[]>([])
  const [query, setQuery] = useState('')
  const [searchResults, setSearchResults] = useState<WikiEntry[] | null>(null)
  const [activeId, setActiveId] = useState('')
  const [body, setBody] = useState('')
  const [editing, setEditing] = useState(false)
  const [saveMessage, setSaveMessage] = useState('')
  const [saving, setSaving] = useState(false)
  const detailRef = useRef<HTMLDivElement>(null)

  const allEntries = useMemo(() => flatten(tree), [tree])

  useEffect(() => {
    const controller = new AbortController()
    setState('loading')
    apiGet<{ok: boolean; data: TreeNode[]}>('/api/v1/wiki/tree', controller.signal)
      .then((res) => {
        const data = Array.isArray(res?.data) ? res.data : []
        setTree(data)
        setState(data.length ? 'ready' : 'empty')
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError) {
          setError(err.message)
          setState('offline')
        } else {
          setError(String(err))
          setState('error')
        }
      })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    if (!query.trim()) {
      setSearchResults(null)
      return
    }
    const controller = new AbortController()
    const handle = setTimeout(() => {
      apiGet<{ok: boolean; data: {items: WikiEntry[]}}>(
        `/api/v1/wiki/search?query=${encodeURIComponent(query.trim())}&limit=40`,
        controller.signal,
      )
        .then((res) => setSearchResults(res?.data?.items || []))
        .catch(() => setSearchResults([]))
    }, 220)
    return () => {
      controller.abort()
      clearTimeout(handle)
    }
  }, [query])

  useEffect(() => {
    if (activeId) return
    const entries = searchResults ?? allEntries
    if (entries[0]) setActiveId(entries[0].content_id)
  }, [activeId, allEntries, searchResults])

  useEffect(() => {
    if (!activeId) {
      setBody('')
      return
    }
    const controller = new AbortController()
    apiGet<{ok: boolean; data: {body: string; title: string; entry: WikiEntry}}>(
      `/api/v1/wiki/${encodeURIComponent(activeId)}`,
      controller.signal,
    )
      .then((res) => {
        setBody(res?.data?.body || '')
        setEditing(false)
        setSaveMessage('')
      })
      .catch(() => setBody(''))
    return () => controller.abort()
  }, [activeId])

  const visibleEntries = searchResults ?? allEntries

  const beginEdit = () => {
    if (!activeId) return
    setEditing(true)
    setSaveMessage('')
  }

  const cancelEdit = () => {
    if (!activeId) return
    const controller = new AbortController()
    apiGet<{ok: boolean; data: {body: string}}>(`/api/v1/wiki/${encodeURIComponent(activeId)}`, controller.signal)
      .then((res) => {
        setBody(res?.data?.body || '')
        setEditing(false)
      })
      .catch(() => {})
    return () => controller.abort()
  }

  const saveEdit = async () => {
    if (!activeId) return
    setSaving(true)
    setSaveMessage('')
    try {
      const res = await apiRequest<{ok: boolean; data: Record<string, unknown>}>(
        `/api/v1/wiki/${encodeURIComponent(activeId)}`,
        'PUT',
        {body, operator: 'user'},
      )
      if (res?.ok) {
        setEditing(false)
        setSaveMessage(`已保存 · file_hash=${text(record(res.data).file_hash, '').slice(0, 12) || '—'}`)
      } else {
        setSaveMessage('保存失败，请稍后重试')
      }
    } catch (err) {
      setSaveMessage(`保存失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setSaving(false)
    }
  }

  const renderTree = (nodes: TreeNode[], depth = 0) => {
    return nodes.map((node) => (
      <div key={node.path}>
        <div
          className={`wiki-tree-folder`}
          style={{paddingLeft: 8 + depth * 14}}
        >
          <span className="wiki-tree-folder-label">
            {node.name || node.bucket} <span className="subtle">{node.files.length}</span>
          </span>
        </div>
        {node.sub_dirs.map((sub) => (
          <div key={sub.path} style={{paddingLeft: 8 + (depth + 1) * 14}}>
            {renderTree([sub], depth + 1)}
          </div>
        ))}
        {node.files.map((file) => (
          <button
            type="button"
            key={file.content_id}
            className={`wiki-tree-file${file.content_id === activeId ? ' active' : ''}`}
            onClick={() => {
              setActiveId(file.content_id)
              detailRef.current?.scrollIntoView({behavior: 'smooth', block: 'nearest'})
            }}
          >
            {file.title}
          </button>
        ))}
      </div>
    ))
  }

  return (
    <div className="module-frame demo-module-page wiki-page">
      <header className="module-top">
        <strong className="module-logo">Wiki / 母文章库</strong>
        <span className="sep" />
        <span className="module-meta">
          母文章 <b>{allEntries.length}</b> 篇
          {searchResults ? ` · 搜索结果 ${searchResults.length}` : ''}
        </span>
        <span className="module-right">
          {state === 'loading'
            ? '正在读取目录'
            : state === 'offline'
              ? 'Hub 离线'
              : state === 'empty'
                ? '母文章库尚未建立索引'
                : `${allEntries.filter((item) => item.has_image).length} 篇含图`}
        </span>
      </header>

      {state === 'offline' && (
        <div className="module-placeholder">
          <strong>Hub 暂时无法连接</strong>
          <span>{error}</span>
        </div>
      )}
      {state === 'error' && (
        <div className="module-placeholder">
          <strong>读取目录异常</strong>
          <span>{error}</span>
        </div>
      )}

      {(state === 'ready' || state === 'empty') && (
        <div className="wiki-shell">
          <aside className="wiki-rail">
            <div className="wiki-r active">
              <b>W</b>
              <span>Wiki</span>
            </div>
            <div className="wiki-r">
              <b>待</b>
              <span>待办</span>
            </div>
            <div className="wiki-r">
              <b>设</b>
              <span>设置</span>
            </div>
          </aside>

          <aside className="wiki-tree">
            <div className="wiki-tree-head">
              <h2>母文章目录</h2>
              <p>
                {tree.length} 个 bucket · <b>{allEntries.length}</b> 个文件
              </p>
            </div>
            <input
              className="wiki-search"
              placeholder="搜索全库文件…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            <div className="wiki-tree-body">
              {visibleEntries.length === 0 ? (
                <div className="wiki-tree-empty">没有匹配结果</div>
              ) : searchResults ? (
                <ul className="wiki-list">
                  {searchResults.map((entry) => (
                    <li key={entry.content_id}>
                      <button
                        type="button"
                        className={`wiki-tree-file${entry.content_id === activeId ? ' active' : ''}`}
                        onClick={() => setActiveId(entry.content_id)}
                      >
                        {entry.title}
                        <small>{entry.relative_path}</small>
                      </button>
                    </li>
                  ))}
                </ul>
              ) : (
                renderTree(tree)
              )}
            </div>
          </aside>

          <main className="wiki-main" ref={detailRef}>
            <div className="wiki-toolbar">
              <span className="wiki-path">
                {visibleEntries.find((item) => item.content_id === activeId)?.relative_path ||
                  '尚未选择母文章'}
              </span>
              <div className="wiki-actions">
                {!editing ? (
                  <button type="button" className="mini-btn" onClick={beginEdit} disabled={!activeId}>
                    编辑
                  </button>
                ) : (
                  <>
                    <button type="button" className="mini-btn" onClick={() => cancelEdit()} disabled={saving}>
                      取消
                    </button>
                    <button type="button" className="mini-btn primary" onClick={saveEdit} disabled={saving}>
                      {saving ? '保存中…' : '保存'}
                    </button>
                  </>
                )}
              </div>
            </div>

            {visibleEntries.length === 0 ? (
              <div className="module-placeholder">
                <strong>母文章库尚未建立索引</strong>
                <p>稍后会展示从 {tree.length} 个目录发现的 Markdown 文件。</p>
              </div>
            ) : editing ? (
              <textarea
                className="wiki-editor"
                value={body}
                onChange={(e) => setBody(e.target.value)}
                spellCheck={false}
              />
            ) : (
              <article className="wiki-article">
                <pre>{body || '正在读取正文…'}</pre>
              </article>
            )}

            <div className="wiki-status">
              <span>{saveMessage || (editing ? '编辑中，所有写操作走原子重命名' : '就绪 · Markdown 为唯一正文源')}</span>
              <span>
                {visibleEntries.find((item) => item.content_id === activeId)?.updated_at
                  ? `更新于 ${dateText(visibleEntries.find((item) => item.content_id === activeId)?.updated_at)}`
                  : '检索全库使用文件路径 · 标题 · 摘要'}
              </span>
            </div>
          </main>
        </div>
      )}
    </div>
  )
}
