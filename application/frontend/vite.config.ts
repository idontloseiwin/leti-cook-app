import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';

const backend = 'http://127.0.0.1:8000';

export default defineConfig(() => {
  const ownerModule = (id: string) => /\/src\/(?:owner\/|owner-main\.)|\/owner-studio\.html(?:$|\?)/.test(id.replaceAll('\\', '/'));
  const excludeOwner: Plugin = {
    name: 'self-hosted-excludes-owner-studio',
    generateBundle(_options, bundle) {
      for (const [name, output] of Object.entries(bundle)) {
        if (name === 'owner-studio.html' || (output.type === 'chunk' && Object.keys(output.modules).some(ownerModule))) {
          throw new Error('Owner Studio must not be bundled in the self-hosted profile');
        }
      }
    },
  };
  return {
    plugins: [react(), excludeOwner],
    define: { 'import.meta.env.VITE_LIC_DEPLOYMENT_PROFILE': JSON.stringify('self-hosted') },
    base: '/app-explorer-v3/',
    build: {
      outDir: 'dist-self-hosted',
      rollupOptions: {
        input: {
          explorerV3: 'explorer-v3.html',
        },
        output: {
          manualChunks(id) {
            if (id.includes('node_modules/react') || id.includes('node_modules/react-dom')) return 'react';
            if (id.includes('node_modules/lucide-react')) return 'icons';
            return undefined;
          },
        },
      },
    },
    server: {
      port: 5173,
      proxy: {
        '/add-channel': backend,
        '/folders': backend,
        '/sidebar-data': backend,
        '/sidebar-user-data': backend,
        '/system-summary': backend,
        '/system': backend,
        '/telemetry': backend,
        '/explorer': backend,
        '/saved': backend,
        '/user-work': backend,
        '/channels': backend,
        '/video': backend,
      },
    },
  };
});
