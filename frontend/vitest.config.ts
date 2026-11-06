import { defineConfig, mergeConfig } from 'vitest/config'

import viteConfig from './vite.config'

// Reuses the Vite config (the `@/` alias, the React plugin) so tests resolve imports
// exactly as the app does; jsdom stands in for the browser.
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      css: false,
    },
  }),
)
