import { defineConfig } from '@playwright/test'

// One Electron at a time: each test starts its own copy with its own folders.
export default defineConfig({
  testDir: 'e2e',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  workers: 1,
  forbidOnly: !!process.env.CI,
  reporter: 'list',
})
