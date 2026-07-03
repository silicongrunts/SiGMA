/**
 * Clipboard utility — copy text with a fallback for non-HTTPS contexts.
 *
 * Browsers restrict navigator.clipboard to secure contexts (HTTPS, localhost).
 * When the app is accessed via plain HTTP (IP + port), the Clipboard API throws.
 * This module falls back to a hidden textarea + execCommand('copy') in that case.
 */

export async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text)
    return
  } catch {
    // Not a secure context or permission denied — try legacy fallback.
  }

  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.style.position = 'fixed'
  textarea.style.left = '-9999px'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  try {
    textarea.select()
    if (!document.execCommand('copy')) {
      throw new Error('execCommand copy failed')
    }
  } finally {
    document.body.removeChild(textarea)
  }
}
