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
 *   4. Fuzzy match (LCS or Myers, best wins)    → status='fuzzy'  (dashed), >=40% similarity
 *   5. No match                                 → status='orphan' (dashed, pinned at doc end)
 */

import search from 'approx-string-match'

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
 * Fuzzy match: find the best-matching substring of `doc` for `originalText`,
 * constrained to the stored anchor neighborhood.
 *
 * Uses Myers' bit-vector approximate string matching (O((k/w)·n) where k is
 * the error budget and w is the 32-bit word size), with Ukkonen-style cutoff
 * that stops expanding any region whose error count exceeds `maxDistance`.
 *
 * Returns { from, to, originalText, score } or null if no match within
 * `maxDistance` (≈ 15% of original length, clamped to [20, 200]).
 */
function fuzzyFind(originalText, doc, storedFrom, storedTo) {
  const len = originalText.length
  // Restrict the search to a neighborhood around the stored anchor so we
  // don't match similar text elsewhere in the document.
  const searchStart = Math.max(0, storedFrom - len)
  const searchEnd = Math.min(doc.length, storedTo + len)
  const region = doc.slice(searchStart, searchEnd)

  // Allow ~15% of characters to differ, bounded so short annotations stay
  // matchable and long ones don't blow up the band width.
  const maxDistance = Math.min(200, Math.max(20, Math.floor(len * 0.15)))

  // `search` returns only the lowest-error matches. Among equal-error
  // matches, pick the highest score (shortest span relative to original).
  const matches = search(region, originalText, maxDistance)
  if (matches.length === 0) return null

  const best = matches.reduce((p, m) => {
    const s = 1 - m.errors / Math.max(len, m.end - m.start)
    return s > p.score ? { score: s, start: m.start, end: m.end, errors: m.errors } : p
  }, { score: -1 })
  const from = searchStart + best.start
  const to = searchStart + best.end
  return {
    from,
    to,
    originalText: doc.slice(from, to),
    score: best.score,
  }
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
  // Two independent matchers, both must score >= 0.4 to be accepted:
  //   4a: longest common substring — clean contiguous overlap, good when
  //       the text was moved but barely edited
  //   4b: Myers bit-vector search — tolerates substitutions/indels, good
  //       when the text was lightly edited in place
  // Keep the higher score; on tie prefer LCS (cleaner span).

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

  // 4b: Myers bit-vector search
  const fuzzy = fuzzyFind(originalText, doc, storedFrom ?? 0, storedTo ?? 0)

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
