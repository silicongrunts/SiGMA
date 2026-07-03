import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App.jsx'
import './index.css'
import 'katex/dist/katex.min.css'
import 'pdfjs-dist/web/pdf_viewer.css'
import './i18n'
import { initTheme } from './hooks/useTheme'
import { initLanguage } from './hooks/useLanguage'
import { storage } from './utils/storage'

// Apply the saved theme and language before React mounts to avoid a flash of
// the wrong values.
storage.initialize()
initTheme()
initLanguage()

// Initialize PDF.js worker (local, no CDN)
import * as pdfjsLib from 'pdfjs-dist'
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.min.js?url'
pdfjsLib.GlobalWorkerOptions.workerSrc = pdfWorkerUrl

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
)
