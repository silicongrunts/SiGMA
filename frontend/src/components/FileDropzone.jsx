import { useState, useRef, useCallback } from 'react'
import { Upload } from 'lucide-react'

export async function collectDropEntries(dataTransfer) {
  const items = Array.from(dataTransfer.items || [])
  const entries = []
  const queue = []

  for (const item of items) {
    const entry = item.webkitGetAsEntry?.()
    if (entry) queue.push({ entry, basePath: '' })
  }

  while (queue.length > 0) {
    const { entry, basePath } = queue.shift()
    if (entry.isFile) {
      const file = await new Promise((resolve, reject) => { entry.file(resolve, reject) })
      entries.push({
        file,
        relativePath: basePath ? `${basePath}/${file.name}` : file.name,
      })
    } else if (entry.isDirectory) {
      const reader = entry.createReader()
      const children = await new Promise((resolve) => {
        const results = []
        const readBatch = () => {
          reader.readEntries((batch) => {
            if (batch.length === 0) { resolve(results); return }
            results.push(...batch)
            readBatch()
          })
        }
        readBatch()
      })
      for (const child of children) {
        queue.push({
          entry: child,
          basePath: basePath ? `${basePath}/${entry.name}` : entry.name,
        })
      }
    }
  }

  if (entries.length > 0) return entries
  return Array.from(dataTransfer.files || []).map(file => ({
    file,
    relativePath: file.webkitRelativePath || file.name,
  }))
}

/**
 * Reusable file-drop zone.  Visual only — the consumer provides *onFiles*
 * to handle the actual upload / selection logic.
 *
 * @param {Object}   props
 * @param {Function} props.onFiles       Called with File[] when files are dropped or selected.
 * @param {boolean}  [props.disabled]     When true, drop/click is ignored.
 * @param {string}   [props.className]    Additional classes on the outer wrapper.
 * @param {ReactNode}[props.children]     Custom prompt text (defaults to "Drop files here or click to browse").
 */
export function FileDropzone({ onFiles, disabled = false, className = '', children }) {
  const [dragOver, setDragOver] = useState(false)
  const inputRef = useRef(null)

  const handleDrag = useCallback((e) => {
    e.preventDefault()
    e.stopPropagation()
  }, [])

  const handleDragIn = useCallback((e) => {
    e.preventDefault()
    e.stopPropagation()
    if (!disabled) setDragOver(true)
  }, [disabled])

  const handleDragOut = useCallback((e) => {
    e.preventDefault()
    e.stopPropagation()
    setDragOver(false)
  }, [])

  const handleDrop = useCallback(async (e) => {
    e.preventDefault()
    e.stopPropagation()
    setDragOver(false)
    if (disabled) return
    const entries = await collectDropEntries(e.dataTransfer)
    if (entries.length > 0) onFiles(entries)
  }, [disabled, onFiles])

  const handleClick = () => {
    if (!disabled) inputRef.current?.click()
  }

  const handleInputChange = (e) => {
    const entries = Array.from(e.target.files).map(file => ({
      file,
      relativePath: file.webkitRelativePath || file.name,
    }))
    if (entries.length > 0) onFiles(entries)
    e.target.value = '' // allow re-selecting the same file
  }

  return (
    <div
      onDragEnter={handleDragIn}
      onDragLeave={handleDragOut}
      onDragOver={handleDrag}
      onDrop={handleDrop}
      onClick={handleClick}
      className={`border-2 border-dashed rounded-xl p-6 text-center cursor-pointer transition-colors ${
        dragOver
          ? 'border-sigma-600 bg-sigma-50 dark:bg-sigma-600/20'
          : 'border-gray-200 dark:border-gray-700 hover:border-gray-300 dark:hover:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-800'
      } ${disabled ? 'opacity-50 cursor-not-allowed' : ''} ${className}`}
    >
      <Upload className="w-6 h-6 text-gray-300 dark:text-gray-600 mx-auto mb-2" />
      <p className="text-sm font-medium text-gray-500 dark:text-gray-400">
        {children || 'Drop files here or click to browse'}
      </p>
      <input
        ref={inputRef}
        type="file"
        multiple
        onChange={handleInputChange}
        className="hidden"
      />
    </div>
  )
}
