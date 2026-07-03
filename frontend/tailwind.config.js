/** @type {import('tailwindcss').Config} */
export default {
  darkMode: 'class',
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        sigma: {
          50: '#f0f7ff',
          100: '#e0effe',
          200: '#bae0fd',
          600: '#2563eb',
          700: '#1d4ed8',
        },
        gemini: {
          bg: '#f8f9fa',
          sidebar: '#ffffff',
          border: '#dee2e6',
        },
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'Menlo', 'monospace'],
      },
    },
  },
  plugins: [],
}
