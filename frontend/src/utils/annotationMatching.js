/**
 * Annotation Matching Utilities
 *
 * Pure functions for resolving annotation positions against the current document.
 * No React/CodeMirror dependencies — works with plain strings.
 *
 * Matching priority (5 tiers):
 *   1. Exact match at stored position          → status='exact'  (solid underline)
 *   2. Exact match elsewhere, unique            → status='exact'  (solid)
 *   3. Exact match elsewhere, multiple          → status='fuzzy'  (dashed), closest pos
 *   4. Fuzzy match via Levenshtein sliding window → status='fuzzy' (dashed), >=40% similarity
 *   5. No match                                 → status='orphan' (dashed, pinned at doc end)
 */

/**
 * Compute Levenshtein edit distance between two strings.
 * O(n*m) DP with 2-row optimization.
 */
function levenshtein(a, b) {
  if (a.length === 0) return b.length
  if (b.length === 0) return a.length

  let prev = Array.from({ length: b.length + 1 }, (_, i) => i)
  let curr = new Array(b.length + 1).fill(0)

  for (let i = 0; i < a.length; i++) {
    curr[0] = i + 1
    for (let j = 0; j < b.length; j++) {
      const cost = a[i] === b[j] ? 0 : 1
      curr[j + 1] = Math.min(
        curr[j] + 1,       // insertion
        prev[j + 1] + 1,   // deletion
        prev[j] + cost     // substitution
      )
    }
    const tmp = prev; prev = curr; curr = tmp
    curr.fill(0)
  }

  return prev[b.length]
}

/**
 * Find the longest common substring (contiguous) between two strings.
 * O(n*m) DP with 2-row optimization.
 * Returns { text, aStart, bStart, length } or null if no match.
 */
export function longestCommonSubstring(a, b) {
  if (!a || !b) return null

  let maxLen = 0
  let endA = 0
  let endB = 0

  let prev = new Array(b.length + 1).fill(0)
  let curr = new Array(b.length + 1).fill(0)

  for (let i = 0; i < a.length; i++) {
    for (let j = 0; j < b.length; j++) {
      if (a[i] === b[j]) {
        curr[j + 1] = prev[j] + 1
        if (curr[j + 1] > maxLen) {
          maxLen = curr[j + 1]
          endA = i + 1
          endB = j + 1
        }
      } else {
        curr[j + 1] = 0
      }
    }
    const tmp = prev; prev = curr; curr = tmp
    curr.fill(0)
  }

  if (maxLen === 0) return null

  return {
    text: a.slice(endA - maxLen, endA),
    aStart: endA - maxLen,
    bStart: endB - maxLen,
    length: maxLen,
  }
}

/**
 * Find all starting indices of needle in doc.
 * Returns array of indices (empty if not found).
 */
export function findAllOccurrences(doc, needle) {
  if (!needle) return []
  const indices = []
  let start = 0
  while (start < doc.length) {
    const idx = doc.indexOf(needle, start)
    if (idx === -1) break
    indices.push(idx)
    start = idx + 1
  }
  return indices
}

/**
 * Fuzzy match: find the best-matching substring in the search region
 * using Levenshtein-distance ratio on a sliding window.
 *
 * Returns { from, to, originalText, score } or null if no match >= threshold.
 */
function fuzzyFind(originalText, doc, storedFrom, storedTo) {
  const len = originalText.length
  const searchStart = Math.max(0, storedFrom - len)
  const searchEnd = Math.min(doc.length, storedTo + len)

  // Minimum window length: at least 1 char or 30% of original length
  const minWin = Math.max(1, Math.floor(len * 0.3))
  // Maximum window length: original length + 50%
  const maxWin = Math.min(doc.length - searchStart, Math.ceil(len * 1.5))

  let bestScore = 0
  let bestMatch = null

  // Slide variable-length windows across the search region
  for (let i = searchStart; i < searchEnd; i++) {
    for (let w = minWin; w <= maxWin && i + w <= doc.length; w++) {
      const candidate = doc.slice(i, i + w)
      const dist = levenshtein(originalText, candidate)
      const score = 1 - dist / Math.max(len, w)

      if (score >= 0.4 && score > bestScore + 0.001) {
        bestScore = score
        bestMatch = { from: i, to: i + w, originalText: candidate, score }
      } else if (score >= 0.4 && Math.abs(score - bestScore) < 0.001 && w < (bestMatch?.to - bestMatch?.from)) {
        // Tiebreaker: prefer shorter windows with equal scores (cleaner match)
        bestMatch = { from: i, to: i + w, originalText: candidate, score }
      }
    }
  }

  return bestMatch
}

/**
 * Core 5-tier annotation matcher.
 *
 * @param {string} doc - current document content
 * @param {{ from: number, to: number, originalText: string }} annotation
 * @returns {{ status: 'exact'|'fuzzy'|'orphan', from: number, to: number, originalText: string }}
 */
export function matchAnnotation(doc, annotation) {
  const { from: storedFrom, to: storedTo, originalText } = annotation

  // Guard: empty document
  if (doc.length === 0) {
    return { status: 'orphan', from: 0, to: 0, originalText }
  }

  // Guard: no original text to match against
  if (!originalText) {
    const pos = Math.max(0, doc.length - 1)
    return { status: 'orphan', from: pos, to: doc.length, originalText }
  }

  const len = originalText.length

  // ── Tier 1: Exact match at stored position ──
  if (storedFrom != null && storedTo != null && storedFrom >= 0 && storedTo <= doc.length && storedFrom < storedTo) {
    if (doc.slice(storedFrom, storedTo) === originalText) {
      return { status: 'exact', from: storedFrom, to: storedTo, originalText }
    }
  }

  // ── Tier 2+3: Exact match elsewhere ──
  const occurrences = findAllOccurrences(doc, originalText)

  if (occurrences.length === 1) {
    // Unique — treat as exact
    return { status: 'exact', from: occurrences[0], to: occurrences[0] + len, originalText }
  }

  if (occurrences.length > 1) {
    // Multiple — pick closest to stored position, mark as fuzzy
    const ref = storedFrom ?? 0
    const best = occurrences.reduce((a, b) =>
      Math.abs(b - ref) < Math.abs(a - ref) ? b : a
    )
    return { status: 'fuzzy', from: best, to: best + len, originalText }
  }

  // ── Tier 4: Fuzzy match ──
  // Compute both LCS (clean contiguous) and Levenshtein (handles substitutions).
  // Pick the one with the higher similarity score.
  // On tie, prefer LCS (cleaner, fewer extraneous characters).

  const searchStart = Math.max(0, storedFrom - len)
  const searchEnd = Math.min(doc.length, storedTo + len)
  const searchRegion = doc.slice(searchStart, searchEnd)
  const threshold = Math.max(1, Math.ceil(len * 0.4))

  // 4a: Find best LCS match
  let lcsMatch = null
  const lcs = longestCommonSubstring(searchRegion, originalText)
  if (lcs && lcs.length >= threshold) {
    const targetInSearch = storedFrom - searchStart
    const lcsOccurrences = findAllOccurrences(searchRegion, lcs.text)
    let bestOcc = lcsOccurrences[0]
    let bestDist = Math.abs(bestOcc - targetInSearch)
    for (const occ of lcsOccurrences) {
      const dist = Math.abs(occ - targetInSearch)
      if (dist < bestDist) { bestDist = dist; bestOcc = occ }
    }
    lcsMatch = {
      from: searchStart + bestOcc,
      to: searchStart + bestOcc + lcs.length,
      originalText: lcs.text,
      score: lcs.length / len,
    }
  }

  // 4b: Find best Levenshtein sliding-window match
  const fuzzy = fuzzyFind(originalText, doc, storedFrom ?? 0, storedTo ?? 0 ?? 0)

  // Pick the better match
  const lcsScore = lcsMatch?.score ?? 0
  const fuzzyScore = fuzzy?.score ?? 0

  if (lcsScore >= 0.4 && lcsScore >= fuzzyScore) {
    return { status: 'fuzzy', from: lcsMatch.from, to: lcsMatch.to, originalText: lcsMatch.originalText }
  }

  if (fuzzyScore >= 0.4) {
    return { status: 'fuzzy', from: fuzzy.from, to: fuzzy.to, originalText: fuzzy.originalText }
  }

  // ── Tier 5: No match — pin to document end ──
  const lastPos = Math.max(0, doc.length - 1)
  return { status: 'orphan', from: lastPos, to: lastPos + 1, originalText }
}
