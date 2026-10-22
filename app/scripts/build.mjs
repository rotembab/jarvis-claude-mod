// Builds the app with esbuild: main and preload for Electron's Node side, the
// two pages for its Chromium side, and (with `tests`) the unit tests.
import { build } from 'esbuild'
import { cpSync, mkdirSync, readdirSync, rmSync } from 'node:fs'

const common = { bundle: true, logLevel: 'warning', sourcemap: 'linked' }

if (process.argv[2] === 'tests') {
  // Unit tests run in plain Node: a module that pulls in Electron cannot be one of them.
  const noElectron = {
    name: 'no-electron',
    setup(b) {
      b.onResolve({ filter: /^electron$/ }, () => ({
        errors: [{ text: 'unit tests run without Electron: import it only in main.ts, windows.ts, tray.ts and preload.ts' }],
      }))
    },
  }
  rmSync('dist-test', { recursive: true, force: true })
  await build({
    entryPoints: readdirSync('test').filter(file => file.endsWith('.test.ts')).map(file => `test/${file}`),
    outdir: 'dist-test', bundle: true, platform: 'node', format: 'cjs', target: 'node22',
    sourcemap: 'inline', plugins: [noElectron], logLevel: 'warning',
  })
} else {
  rmSync('dist', { recursive: true, force: true })
  await build({ ...common, entryPoints: ['src/main/main.ts'], outfile: 'dist/main.js', platform: 'node', format: 'cjs', target: 'node22', external: ['electron'] })
  await build({ ...common, entryPoints: ['src/preload/preload.ts'], outfile: 'dist/preload.js', platform: 'node', format: 'cjs', target: 'node22', external: ['electron'] })
  // The pages load over file://, where module scripts are refused: plain scripts.
  await build({ ...common, entryPoints: ['src/renderer/overlay.ts', 'src/renderer/orb.ts'], outdir: 'dist/renderer', platform: 'browser', format: 'iife', target: 'chrome130' })
  mkdirSync('dist/renderer', { recursive: true })
  for (const file of ['overlay.html', 'orb.html', 'style.css']) cpSync(`src/renderer/${file}`, `dist/renderer/${file}`)
}
