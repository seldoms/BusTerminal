import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const projectRoot = dirname(fileURLToPath(import.meta.url))
const apiTarget = process.env.BUS_TERMINAL_API_TARGET || 'http://127.0.0.1:8001'

// https://vitejs.dev/config/
export default defineConfig({
    root: projectRoot,
    cacheDir: resolve(projectRoot, 'node_modules/.vite'),
    plugins: [react()],
    server: {
        host: true,
        port: 3000,
        watch: {
            usePolling: true,
        },
        proxy: {
            '/api': {
                target: apiTarget,
                changeOrigin: true,
                timeout: 300000,
            }
        }
    },
    build: {
        outDir: 'dist',
        sourcemap: false,
        rollupOptions: {
            input: resolve(projectRoot, 'index.html'),
            output: {
                manualChunks: {
                    'react-vendor': ['react', 'react-dom', 'react-router-dom'],
                }
            }
        }
    }
})
