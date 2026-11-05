import { fileURLToPath, URL } from 'node:url'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    // `@/` -> src/. Must match "paths" in tsconfig.json / tsconfig.app.json;
    // TypeScript only type-checks the alias, Vite is what actually resolves it.
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    // Same-origin /api in dev: no CORS, and SSE streams pass straight through.
    // No path rewrite: the backend itself serves under /api (see api/app.py).
    proxy: { '/api': 'http://localhost:8000' },
  },
})
