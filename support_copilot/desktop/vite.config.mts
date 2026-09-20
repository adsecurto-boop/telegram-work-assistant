import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'path';
import { fileURLToPath } from 'url';

const directory = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [react()],
  root: path.resolve(directory, 'src/renderer'),
  build: {
    outDir: path.resolve(directory, 'dist/renderer'),
    emptyOutDir: true,
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: [path.resolve(directory, 'tests/setup.ts')],
    root: path.resolve(directory),
  },
});
