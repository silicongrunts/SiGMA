import { findAllOccurrences } from './annotationMatching'

/**
 * Occurrences of `text` that are NOT nested inside an occurrence of
 * `counterpart`.
 *
 * Insertion-style diffs (before is a prefix of after) keep the before text
 * present inside the applied after text, and deletion-style diffs keep the
 * after text present inside the un-applied before text. Raw occurrence counts
 * therefore cannot tell "applied" from "unapplied"; only occurrences outside
 * the counterpart's matches count as independent evidence.
 */
export function findIndependentOccurrences(doc, text, counterpart) {
  if (!doc) return []
  const occ = findAllOccurrences(doc, text)
  const nests = findAllOccurrences(doc, counterpart).map(start => [start, start + counterpart.length])
  if (nests.length === 0) return occ
  return occ.filter(start =>
    !nests.some(([from, to]) => from <= start && start + text.length <= to))
}

/**
 * Resolve a diff suggestion ({ before, after }) against the live document.
 * Shared by the annotation diff panel and the inline diff buttons so both
 * always render the same state:
 *
 *   canApply  — before occurs exactly once independently and no applied copy
 *               exists
 *   canRevert — an applied copy (after) occurs exactly once independently;
 *               before may still appear nested inside it, or as a stray copy
 *   blocked   — neither action is safe:
 *     notFound         — neither text occurs independently
 *     multipleOriginal — before occurs 2+ times (ambiguous, refuse to edit)
 *     multipleApplied  — after occurs 2+ times (ambiguous restore target)
 */
export function computeDiffState(doc, before, after) {
  const beforeOcc = findIndependentOccurrences(doc, before, after)
  const afterOcc = findIndependentOccurrences(doc, after, before)
  const canApply = beforeOcc.length === 1 && afterOcc.length === 0
  const canRevert = afterOcc.length === 1
  let blockedReason = null
  if (!canApply && !canRevert) {
    if (beforeOcc.length === 0 && afterOcc.length === 0) blockedReason = 'notFound'
    else if (beforeOcc.length >= 2) blockedReason = 'multipleOriginal'
    else blockedReason = 'multipleApplied'
  }
  return { canApply, canRevert, blockedReason }
}
