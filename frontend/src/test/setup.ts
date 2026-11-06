// Adds DOM matchers (toBeInTheDocument, toHaveTextContent, ...) to Vitest's expect,
// and unmounts rendered components after each test.
import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => cleanup())
