/**
 * ChatShared - Reusable chat sub-components
 * Extracted from App.jsx for use in sidebar chat, ExploreTab, and LibraryTab.
 */
import { useEffect, useState, useRef, useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { ChevronDown, MessageSquare, Cpu, CheckCircle2, Loader2, AlertCircle } from 'lucide-react'
import TaskList from './TaskList'

marked.setOptions({ gfm: true, breaks: true })

function rewriteProjectImageSrc(html, projectId) {
    if (!projectId || !html) return html
    const doc = new DOMParser().parseFromString(html, 'text/html')
    doc.querySelectorAll('img').forEach(img => {
        const src = img.getAttribute('src') || ''
        if (!src || src.startsWith('http://') || src.startsWith('https://') || src.startsWith('data:') || src.startsWith('/') || src.startsWith('#')) {
            return
        }
        img.setAttribute('src', `/api/v1/files/${encodeURIComponent(projectId)}/inline?path=${encodeURIComponent(src)}`)
    })
    return doc.body.innerHTML
}

function isAgentToolName(tool) {
    return String(tool || '').toLowerCase() === 'agent'
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

export const MarkdownContent = ({ content, projectId = null }) => {
    // Memoize parsing+sanitizing: in streaming chat the parent re-renders on
    // every token delta, and without this every prior message re-parses its
    // full markdown each frame.
    const html = useMemo(() => {
        const parsed = marked.parse(content || '')
        return DOMPurify.sanitize(rewriteProjectImageSrc(parsed, projectId))
    }, [content, projectId])
    return (
        <div
            className="sigma-content text-sm leading-relaxed break-words overflow-hidden"
            dangerouslySetInnerHTML={{ __html: html }}
        />
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
              {step.subSteps && step.subSteps.map((s, i) => <ThinkingStep key={i} step={s} />)}
          </div>
      )}
  </div>
}

export const ThinkingStep = ({ step }) => {
    const { t } = useTranslation()
    // ── hint / streaming text (processing status, intermediate thoughts) ──
    if (step.type === 'hint' || step.type === 'streaming_text') {
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

        const Icon = step.status === 'running' ? Loader2 : CheckCircle2
        const iconCls = step.status === 'running' ? 'text-blue-400 animate-spin' : 'text-green-500'
        const cleanParams = sanitizeToolParams(step.params)
        return <div className="flex items-start gap-1.5 py-0.5">
            <Icon className={`w-3 h-3 mt-0.5 flex-shrink-0 ${iconCls}`} />
            <div className="flex-1 min-w-0">
                <span className="text-[10px] font-mono text-gray-500 dark:text-gray-400">{step.tool}</span>
                {cleanParams && <span className="text-[9px] text-gray-400 dark:text-gray-500 ml-1 break-all">({cleanParams})</span>}
                {step.result && (
                    <div className="mt-0.5 text-[9px] text-gray-400 dark:text-gray-500 bg-gray-50/50 dark:bg-gray-900 rounded px-1.5 py-0.5 max-h-16 overflow-y-auto whitespace-pre-wrap break-all border border-gray-100 dark:border-gray-800">
                        {step.result}
                    </div>
                )}
            </div>
        </div>
    }

    // ── agent call ──
    if (step.type === 'agent_call') {
        return <div className="flex items-start gap-1.5 py-0.5">
            <Cpu className="w-3 h-3 mt-0.5 text-purple-400 flex-shrink-0" />
            <div className="flex-1 min-w-0">
                <span className="text-[10px] font-mono text-purple-600">{step.agent}</span>
                {step.description && <span className="text-[9px] text-gray-400 dark:text-gray-500 ml-1">{step.description}</span>}
            </div>
        </div>
    }

    // ── agent progress (step_delta) ──
    if (step.type === 'agent') {
        return <div className="flex items-start gap-1.5 py-0.5">
            <MessageSquare className="w-3 h-3 mt-0.5 text-sigma-400 flex-shrink-0" />
            <div className="flex-1 min-w-0">
                <span className="text-[9px] font-semibold text-sigma-500">{step.agent}</span>
                <div className="text-[10px] text-gray-500 dark:text-gray-400 leading-relaxed whitespace-pre-wrap mt-0.5">{step.content}</div>
            </div>
        </div>
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

export const ThinkingProcess = ({ steps, isStreaming }) => {
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

    const visibleSteps = (steps || []).filter(s => s.type !== 'reasoning')
    if (visibleSteps.length === 0) return null

    const toolSteps = visibleSteps.filter(s => s.type === 'tool' || s.type === 'agent_call')
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
                    {isOpen ? 'Hide' : 'Show'} work
                    {toolSteps.length > 0 && ` (${toolSteps.length} step${toolSteps.length > 1 ? 's' : ''})`}
                </span>
                <ChevronDown className={`w-2.5 h-2.5 transition-transform duration-200 ${isOpen ? 'rotate-180' : ''}`} />
            </button>
            {isOpen && (
                <div className="mt-1 ml-0.5 pl-2 border-l-2 border-dashed border-gray-200 dark:border-gray-700 text-[10px] animate-in fade-in slide-in-from-top-1 duration-150">
                    {visibleSteps.map((s, i) => <ThinkingStep key={i} step={s} />)}
                    <TaskList expanded={isStreaming} />
                </div>
            )}
        </div>
    )
}
