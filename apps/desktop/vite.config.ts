import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// base './' lets the built bundle load from file:// inside Electron.
export default defineConfig({
  base: './',
  plugins: [react()],
  server: { port: 5173, strictPort: true },
  build: { outDir: 'dist', emptyOutDir: true },
});