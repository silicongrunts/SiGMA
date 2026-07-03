import { useState, useEffect, useCallback } from 'react'
import { create } from 'zustand'
import { AlertCircle, CheckCircle2, Info, X } from 'lucide-react'

const useToastStore = create((set) => ({
  toasts: [],
  addToast: (toast) => set((state) => ({ 
    toasts: [...state.toasts, { ...toast, id: Date.now() }] 
  })),
  removeToast: (id) => set((state) => ({ 
    toasts: state.toasts.filter((t) => t.id !== id) 
  })),
}))

export function ToastContainer() {
  const toasts = useToastStore((state) => state.toasts)
  const removeToast = useToastStore((state) => state.removeToast)

  return (
    <div className="fixed top-8 right-8 z-[9999] flex flex-col gap-4 pointer-events-none">
      {toasts.map((toast) => (
        <Toast key={toast.id} toast={toast} onRemove={() => removeToast(toast.id)} />
      ))}
    </div>
  )
}

function Toast({ toast, onRemove }) {
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    const timer = setTimeout(() => setVisible(true), 10)
    const removeTimer = setTimeout(() => {
        setVisible(false)
        setTimeout(onRemove, 400)
    }, 4000) // Increased display time to 4s
    return () => { clearTimeout(timer); clearTimeout(removeTimer); }
  }, [onRemove])

  const icons = {
    success: <CheckCircle2 className="w-6 h-6 text-green-500" />,
    error: <AlertCircle className="w-6 h-6 text-red-500" />,
    info: <Info className="w-6 h-6 text-blue-500" />,
  }

  const colors = {
    success: 'border-green-200 dark:border-green-800/50 bg-green-50/90 dark:bg-green-900/30',
    error: 'border-red-200 dark:border-red-800/50 bg-red-50/90 dark:bg-red-900/30',
    info: 'border-blue-200 dark:border-blue-800/50 bg-blue-50/90 dark:bg-blue-900/30',
  }

  return (
    <div
      className={`
        flex items-center gap-4 px-6 py-4 rounded-2xl border shadow-[0_20px_50px_rgba(0,0,0,0.15)] backdrop-blur-md pointer-events-auto transition-all duration-500 transform
        ${visible ? 'translate-x-0 opacity-100 scale-100' : 'translate-x-20 opacity-0 scale-90'}
        ${colors[toast.type]}
      `}
      style={{ minWidth: '340px' }}
    >
      <div className="flex-shrink-0 drop-shadow-sm">{icons[toast.type]}</div>
      <div className="flex-1 text-base font-bold text-gray-900 dark:text-gray-100 tracking-tight">{toast.message}</div>
      <button onClick={() => { setVisible(false); setTimeout(onRemove, 400); }} className="p-1.5 hover:bg-white/50 dark:hover:bg-gray-800/50 rounded-xl text-gray-500 dark:text-gray-400 transition-colors">
        <X className="w-5 h-5" />
      </button>
    </div>
  )
}

export const toastSuccess = (msg) => useToastStore.getState().addToast({ type: 'success', message: msg })
export const toastError = (msg) => useToastStore.getState().addToast({ type: 'error', message: msg })
export const toastInfo = (msg) => useToastStore.getState().addToast({ type: 'info', message: msg })
