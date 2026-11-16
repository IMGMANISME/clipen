import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// What `npx @clipen/clipen-cli` unpacks: the program and its pinned font list, not the fonts themselves.
test('the npm package carries the program, not the fonts or build leftovers', () => {
  const packed = spawnSync('npm', ['pack', '--dry-run', '--json'], { encoding: 'utf8' })
  assert.equal(packed.status, 0, packed.stderr)
  const files = JSON.parse(packed.stdout)[0].files.map((file) => file.path)
  for (const needed of ['bin/clipen.js', 'clipen', 'pyproject.toml', 'requirements/runtime.txt', 'apps/web/dist/index.html',
    'src/clipen/bootstrap.py', 'src/clipen/rendering/fonts/manifest.json', 'src/clipen/rendering/fonts/yomogi-OFL.txt']) {
    assert.ok(files.includes(needed), needed)
  }
  assert.deepEqual(files.filter((file) => file.endsWith('.ttf') || file.includes('__pycache__') || file.startsWith('tests/')), [])
})

test('the package version follows the Python project', () => {
  const python = readFileSync('pyproject.toml', 'utf8').match(/^version = "([^"]+)"/m)[1]
  assert.equal(JSON.parse(readFileSync('package.json', 'utf8')).version, python)
})
