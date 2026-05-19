import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../src/berth/ui',
    emptyOutDir: true,
    assetsDir: 'assets',
  },
  server: {
    proxy: {
      // Daemon serves TLS on 11500 now; secure:false skips self-signed verify.
      '/admin': { target: 'https://127.0.0.1:11500', secure: false, changeOrigin: true },
      '/v1':    { target: 'https://127.0.0.1:11500', secure: false, changeOrigin: true },
    },
  },
})
