import { defineConfig, mergeConfig } from 'vitest/config'

import viteConfig from './vite.config'

// Message times are shown in the viewer's local zone; pin it so tests can assert exact
// times on any machine. Set here, before the test workers start, so they inherit it.
process.env.TZ = 'Asia/Kolkata'

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
