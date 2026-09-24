import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Dev proxy keeps local testing CORS-free: frontend :5173 -> FastAPI :8000.
// In production (Coolify) both are served behind the same domain.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Dev server often runs in WSL over /mnt/c where inotify misses Windows-side
    // edits — poll so file changes are always picked up.
    watch: {
      usePolling: true,
      interval: 400,
    },
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/health': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      // Unpushed generated-site previews are served by the API itself;
      // keep them same-origin so relative /preview links resolve in every env.
      '/preview': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  preview: {
    port: 4173,
  },
})
