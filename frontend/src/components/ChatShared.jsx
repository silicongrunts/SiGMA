/**
 * ChatShared - Reusable chat sub-components
 * Markdown rendering (MarkdownContent) and workflow-timeline components
 * shared by the sidebar ChatPanel, DiffViewer, LibraryBrowser, and
 * PlanApprovalDialog.
 */
import { useEffect, useState, useRef, useMemo } from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import renderMathInElement from 'katex/dist/contrib/auto-render.mjs'
import { extractMath, restoreMath, applyMathOverflow } from '../utils/mathGuard'
import { ChevronDown, Cpu, CheckCircle2, Loader2, AlertCircle, AlertTriangle, FoldVertical, Search, FileText, X } from 'lucide-react'
import TaskList from './TaskList'
import DiffView from './DiffView'

marked.setOptions({ gfm: true, breaks: true })

/**
 * Rewrite relative <img src="..."> in rendered markdown HTML to project-scoped
 * inline URLs so local images (e.g. `![](assets/fig.png)`) load from the
 * backend. Absolute URLs (http(s):, data:, /, #) are left untouched.
 *
 * Shared by MarkdownContent (chat/annotation/diff) and Preview (editor
 * preview) so the two paths render local images identically.
 */
export function rewriteProjectImageSrc(html, projectId) {
    if (!projectId || !html) return html
    const doc = new DOMParser().parseFromString(html, 'text/html')
    doc.querySelectorAll('img').forEach(img => {
        const src = img.getAttribute('src') || ''
        // Skip absolute URLs (already resolvable) AND dangerous schemes that
        // must never become a project path. javascript:/vbscript:/file: are
        // listed explicitly as defense-in-depth even though DOMPurify (run
        // after this) also strips them — this function may be called from a
        // null-projectId early-return path, so we don't rely on downstream
        // sanitization alone.
        if (!src
            || src.startsWith('http://') || src.startsWith('https://')
            || src.startsWith('data:') || src.startsWith('/')
            || src.startsWith('#')
            || src.startsWith('javascript:') || src.startsWith('vbscript:')
            || src.startsWith('file:')) {
            return
        }
        img.setAttribute('src', `/api/v1/files/${encodeURIComponent(projectId)}/inline?path=${encodeURIComponent(src)}`)
    })
    return doc.body.innerHTML
}

// A href has a URI scheme if it starts with e.g. "mailto:" or "c:". Used to
// separate "browser-handled link" from "project-relative path" below. A
// single letter before ":" also catches Windows drive paths ("C:\...").
const URI_SCHEME_RE = /^[a-z][a-z0-9+.-]*:/i

/**
 * Classify and decorate links in rendered markdown HTML:
 *
 *   - external (http(s)://, protocol-relative //) → target="_blank" +
 *     rel="noopener noreferrer", so they never navigate the SPA away
 *   - project-relative paths → data-sigma-path attribute; clicks are
 *     intercepted by MarkdownContent / Preview and opened in-app (same
 *     convention as rewriteProjectImageSrc: relative = project-root-relative)
 *   - everything else (#anchors, sigma: citations, mailto:/tel:/... schemes)
 *     → untouched
 *
 * href attributes are never rewritten: they stay honest for copy-link, and
 * no DOMPurify URI-regexp changes are needed (a rewritten sigma: href would
 * be stripped by the default regexp in callers without citation support).
 * data-sigma-path is only set when a projectId is in scope — without a
 * project the path cannot resolve to a file, so those links stay native.
 */
export function decorateMarkdownLinks(html, projectId) {
    if (!html) return html
    const doc = new DOMParser().parseFromString(html, 'text/html')
    let changed = false
    doc.querySelectorAll('a[href]').forEach(a => {
        const href = a.getAttribute('href') || ''
        if (!href || href.startsWith('#') || href.startsWith('sigma:')) return
        if (/^(?:https?:|\/\/)/i.test(href)) {
            a.setAttribute('target', '_blank')
            a.setAttribute('rel', 'noopener noreferrer')
            changed = true
            return
        }
        if (URI_SCHEME_RE.test(href)) return
        if (!projectId) return
        // marked percent-encodes hrefs; store the decoded path so the click
        // handler re-encodes it exactly once when building the open URL
        // (encode → URLSearchParams.get decode = lossless round-trip).
        let path = href
        try { path = decodeURIComponent(href) } catch { /* malformed escape — keep raw */ }
        a.setAttribute('data-sigma-path', path)
        changed = true
    })
    return changed ? doc.body.innerHTML : html
}

function isAgentToolName(tool) {
    return String(tool || '').toLowerCase() === 'agent'
}

/**
 * Extract the subagent's instruction prompt from the agent tool step params.
 * Params arrive as a JSON string that the backend may truncate mid-value, so
 * a regex fallback recovers the (possibly cut) prompt when JSON.parse fails.
 */
function agentPromptFromParams(params) {
  const s = String(params || '')
  if (!s) return ''
  try {
    const obj = JSON.parse(s)
    if (typeof obj?.prompt === 'string') return obj.prompt
  } catch { /* truncated JSON — extract the raw prompt text below */ }
  const unescape = (v) => v.replace(/\\n/g, '\n').replace(/\\t/g, '\t').replace(/\\"/g, '"')
  const closed = s.match(/"prompt"\s*:\s*"((?:[^"\\]|\\.)*)"/)
  if (closed) return unescape(closed[1])
  const tail = s.split(/"prompt"\s*:\s*"/)[1]
  return tail !== undefined ? unescape(tail) : ''
}

// Target path of a running edit/write call. Live params arrive truncated at
// 200 chars, so like agentPromptFromParams this falls back to a regex over
// the raw string when the JSON no longer parses.
function filePathFromParams(params) {
  const s = String(params || '')
  if (!s) return ''
  try {
    const obj = JSON.parse(s)
    const p = obj?.file_path ?? obj?.path
    if (typeof p === 'string') return p
  } catch { /* truncated JSON — regex below */ }
  const m = s.match(/"(?:file_path|path)"\s*:\s*"((?:[^"\\]|\\.)*)"/)
  return m ? m[1].replace(/\\n/g, '\n').replace(/\\t/g, '\t').replace(/\\"/g, '"') : ''
}

// Consecutive read-only lookups collapse into one "Exploring" group in the
// timeline — they are high-frequency, low-signal steps that would otherwise
// flood the workflow view while the model explores the project.
const READ_GROUP_TOOLS = new Set(['read', 'glob', 'grep', 'ls'])

function groupReadLookups(steps) {
  const items = []
  let i = 0
  while (i < steps.length) {
    const isLookup = steps[i].type === 'tool' && READ_GROUP_TOOLS.has(String(steps[i].tool).toLowerCase())
    if (!isLookup) { items.push(steps[i]); i++; continue }
    const group = []
    while (i < steps.length
      && steps[i].type === 'tool'
      && READ_GROUP_TOOLS.has(String(steps[i].tool).toLowerCase())) {
      group.push(steps[i])
      i++
    }
    if (group.length >= 2) items.push({ group })
    else items.push(...group)
  }
  return items
}

// Internal context params the backend runner injects into tool_args before
// calling a tool (see llm_loop_runner.py). They are required server-side but
// should never be shown to the user in the workflow timeline.
const INTERNAL_PARAM_KEYS = ['project_id', 'session_id', 'model_role']

// Pre-compiled patterns used to scrub the params string when JSON.parse fails
// (the backend truncates at 200 chars, so the blob is often unparseable).
// Order matters: closed-value first, then numeric, then truncated-value, then
// the rare case where the truncation lands inside the key name itself.
const INTERNAL_PARAM_PATTERNS = INTERNAL_PARAM_KEYS.flatMap(k => [
    new RegExp(`,?\\s*"${k}"\\s*:\\s*"(?:[^"\\\\]|\\\\.)*"`, 'g'), // closed string
    new RegExp(`,?\\s*"${k}"\\s*:\\s*[0-9]+`, 'g'),                // numeric
    new RegExp(`,?\\s*"${k}"\\s*:\\s*"[^"]*$`, 'g'),               // truncated value
])
// Tail fallback: an internal key whose name itself was cut by the 200-char
// truncation, e.g. `"project_i`, `"session_`, `"model_r`. We match the three
// known key names reduced to any of their own prefixes via a character-class
// trick: build the prefix set once, match the longest one present at EOL.
const INTERNAL_KEY_PREFIXES = [
    'project_id', 'session_id', 'model_role',
].flatMap(full => Array.from({ length: full.length }, (_, i) => full.slice(0, i + 1)))
const INTERNAL_PARAM_TAIL = new RegExp(
    `,?\\s*"(?:${[...new Set(INTERNAL_KEY_PREFIXES)].sort((a, b) => b.length - a.length).join('|')})[^"]*$`
)

/**
 * Strip internal params from the tool_start `params` string before display.
 * The backend truncates the JSON at 200 chars, so a regex fallback covers the
 * case where the truncation lands mid-value (or mid-key) and JSON.parse fails.
 * Returns '' when nothing meaningful remains (so the surrounding parentheses
 * can be hidden entirely).
 */
function sanitizeToolParams(paramsStr) {
    if (!paramsStr) return ''
    let s = paramsStr
    try {
        const obj = JSON.parse(paramsStr)
        if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
            for (const k of INTERNAL_PARAM_KEYS) delete obj[k]
            s = JSON.stringify(obj)
        }
    } catch {
        // truncated / not a flat object dict — scrub the raw string below
        for (const re of INTERNAL_PARAM_PATTERNS) {
            s = s.replace(re, '')
        }
        s = s.replace(INTERNAL_PARAM_TAIL, '')
    }
    // tidy dangling separators left by a removed leading/only/trailing key
    s = s.replace(/\{\s*,/g, '{').replace(/,\s*,/g, ',').replace(/,\s*\}/g, '}').replace(/,\s*$/g, '')
    // restore a brace dropped by truncation, so the chip doesn't read as broken
    if (s.startsWith('{') && !s.endsWith('}')) s += '}'
    const out = s.trim()
    return out === '{}' ? '' : out
}

// Citation links use the sigma:// scheme. DOMPurify's default URI regex strips
// unknown schemes, so we extend it (only when citations are active) to preserve
// sigma: hrefs on anchors. Scoped to MarkdownContent's sanitize call; other
// callers of DOMPurify keep the stricter default.
const SIGMA_CITATION_SCHEME = 'sigma'
const citationUriRegexp = /^(?:sigma:|(?:(?:f|ht)tps?|mailto|tel|callto|sms|cid|xmpp|matrix):|[^a-z]|[a-z+.\-]+(?:[^a-z+.\-:]|$))/i

export const MarkdownContent = ({ content, projectId = null, onCitation = null, compact = false }) => {
    const containerRef = useRef(null)

    // Memoize parsing+sanitizing: in streaming chat the parent re-renders on
    // every token delta, and without this every prior message re-parses its
    // full markdown each frame.
    const html = useMemo(() => {
        // Protect math from marked (breaks:true <br> insertion, emphasis eating,
        // entity escaping) by lifting it out before parse and stitching it back
        // after. See utils/mathGuard.js for the full rationale.
        const { text, map } = extractMath(content || '')
        const parsed = marked.parse(text)
        const restored = restoreMath(parsed, map)
        const rewritten = rewriteProjectImageSrc(restored, projectId)
        // Decorate AFTER sanitize: attributes added past this point are never
        // filtered, so no DOMPurify config changes are needed for them.
        const sanitized = onCitation
            ? DOMPurify.sanitize(rewritten, { ALLOWED_URI_REGEXP: citationUriRegexp })
            : DOMPurify.sanitize(rewritten)
        return decorateMarkdownLinks(sanitized, projectId)
    }, [content, projectId, onCitation])

    // Render math ($...$ inline, $$...$$ display) after React commits the HTML.
    // Same delimiter set as Preview.jsx so chat and preview render math alike.
    // Note: this runs on every `html` change, so during streaming it re-walks
    // the whole subtree and re-typesets every formula each token (O(message
    // length) per token). Acceptable for now; if long math-heavy messages lag,
    // gate this on an isStreaming flag and run once on completion.
    useEffect(() => {
        if (containerRef.current) {
            renderMathInElement(containerRef.current, {
                delimiters: [
                    { left: '$$', right: '$$', display: true },
                    { left: '$', right: '$', display: false },
                ],
                throwOnError: false,
            })
            // After typesetting, flag inline formulas that truly overflow the
            // bubble so each gets its own scrollbar. See utils/mathGuard.js.
            applyMathOverflow(containerRef.current)
        }
    }, [html])

    // Links that are never real navigation are intercepted via a single
    // delegated handler instead of per-anchor binding: sigma: citations, and
    // project-relative markdown links (marked by decorateMarkdownLinks with
    // data-sigma-path). The latter dispatch through the citation channel as a
    // synthesized sigma://synthesis URL, so validation, tab switching,
    // autosave-before-switch, and error toasts all live in one place
    // (EditorView's handleCitation / openProjectPath). Only active when
    // onCitation is set, so other MarkdownContent callers are unaffected.
    const handleClick = useMemo(() => {
        if (!onCitation) return undefined
        return (e) => {
            const anchor = e.target.closest?.('a')
            if (!anchor) return
            const sigmaPath = anchor.getAttribute('data-sigma-path')
            if (sigmaPath) {
                e.preventDefault()
                onCitation(`sigma://synthesis/file?path=${encodeURIComponent(sigmaPath)}`)
                return
            }
            const href = anchor.getAttribute('href') || ''
            if (!href.startsWith(SIGMA_CITATION_SCHEME + ':')) return
            e.preventDefault()
            onCitation(href)
        }
    }, [onCitation])

    return (
        <div
            ref={containerRef}
            className={`${compact ? 'sigma-content sigma-content-compact' : 'sigma-content'} text-sm leading-relaxed break-words overflow-hidden`}
            dangerouslySetInnerHTML={{ __html: html }}
            onClick={handleClick}
        />
    )
}

/**
 * FileEditStep — timeline card for a finished edit/write tool call: file
 * path with +adds/-dels badges (line count for writes). Clicking opens a
 * modal with the char-level diff (edit) or the written content (write).
 */
function FileEditStep({ step }) {
    const { t } = useTranslation()
    const fe = step.fileEdit
    const [open, setOpen] = useState(false)
    const isEdit = fe.kind === 'edit'
    return <div className="py-0.5">
        <button
            onClick={() => setOpen(true)}
            className="flex items-center gap-1.5 w-full text-left rounded transition-colors py-0.5 hover:bg-gray-50/50 dark:hover:bg-gray-800"
        >
            <FileText className="w-3 h-3 flex-shrink-0 text-emerald-500 dark:text-emerald-400" />
            <span className="flex-1 min-w-0 truncate text-[10px] font-mono text-gray-600 dark:text-gray-300" title={fe.path}>{fe.path}</span>
            <span className="flex-shrink-0 flex items-baseline gap-1.5 font-mono text-[9px]">
                {isEdit ? (<>
                    {fe.dels > 0 && <span className="text-rose-500 dark:text-rose-400">-{fe.dels}</span>}
                    {fe.adds > 0 && <span className="text-emerald-600 dark:text-emerald-400">+{fe.adds}</span>}
                </>) : (
                    <span className="text-gray-400 dark:text-gray-500">{t('chat.linesWritten', { count: fe.adds })}</span>
                )}
            </span>
        </button>
        {open && <FileEditModal fe={fe} onClose={() => setOpen(false)} />}
    </div>
}

/**
 * FileEditModal — read-only preview of one finished edit/write call:
 * side-by-side char-level diff (edit, the same DiffView the permission
 * dialog renders) or the written content (write). ESC and backdrop close.
 */
function FileEditModal({ fe, onClose }) {
    const { t } = useTranslation()
    useEffect(() => {
        const handleEsc = (e) => { if (e.key === 'Escape') onClose() }
        window.addEventListener('keydown', handleEsc)
        return () => window.removeEventListener('keydown', handleEsc)
    }, [onClose])
    const isEdit = fe.kind === 'edit'
    const truncated = isEdit ? fe.diff_truncated : fe.content_truncated
    // Portalled to <body>: the chat timeline sits inside animated, clipped
    // containers (overflow-hidden panels, fade-in wrapper) whose stacking
    // contexts can trap or clip a fixed-position child depending on the
    // browser's compositing. The body root is the one place a fixed overlay
    // is guaranteed to paint above the app.
    return createPortal(
        <div className="fixed inset-0 z-[5000] flex items-center justify-center p-4">
            <div className="absolute inset-0 bg-gray-900/40 backdrop-blur-sm animate-in fade-in duration-300" onClick={onClose} />
            <div className="bg-white dark:bg-gray-900 rounded-3xl w-full max-w-4xl relative z-[5001] shadow-2xl border border-gray-100 dark:border-gray-800 overflow-hidden flex flex-col max-h-[85vh] animate-in zoom-in duration-300">
                <div className="px-5 py-3.5 border-b border-gray-100 dark:border-gray-800 flex items-center gap-2.5">
                    <FileText className="w-4 h-4 flex-shrink-0 text-emerald-500 dark:text-emerald-400" />
                    <span className="flex-1 min-w-0 truncate text-sm font-mono font-semibold text-gray-800 dark:text-gray-200" title={fe.path}>{fe.path}</span>
                    <button onClick={onClose} className="flex-shrink-0 p-1 rounded-lg text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors">
                        <X className="w-4 h-4" />
                    </button>
                </div>
                <div className="flex-1 overflow-y-auto px-5 py-4">
                    {isEdit ? (
                        <div className="border border-gray-200 dark:border-gray-700 rounded-xl overflow-hidden">
                            <DiffView lines={fe.diff_lines || []} maxH="max-h-[60vh]" />
                        </div>
                    ) : (
                        <pre className="bg-gray-900 text-gray-100 rounded-xl px-4 py-3 text-xs font-mono leading-relaxed whitespace-pre-wrap break-all max-h-[60vh] overflow-y-auto">{fe.content}</pre>
                    )}
                    {truncated && (
                        <div className="mt-1.5 text-[11px] text-amber-600 dark:text-amber-400 flex items-center gap-1">
                            <AlertTriangle className="w-3 h-3 flex-shrink-0" />
                            <span>{t(isEdit ? 'permission.diffTruncated' : 'chat.fileContentTruncated')}</span>
                        </div>
                    )}
                </div>
            </div>
        </div>,
        document.body
    )
}

/**
 * AgentToolStep — renders a nested timeline for an agent tool call.
 * Extracted from ThinkingStep to keep useState at the component top level
 * (React hooks must not be called conditionally).
 */
function AgentToolStep({ step }) {
  const isRunning = step.status === 'running'
  const [agentOpen, setAgentOpen] = useState(isRunning)
  const agentLabel = step.agentType || 'agent'
  const prompt = agentPromptFromParams(step.params)
  return <div className="flex flex-col gap-0.5 py-0.5">
      <button
          onClick={() => setAgentOpen(!agentOpen)}
          className="flex items-start gap-1.5 w-full text-left hover:bg-gray-50/50 dark:hover:bg-gray-800 rounded transition-colors"
      >
          <Cpu className={`w-3 h-3 mt-0.5 flex-shrink-0 ${isRunning ? 'text-purple-400 animate-pulse' : 'text-purple-500'}`} />
          <div className="flex-1 min-w-0">
              <span className="text-[10px] font-mono text-purple-600">Agent({agentLabel})</span>
              {step.result && (
                  <div className="mt-0.5 text-[9px] text-gray-400 dark:text-gray-500 bg-gray-50/50 dark:bg-gray-900 rounded px-1.5 py-0.5 max-h-16 overflow-y-auto whitespace-pre-wrap break-all border border-gray-100 dark:border-gray-800">
                      {step.result}
                  </div>
              )}
          </div>
          <ChevronDown className={`w-2.5 h-2.5 mt-0.5 text-gray-300 dark:text-gray-600 transition-transform ${agentOpen ? 'rotate-180' : ''}`} />
      </button>
      {agentOpen && (
          <div className="ml-2 pl-2 border-l-2 border-dashed border-purple-200">
              {prompt && (
                  <div className="mb-1 text-[9px] text-gray-400 dark:text-gray-500 whitespace-pre-wrap break-words max-h-24 overflow-y-auto bg-gray-50/50 dark:bg-gray-900 rounded px-1.5 py-1 border border-gray-100 dark:border-gray-800">
                      {prompt}
                  </div>
              )}
              {step.subSteps && step.subSteps.map((s, i) => <ThinkingStep key={i} step={s} />)}
          </div>
      )}
  </div>
}

/**
 * CompactSummaryNote — collapsed affordance marking where a session was
 * compacted. Expands to the summary the next LLM call received (the LLM
 * instruction preface is stripped server-side). Used both as the standalone
 * card between chat bubbles and as a step inside the workflow timeline.
 */
export const CompactSummaryNote = ({ summary }) => {
    const { t } = useTranslation()
    const [open, setOpen] = useState(false)
    return (
        <div className="w-full">
            <button
                onClick={() => setOpen(!open)}
                className="flex items-center gap-1.5 py-0.5 text-left group"
                title={t('chat.compactBoundary')}
            >
                <FoldVertical className="w-3 h-3 text-amber-500 dark:text-amber-400 flex-shrink-0" />
                <span className="text-[10px] font-medium text-gray-400 dark:text-gray-500 group-hover:text-gray-600 dark:group-hover:text-gray-300 transition-colors">
                    {t('chat.compactBoundary')}
                </span>
                <ChevronDown className={`w-2.5 h-2.5 text-gray-300 dark:text-gray-600 transition-transform ${open ? 'rotate-180' : ''}`} />
            </button>
            {open && (
                <div className="mt-1 ml-0.5 pl-2 border-l-2 border-dashed border-amber-200 dark:border-amber-900/50">
                    <div className="text-[10px] text-gray-500 dark:text-gray-400 leading-relaxed whitespace-pre-wrap max-h-64 overflow-y-auto bg-gray-50/50 dark:bg-gray-900 rounded px-2 py-1.5 border border-gray-100 dark:border-gray-800">
                        {summary}
                    </div>
                </div>
            )}
        </div>
    )
}

export const ThinkingStep = ({ step }) => {
    const { t } = useTranslation()
    // ── hint / streaming text (processing status, intermediate thoughts) ──
    if (step.type === 'hint' || step.type === 'streaming_text') {
        // Model-authored intermediate text renders as markdown; system status
        // hints (transient markers, retry notices, plain flags) stay plain.
        if (step.type === 'hint' && !step.transient && !step.plain) {
            return <div className="py-0.5 max-h-64 overflow-y-auto">
                <MarkdownContent content={step.content} compact />
            </div>
        }
        // LLM stream retry — amber notice with the underlying error as a
        // subtle second line so the user sees why output paused.
        if (step.retry) {
            return <div className="flex items-start gap-2 py-0.5">
                <AlertCircle className="w-3 h-3 mt-0.5 flex-shrink-0 text-amber-500 dark:text-amber-400" />
                <div className="min-w-0">
                    <div className="text-[10px] font-medium text-amber-600 dark:text-amber-400">{step.content}</div>
                    {step.error && <div className="text-[9px] text-gray-400 dark:text-gray-500 truncate">{step.error}</div>}
                </div>
            </div>
        }
        const isLive = step.type === 'streaming_text'
        return <div className="flex items-center gap-2 py-0.5">
            <div className={`w-1 h-1 rounded-full flex-shrink-0 ${isLive ? 'bg-blue-400 animate-pulse' : 'bg-gray-300 dark:bg-gray-600'}`} />
            <div className={`text-[10px] leading-tight ${isLive ? 'text-gray-500 dark:text-gray-400' : 'text-gray-400 dark:text-gray-500 italic'}`}>{step.content}</div>
        </div>
    }

    // ── tool call ──
    if (step.type === 'tool') {
        // agent tool with nested subagent steps
        if (isAgentToolName(step.tool) && step.subSteps && step.subSteps.length > 0) {
            return <AgentToolStep step={step} />
        }
        // finished edit/write — diff card from backend file_edit metadata
        if (step.fileEdit) {
            return <FileEditStep step={step} />
        }

        const Icon = step.status === 'running' ? Loader2 : CheckCircle2
        const iconCls = step.status === 'running' ? 'text-blue-400 animate-spin' : 'text-green-500'
        // A running edit/write shows its target path instead of raw truncated
        // JSON params; once done, the FileEditStep card or the error result
        // in the generic row below takes over.
        const pendingPath = step.status === 'running' && (step.tool === 'edit' || step.tool === 'write')
            ? filePathFromParams(step.params) : ''
        const cleanParams = pendingPath ? '' : sanitizeToolParams(step.params)
        return <div className="flex items-start gap-1.5 py-0.5">
            <Icon className={`w-3 h-3 mt-0.5 flex-shrink-0 ${iconCls}`} />
            <div className="flex-1 min-w-0">
                <span className="text-[10px] font-mono text-gray-500 dark:text-gray-400">{step.tool}</span>
                {pendingPath && <span className="text-[9px] font-mono text-gray-400 dark:text-gray-500 ml-1 truncate">{pendingPath}</span>}
                {cleanParams && <span className="text-[9px] text-gray-400 dark:text-gray-500 ml-1 break-all">({cleanParams})</span>}
                {step.result && (
                    <div className="mt-0.5 text-[9px] text-gray-400 dark:text-gray-500 bg-gray-50/50 dark:bg-gray-900 rounded px-1.5 py-0.5 max-h-16 overflow-y-auto whitespace-pre-wrap break-all border border-gray-100 dark:border-gray-800">
                        {step.result}
                    </div>
                )}
            </div>
        </div>
    }

    // ── compaction boundary (session was compacted here) ──
    if (step.type === 'compact') {
        return <div className="py-0.5"><CompactSummaryNote summary={step.content} /></div>
    }

    // ── awaiting user input ──
    if (step.type === 'awaiting_input') {
        return <div className="flex items-center gap-2 py-0.5">
            <AlertCircle className="w-3 h-3 text-amber-400 animate-pulse flex-shrink-0" />
            <div className="text-[10px] text-amber-600 font-medium">{t('chat.waitingInput')}</div>
        </div>
    }

    // ── fallback ──
    return null
}

/** Collapsible group for a run of consecutive read-only lookups. */
function ReadToolGroup({ steps, isStreaming }) {
    const { t } = useTranslation()
    const [open, setOpen] = useState(false)
    const running = isStreaming && steps.some(s => s.status === 'running')
    return <div className="flex flex-col py-0.5">
        <button
            onClick={() => setOpen(!open)}
            className="flex items-center gap-1.5 w-full text-left hover:bg-gray-50/50 dark:hover:bg-gray-800 rounded transition-colors py-0.5"
        >
            {running
                ? <Loader2 className="w-3 h-3 flex-shrink-0 text-blue-400 animate-spin" />
                : <Search className="w-3 h-3 flex-shrink-0 text-gray-400 dark:text-gray-500" />}
            <span className="flex items-baseline gap-1 min-w-0">
                <span className={`text-[10px] font-medium tracking-wide ${running ? 'shimmer-text' : 'text-gray-400 dark:text-gray-500'}`}>
                    {running ? t('chat.exploring') : t('chat.explored')}
                </span>
                <span className="text-[9px] text-gray-400 dark:text-gray-500">· {t('chat.lookupCount', { count: steps.length })}</span>
            </span>
            <ChevronDown className={`w-2.5 h-2.5 flex-shrink-0 text-gray-300 dark:text-gray-600 transition-transform ${open ? 'rotate-180' : ''}`} />
        </button>
        {open && (
            <div className="ml-2 pl-2 border-l-2 border-dashed border-gray-200 dark:border-gray-700">
                {steps.map((s, i) => <ThinkingStep key={i} step={s} />)}
            </div>
        )}
    </div>
}

export const ThinkingProcess = ({ steps, isStreaming }) => {
    const { t } = useTranslation()
    const [isOpen, setIsOpen] = useState(false)
    const prevStreamingRef = useRef(isStreaming)

    useEffect(() => {
        if (isStreaming && !isOpen && (steps?.length > 0)) {
            setIsOpen(true)
        }
    }, [isStreaming, steps?.length])

    // Auto-collapse when streaming finishes
    useEffect(() => {
        if (prevStreamingRef.current && !isStreaming) {
            setIsOpen(false)
        }
        prevStreamingRef.current = isStreaming
    }, [isStreaming])

    const visibleSteps = steps || []
    if (visibleSteps.length === 0) return null

    const toolSteps = visibleSteps.filter(s => s.type === 'tool')
    const runningTools = toolSteps.filter(s => s.status === 'running')

    return (
        <div className="w-full mb-2">
            <button
                onClick={() => setIsOpen(!isOpen)}
                className="flex items-center gap-1.5 text-[10px] text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-400 transition-colors group py-0.5"
            >
                {isStreaming && runningTools.length > 0 ? (
                    <Loader2 className="w-2.5 h-2.5 text-blue-400 animate-spin" />
                ) : (
                    <div className={`w-1.5 h-1.5 rounded-full ${toolSteps.length > 0 ? 'bg-green-400' : 'bg-gray-300 dark:bg-gray-600'}`} />
                )}
                <span className="font-medium tracking-wide">
                    {t(isOpen ? 'chat.hideWork' : 'chat.showWork')}
                    {toolSteps.length > 0 && ` · ${t('chat.workSteps', { count: toolSteps.length })}`}
                </span>
                <ChevronDown className={`w-2.5 h-2.5 transition-transform duration-200 ${isOpen ? 'rotate-180' : ''}`} />
            </button>
            {isOpen && (
                <div className="mt-1 ml-0.5 pl-2 border-l-2 border-dashed border-gray-200 dark:border-gray-700 text-[10px] animate-in fade-in duration-150">
                    {groupReadLookups(visibleSteps).map((item, i) => (
                        item.group
                            ? <ReadToolGroup key={i} steps={item.group} isStreaming={isStreaming} />
                            : <ThinkingStep key={i} step={item} />
                    ))}
                    <TaskList expanded={isStreaming} />
                </div>
            )}
        </div>
    )
}
