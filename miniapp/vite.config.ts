import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Telegram opens the Mini App only over HTTPS: in dev, expose port 5173 through a tunnel
// (see .claude/skills/run-miniapp). `allowedHosts: true` lets the tunnel hostname through.
export default defineConfig({
  plugins: [react()],
  base: './',
  // The program JSON is imported from ../data/programs, outside the Vite root.
  server: {
    host: true,
    port: 5173,
    allowedHosts: true,
    fs: { allow: ['..'] },
    proxy: { '/api': 'http://localhost:8000' },
  },
})
