import { Loader2 } from 'lucide-react'

const SIZE_CLASSES = {
  xs: 'w-3 h-3',
  sm: 'w-4 h-4',
  md: 'w-6 h-6',
  lg: 'w-8 h-8',
}

/**
 * Unified spinning loader icon.
 * Use this everywhere instead of mixing Loader / Loader2 / RotateCw / RefreshCw.
 *
 * @param {('xs'|'sm'|'md'|'lg')} [size='sm']
 * @param {string} [className] - extra classes appended after size + animate-spin
 */
export function Spinner({ size = 'sm', className = '' }) {
  const sizeClass = SIZE_CLASSES[size] || SIZE_CLASSES.sm
  return <Loader2 className={`${sizeClass} animate-spin ${className}`} />
}
