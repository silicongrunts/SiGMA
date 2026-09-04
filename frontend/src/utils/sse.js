/**
 * Shared SSE stream parser used by every streaming consumer (chat, annotation
 * replies, settings checks).
 *
 * Wire contract:
 *   - Events are separated by a blank line ("\n\n"); a trailing chunk that
 *     ends mid-event is kept in the buffer until the boundary arrives.
 *   - A `data: ` payload may span multiple lines; the lines are joined with
 *     "\n" and parsed as a single JSON document.
 *   - Each event carries `id: <seq>`, a monotonic sequence number per task
 *     stream. The parser surfaces that integer to onEvent; callers track the
 *     last applied seq and pass it back as the `cursor` query parameter when
 *     reconnecting, and the server replays only events with a higher seq.
 *
 * `parseSSEEvent` parses one pre-split event block;
 * `createSSEStreamParser` drains a fetch ReadableStream and dispatches
 * parsed events to its callbacks.
 */

/**
 * Parse a single SSE event string (one id:/event: + data: block separated by
 * \n\n). Returns { type, data, id } or null if unparseable. `id` is the
 * integer parsed from the `id: ` line, or null when absent/unparseable.
 */
export function parseSSEEvent(rawText) {
  const lines = rawText.split('\n')
  let eventType = null
  let dataLines = []
  let id = null

  for (const line of lines) {
    if (line.startsWith('id: ')) {
      const parsedId = Number.parseInt(line.slice(4).trim(), 10)
      id = Number.isInteger(parsedId) ? parsedId : null
    } else if (line.startsWith('event: ')) {
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

  return { type: eventType, data, id }
}

/**
 * Create an SSE stream parser that reads from a ReadableStream reader.
 *
 * Usage:
 *   const parser = createSSEStreamParser({
 *     onEvent: (type, data, id) => { ... },
 *     onError: (err) => { ... },
 *     onDone: () => { ... },
 *   })
 *   const reader = response.body.getReader()
 *   const decoder = new TextDecoder()
 *   await parser.start(reader, decoder, signal)
 *
 * @param {object} callbacks
 * @param {function} callbacks.onEvent  — (eventType: string, data: object, id: number|null) => void
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

          // done / error / cancelled are all terminal: the server ends the
          // subscription after delivering one, so any of them suppresses the
          // synthetic onDone and a future onDone consumer cannot
          // double-finalize the turn.
          if (ev.type === 'done' || ev.type === 'error' || ev.type === 'cancelled') {
            receivedDoneEvent = true
          }

          if (onEvent) {
            try {
              onEvent(ev.type, ev.data, ev.id)
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
      try { reader.releaseLock() } catch (e) {
        // releaseLock throws if the reader is mid-read or already released;
        // either way the stream is finished, so the lock state is irrelevant.
      }

      // Fire done if we never received an explicit done event
      if (!receivedDoneEvent && onDone) {
        onDone()
      }
    }
  }

  return { start }
}
