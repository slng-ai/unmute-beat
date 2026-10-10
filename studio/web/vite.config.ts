import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// `slng studio up --dev` runs this beside the engine and sets STUDIO_ENGINE; /api goes there.
export default defineConfig({
  plugins: [react()],
  // The map's chunk carries ELK (about 1.6 MB, 0.5 MB gzipped); it loads only when the map opens.
  build: { chunkSizeWarningLimit: 1800 },
  server: {
    port: 7802,
    strictPort: true,
    proxy: { '/api': process.env.STUDIO_ENGINE ?? 'http://127.0.0.1:7800' },
  },
});
