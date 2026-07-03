import { Spinner } from './Spinner'

/**
 * Button with built-in loading state.
 * When `loading` is true the button is disabled, shows a spinner, and swaps its label.
 * Defaults include the standard disabled + flex layout used across the app.
 */
export function LoadingButton({
  loading = false,
  loadingLabel,
  children,
  disabled,
  className = '',
  ...rest
}) {
  return (
    <button
      disabled={disabled || loading}
      className={`disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2 ${className}`}
      {...rest}
    >
      {loading && <Spinner size="sm" />}
      {loading ? (loadingLabel || children) : children}
    </button>
  )
}
