import { Spinner } from './Spinner'

/**
 * Local overlay covering the nearest positioned ancestor (parent must be `relative`).
 * Matches the existing FileTree "extracting" overlay styling.
 *
 * @param {string} [label] - optional caption below the spinner
 * @param {string} [backdropClassName] - override the default backdrop
 * @param {string} [className] - extra classes on the overlay container
 */
export function LoadingOverlay({ label, backdropClassName = 'bg-white/70 dark:bg-gray-900/70 backdrop-blur-sm', className = '' }) {
  return (
    <div
      className={`absolute inset-0 z-50 flex flex-col items-center justify-center ${backdropClassName} ${className}`}
    >
      <Spinner size="md" className="text-sigma-600" />
      {label && <p className="mt-2 text-xs font-semibold text-sigma-600">{label}</p>}
    </div>
  )
}
