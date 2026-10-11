import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    // Default: node (pure logic). Component tests opt into jsdom
    // per-file via `// @vitest-environment jsdom`.
    environment: 'node',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    // No network, no Tauri backend in tests.
    // Backend access goes only through the mocked invoke helper.
    testTimeout: 10000,
  },
})
