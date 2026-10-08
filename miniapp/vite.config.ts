import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Telegram opens the Mini App only over HTTPS: in dev, expose port 5173 through a tunnel
// (see .claude/skills/run-miniapp). only *.trycloudflare.com hosts are let through.
// The dev server listens on localhost; for a tunnel run it as `VITE_HOST=0.0.0.0 npm run dev`.
export default defineConfig({
  plugins: [react()],
  base: './',
  // The program JSON is imported from ../data/programs, outside the Vite root.
  server: {
    host: process.env.VITE_HOST ?? 'localhost',
    port: 5173,
    allowedHosts: ['.trycloudflare.com'],
    fs: {
      allow: ['../data/programs', '../data/progression_cases.json', '.'],
      deny: ['**/*.db*', '**/backups/**', '.env*'],
    },
    proxy: { '/api': { target: 'http://localhost:8000', xfwd: true } },
  },
})
