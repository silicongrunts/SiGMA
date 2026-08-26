/**
 * ChatSearchModal — search session titles and user-visible message text.
 *
 * Results are grouped by session. Clicking a match (or a session header)
 * asks the parent to navigate: active sessions jump inside the chat panel,
 * archived sessions open the archive modal. Matching happens server-side
 * over UI-shaped content, so intermediate process text never matches.
 */
import { useState, useEffect, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { Search, X, Loader2, Archive, User, Bot } from 'lucide-react'
import { chatAPI } from '../api'
import { ModalOverlay } from './Modal'
import { HighlightText } from './Highlight'
import { formatTimestamp } from '../utils/formatTimestamp'

const SEARCH_DEBOUNCE_MS = 300

export default function ChatSearchModal({ isOpen, onClose, projectId, currentSessionId, onJump }) {
  const { t } = useTranslation()
  const [query, setQuery] = useState('')
  const [results, setResults] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [activeIndex, setActiveIndex] = useState(0)
  // Client-side scope filter over the returned groups.
  const [scope, setScope] = useState('all')
  const inputRef = useRef(null)
  const abortRef = useRef(null)
  const matchRefs = useRef([])

  // Results belong to one project DB; a project switch invalidates them.
  useEffect(() => {
    setQuery('')
    setResults(null)
    setError(null)
    setActiveIndex(0)
  }, [projectId])

  // Debounced search; a newer keystroke cancels the in-flight request.
  useEffect(() => {
    const trimmed = query.trim()
    if (!isOpen || !trimmed) {
      abortRef.current?.abort()
      setLoading(false)
      setResults(null)
      setError(null)
      return
    }
    // Pending state starts with the debounce, not the fetch: reopening with
    // a kept query must show "searching", not a flash of "no results".
    setLoading(true)
    const timer = setTimeout(async () => {
      abortRef.current?.abort()
      const controller = new AbortController()
      abortRef.current = controller
      setError(null)
      try {
        const data = await chatAPI.search(projectId, trimmed, controller.signal)
        setResults(data)
        setActiveIndex(0)
      } catch (e) {
        if (e?.name !== 'AbortError') setError(e)
      } finally {
        if (abortRef.current === controller) setLoading(false)
      }
    }, SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [query, projectId, isOpen])

  useEffect(() => {
    if (isOpen) inputRef.current?.focus()
  }, [isOpen])

  const allGroups = results?.groups || []
  const trimmedQuery = query.trim()
  const groups = allGroups.filter(g =>
    scope === 'all' || (scope === 'archived') === Boolean(g.session.is_archived),
  )
  // Summary describes the visible window: per-session match_count sums,
  // not just the capped shown matches.
  const visibleMatches = groups.reduce((sum, g) => sum + (g.match_count || 0), 0)
  // Flat list for keyboard navigation; title-only groups jump session-level.
  const jumpTargets = groups.flatMap(g =>
    g.matches.map(m => ({ session: g.session, match: m })),
  )
  if (jumpTargets.length === 0 && groups.length > 0 && groups[0].title_match) {
    jumpTargets.push({ session: groups[0].session, match: null })
  }
  const flatIndexByMatch = new Map()
  let counter = 0
  for (const g of groups) {
    for (const m of g.matches) flatIndexByMatch.set(`${g.session.id}:${m.id}`, counter++)
  }

  useEffect(() => {
    matchRefs.current[activeIndex]?.scrollIntoView({ block: 'nearest' })
  }, [activeIndex])

  const handleInputKeyDown = (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (jumpTargets.length === 0) return
      e.preventDefault()
      const delta = e.key === 'ArrowDown' ? 1 : -1
      setActiveIndex(idx => (idx + delta + jumpTargets.length) % jumpTargets.length)
    } else if (e.key === 'Enter') {
      const target = jumpTargets[activeIndex]
      if (target) {
        e.preventDefault()
        onJump(target.session, target.match)
      }
    }
  }

  const showBody = () => {
    if (!trimmedQuery) return 'hint'
    if (error) return 'error'
    if (loading && !results) return 'loading'
    if (groups.length === 0) return 'empty'
    return 'results'
  }

  return (
    <ModalOverlay isOpen={isOpen} onClose={onClose}>
      <div className="flex flex-col max-h-[70vh]">
        {/* Search input */}
        <div className="flex items-center gap-2.5 px-4 py-3 border-b border-gray-100 dark:border-gray-800 flex-shrink-0">
          <Search className="w-4 h-4 text-gray-400 flex-shrink-0" />
          <input
            ref={inputRef}
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={handleInputKeyDown}
            placeholder={t('chat.searchPlaceholder')}
            className="flex-1 min-w-0 bg-transparent text-sm text-gray-800 dark:text-gray-200 outline-none placeholder:text-gray-400"
            aria-label={t('chat.search')}
          />
          {loading && <Loader2 className="w-3.5 h-3.5 animate-spin text-gray-400 flex-shrink-0" />}
          {query && !loading && (
            <button onClick={() => setQuery('')} className="p-0.5 text-gray-300 hover:text-gray-500 dark:hover:text-gray-400 rounded" title={t('common.cancel')}>
              <X className="w-3.5 h-3.5" />
            </button>
          )}
        </div>

        {/* Results */}
        <div className="flex-1 overflow-y-auto p-2">
          {/* Anchored here, above every body state: a scope filter that
              empties the view must not hide its own way back. */}
          {results && !error && (
            <div className="flex items-center justify-between gap-2 px-2.5 py-1.5">
              <span className="text-[10px] text-gray-400 dark:text-gray-500">
                {t('chat.searchSummary', { matches: visibleMatches, sessions: groups.length })}
              </span>
              <span className="flex items-center gap-0.5">
                {['all', 'active', 'archived'].map(s => (
                  <button
                    key={s}
                    onClick={() => { setScope(s); setActiveIndex(0) }}
                    className={`px-1.5 py-0.5 rounded text-[9px] font-medium transition-colors ${
                      scope === s
                        ? 'bg-sigma-100 dark:bg-sigma-600/20 text-sigma-700 dark:text-sigma-200'
                        : 'text-gray-400 dark:text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800'
                    }`}
                  >
                    {t(`chat.searchScope.${s}`)}
                  </button>
                ))}
              </span>
            </div>
          )}
          {showBody() === 'hint' && (
            <div className="px-3 py-10 text-center text-xs text-gray-400 dark:text-gray-500">{t('chat.searchHint')}</div>
          )}
          {showBody() === 'error' && (
            <div className="px-3 py-10 text-center text-xs text-red-500">{t('chat.searchFailed')}</div>
          )}
          {showBody() === 'loading' && (
            <div className="flex items-center justify-center gap-2 px-3 py-10 text-xs text-gray-400 dark:text-gray-500">
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
              {t('chat.searchSearching')}
            </div>
          )}
          {showBody() === 'empty' && (
            <div className="px-3 py-10 text-center text-xs text-gray-400 dark:text-gray-500">{t('chat.searchNoResults')}</div>
          )}
          {showBody() === 'results' && (
            <>
              {groups.map(group => {
                const isCurrent = group.session.id === currentSessionId
                return (
                  <div key={group.session.id} className="mb-1">
                    <button
                      onClick={() => onJump(group.session, null)}
                      className="w-full flex items-center gap-2 px-2.5 py-2 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 text-left transition-colors"
                    >
                      <span className="flex-1 min-w-0 text-sm font-medium text-gray-700 dark:text-gray-300 truncate">
                        <HighlightText text={group.session.title || t('chat.untitled')} query={trimmedQuery} />
                      </span>
                      {isCurrent && (
                        <span className="text-[9px] font-bold text-sigma-600 dark:text-sigma-300 bg-sigma-50 dark:bg-sigma-600/20 px-1.5 py-0.5 rounded flex-shrink-0">
                          {t('chat.searchCurrent')}
                        </span>
                      )}
                      {group.session.is_archived && (
                        <span className="flex items-center gap-0.5 text-[9px] font-medium text-amber-600 dark:text-amber-400 bg-amber-50 dark:bg-amber-900/30 px-1.5 py-0.5 rounded flex-shrink-0">
                          <Archive className="w-2.5 h-2.5" />
                          {t('chat.searchArchivedBadge')}
                        </span>
                      )}
                      <span className="text-[9px] text-gray-400 dark:text-gray-500 flex-shrink-0">
                        {formatTimestamp(group.session.updated_at)}
                      </span>
                    </button>

                    {group.matches.map(m => {
                      const flatIndex = flatIndexByMatch.get(`${group.session.id}:${m.id}`)
                      const active = flatIndex === activeIndex
                      return (
                        <button
                          key={m.id}
                          ref={el => { matchRefs.current[flatIndex] = el }}
                          onClick={() => onJump(group.session, m)}
                          className={`w-full flex items-start gap-2 ml-3 px-2.5 py-1.5 rounded-lg text-left transition-colors ${
                            active
                              ? 'bg-sigma-50 dark:bg-sigma-600/20'
                              : 'hover:bg-gray-50 dark:hover:bg-gray-800'
                          }`}
                        >
                          {m.role === 'user'
                            ? <User className="w-3 h-3 mt-0.5 text-gray-400 flex-shrink-0" />
                            : <Bot className="w-3 h-3 mt-0.5 text-sigma-500 flex-shrink-0" />}
                          <span className="flex-1 min-w-0">
                            <span className="block text-xs text-gray-600 dark:text-gray-300 leading-relaxed break-words">
                              <HighlightText text={m.snippet} query={trimmedQuery} />
                            </span>
                            {m.created_at && (
                              <span className="block text-[9px] text-gray-400 dark:text-gray-500 mt-0.5">
                                {formatTimestamp(m.created_at)}
                              </span>
                            )}
                          </span>
                        </button>
                      )
                    })}
                    {group.match_count > group.matches.length && (
                      <div className="ml-3 px-2.5 py-1 text-[10px] text-gray-400 dark:text-gray-500">
                        {t('chat.searchMoreMatches', { count: group.match_count - group.matches.length })}
                      </div>
                    )}
                  </div>
                )
              })}
              {scope === 'all' && groups.length < results.total_sessions && (
                <div className="px-2.5 py-2 text-[10px] text-gray-400 dark:text-gray-500 text-center">
                  {t('chat.searchMoreSessions', { shown: groups.length, total: results.total_sessions })}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </ModalOverlay>
  )
}
