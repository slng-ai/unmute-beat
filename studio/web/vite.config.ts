import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// `slng studio up --dev` runs this beside the engine and sets STUDIO_ENGINE; /api goes there.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 7802,
    strictPort: true,
    proxy: { '/api': process.env.STUDIO_ENGINE ?? 'http://127.0.0.1:7800' },
  },
});
