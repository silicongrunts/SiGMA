import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import fs from 'fs'
import path from 'path'

function pdfjsAssetsPlugin() {
  const pdfjsRoot = path.resolve(__dirname, 'node_modules/pdfjs-dist')
  const assetRoots = {
    '/pdfjs/cmaps/': path.join(pdfjsRoot, 'cmaps'),
    '/pdfjs/standard_fonts/': path.join(pdfjsRoot, 'standard_fonts'),
  }

  const isInside = (root, filePath) => {
    const relativePath = path.relative(root, filePath)
    return relativePath && !relativePath.startsWith('..') && !path.isAbsolute(relativePath)
  }

  return {
    name: 'sigma-pdfjs-assets',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const requestUrl = req.url?.split('?')[0] || ''
        const route = Object.keys(assetRoots).find(prefix => requestUrl.startsWith(prefix))
        if (!route) {
          next()
          return
        }

        const relativePath = requestUrl.slice(route.length)
        const filePath = path.resolve(assetRoots[route], relativePath)
        if (!isInside(assetRoots[route], filePath) || !fs.existsSync(filePath)) {
          next()
          return
        }

        fs.createReadStream(filePath).pipe(res)
      })
    },
    closeBundle() {
      for (const [route, sourceDir] of Object.entries(assetRoots)) {
        fs.cpSync(sourceDir, path.resolve(__dirname, 'dist', route.slice(1)), {
          recursive: true,
        })
      }
    },
  }
}

export default defineConfig({
  plugins: [react(), pdfjsAssetsPlugin()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    host: '0.0.0.0',
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        ws: true,
      },
      '/vnc.html': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/novnc': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
  optimizeDeps: {
    include: [
      '@codemirror/state',
      '@codemirror/view',
      '@codemirror/language',
      '@codemirror/commands',
      '@codemirror/search',
      '@codemirror/autocomplete',
      '@codemirror/lint',
      '@codemirror/lang-javascript',
      '@codemirror/lang-python',
      '@codemirror/lang-json',
      '@codemirror/lang-markdown',
      '@codemirror/lang-html',
      '@codemirror/lang-css',
      '@codemirror/lang-cpp',
      'codemirror',
      'react',
      'react-dom',
      'react-router-dom',
      'zustand',
    ],
    exclude: ['@codemirror/legacy-modes', '@novnc/novnc'],
  },
})
