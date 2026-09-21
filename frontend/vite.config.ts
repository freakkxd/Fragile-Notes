import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['react', 'react-dom', 'zustand'],
          md: ['marked', 'dompurify'],
        },
      },
    },
  },
  server: { port: 5173, strictPort: true },
  clearScreen: false,
  envPrefix: ['VITE_', 'TAURI_'],
})
