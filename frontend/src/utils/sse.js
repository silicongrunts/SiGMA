/**
 * Unified SSE stream parser.
 *
 * Consolidates three separate SSE parsing implementations that previously
 * existed in:
 *   - App.jsx (inline while(true) + split('\n\n'))
 *   - ChatPanel.jsx (recursive pump() + parseSSE helper)
 *   - api/index.js agentsAPI.streamResponse (line-by-line, multi-line data)
 *
 * Design decisions:
 *   - Uses the "\n\n" (double-newline) event boundary from App.jsx/ChatPanel
 *     which is standard SSE spec.
 *   - Accumulates multi-line data fields like api/index.js streamResponse
 *     (needed when a data payload contains embedded newlines).
 *   - Provides a single `parseSSEEvent(rawText)` helper for pre-split events.
 *   - Provides `createSSEStreamParser` for streaming ReadableStream usage.
 */

/**
 * Parse a single SSE event string (one event: + data: block separated by \n\n).
 * Returns { type, data } or null if unparseable.
 */
export function parseSSEEvent(rawText) {
  const lines = rawText.split('\n')
  let eventType = null
  let dataLines = []

  for (const line of lines) {
    if (line.startsWith('event: ')) {
      eventType = line.slice(7).trim()
    } else if (line.startsWith('data: ')) {
      dataLines.push(line.slice(6))
    }
  }

  if (!eventType || dataLines.length === 0) return null

  let data = {}
  try {
    data = JSON.parse(dataLines.join('\n'))
  } catch {
    return null
  }

  return { type: eventType, data }
}

/**
 * Create an SSE stream parser that reads from a ReadableStream reader.
 *
 * Usage:
 *   const parser = createSSEStreamParser({
 *     onEvent: (type, data) => { ... },
 *     onError: (err) => { ... },
 *     onDone: () => { ... },
 *   })
 *   const reader = response.body.getReader()
 *   const decoder = new TextDecoder()
 *   await parser.start(reader, decoder, signal)
 *
 * @param {object} callbacks
 * @param {function} callbacks.onEvent  — (eventType: string, data: object) => void
 * @param {function} callbacks.onError  — (error: Error) => void
 * @param {function} callbacks.onDone   — () => void
 * @returns {{ start: (reader, decoder, abortSignal?) => Promise<void> }}
 */
export function createSSEStreamParser({ onEvent, onError, onDone }) {
  let receivedDoneEvent = false

  async function start(reader, decoder, abortSignal) {
    let buffer = ''

    try {
      while (true) {
        if (abortSignal?.aborted) break

        const { value, done } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })

        // SSE events are separated by double newlines
        const parts = buffer.split('\n\n')
        buffer = parts.pop() || '' // last part may be incomplete

        for (const part of parts) {
          if (!part.trim()) continue
          const ev = parseSSEEvent(part)
          if (!ev) continue

          if (ev.type === 'done') {
            receivedDoneEvent = true
          }

          if (onEvent) {
            try {
              onEvent(ev.type, ev.data)
            } catch (e) {
              console.error('[SSE] Callback error:', e)
            }
          }
        }
      }
    } catch (err) {
      if (abortSignal?.aborted) {
        // expected — do nothing
      } else if (onError) {
        onError(err)
      }
    } finally {
      try { reader.releaseLock() } catch {}

      // Fire done if we never received an explicit done event
      if (!receivedDoneEvent && onDone) {
        onDone()
      }
    }
  }

  return { start }
}
