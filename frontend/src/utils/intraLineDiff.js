/**
 * intraLineDiff — word/character-level diff for diff viewers.
 *
 * The backend only ships line-level diffs ({type, content}). To produce a
 * GitHub-style view where the *changed words* inside a changed line are
 * highlighted, we compute a finer-grained diff on the frontend, over each
 * paired remove/add line (or over two free-form strings for the annotation
 * suggestion panel).
 *
 * Strategy mirrors GitHub:
 *  - Word-level by default (diffWordsWithSpace), so unchanged words stay
 *    legible inside an edited line.
 *  - CJK fallback: languages without word separators (Chinese, Japanese,
 *    Korean) would be treated as one giant token by the word differ and lose
 *    all granularity. When CJK characters dominate, fall back to char-level
 *    so each changed glyph is highlighted. SiGMA users write a lot of CJK +
 *    LaTeX, so this path matters.
 *  - Oversized inputs bail out (return null) so a pathological single line
 *    can never stall the UI; the caller then renders the whole-line tint as
 *    before.
 */
import { diffWordsWithSpace, diffChars } from 'diff'

/** Skip intra-line work beyond this length — keeps rendering responsive. */
const MAX_INTRA_LINE_LEN = 2000

/**
 * Unicode ranges for CJK ideographs, kana, and hangul. Used to decide whether
 * a piece of text is dominated by space-less scripts.
 */
const CJK_REGEX = /[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]/g

/**
 * Fraction of CJK characters above which we switch from word- to char-level.
 * 0.3 means "if roughly a third or more of the letters are CJK, treat the
 * text as space-less".
 */
const CJK_THRESHOLD = 0.3

/**
 * Decide whether two texts should be diffed char-by-char instead of
 * word-by-word. Triggered when either side is dominated by CJK.
 *
 * @param {string} a - First text.
 * @param {string} b - Second text.
 * @returns {boolean} True when a char-level diff will read better.
 */
function shouldUseCharLevel(a, b) {
  const cjkCount = (a.match(CJK_REGEX) || []).length + (b.match(CJK_REGEX) || []).length
  const letters = (a.match(/\S/g) || []).length + (b.match(/\S/g) || []).length
  return letters > 0 && cjkCount / letters > CJK_THRESHOLD
}

/**
 * Build two parallel segment arrays (one for the "before"/removed side, one
 * for the "after"/added side) from a jsdiff part list. Equal parts appear on
 * both sides; removed-only parts only on the removed side; added-only parts
 * only on the added side.
 *
 * @param {Array<{added?: boolean, removed?: boolean, value: string}>} parts
 * @returns {{ removed: Array<{text: string, changed: boolean}>, added: Array<{text: string, changed: boolean}> }}
 */
function buildSegments(parts) {
  const removed = []
  const added = []
  for (const part of parts) {
    if (part.added) {
      added.push({ text: part.value, changed: true })
    } else if (part.removed) {
      removed.push({ text: part.value, changed: true })
    } else {
      // Equal segment is shared by both sides and shown without emphasis.
      removed.push({ text: part.value, changed: false })
      added.push({ text: part.value, changed: false })
    }
  }
  return { removed, added }
}

/**
 * Compute a word/character-level diff between two strings.
 *
 * Returns two segment arrays — `removed` for the old text, `added` for the
 * new text — where each segment is marked `changed` when it differs between
 * the sides. Callers render changed segments with a stronger tint and leave
 * unchanged segments in the surrounding line color.
 *
 * Returns `null` when inputs are too long to diff safely; the caller should
 * then fall back to whole-line coloring.
 *
 * @param {string} oldText - The previous text (removed side).
 * @param {string} newText - The new text (added side).
 * @returns {{ removed: Array<{text: string, changed: boolean}>, added: Array<{text: string, changed: boolean}> } | null}
 */
export function intraLineDiff(oldText, newText) {
  if (typeof oldText !== 'string' || typeof newText !== 'string') return null
  if (oldText.length > MAX_INTRA_LINE_LEN || newText.length > MAX_INTRA_LINE_LEN) {
    return null
  }
  // Identical strings have nothing to highlight; let the caller render plain.
  if (oldText === newText) return null
  const parts = shouldUseCharLevel(oldText, newText)
    ? diffChars(oldText, newText)
    : diffWordsWithSpace(oldText, newText)
  return buildSegments(parts)
}
