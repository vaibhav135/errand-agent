// Adds DOM matchers (toBeInTheDocument, toHaveTextContent, ...) to Vitest's expect,
// and unmounts rendered components after each test.
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { toast } from 'sonner'
import { afterEach } from 'vitest'

afterEach(() => {
  cleanup()
  toast.dismiss() // sonner keeps toasts in a module-level store: don't leak between tests
})

// jsdom has no ResizeObserver; react-resizable-panels measures the workspace with one.
// jsdom has no layout either, so an observer that never fires is all a test can use.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
}
