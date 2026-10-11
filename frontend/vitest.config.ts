import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
    // No DOM, no network, no Tauri backend in unit tests.
    // Backend access goes only through the mocked invoke helper.
    testTimeout: 10000,
  },
})
