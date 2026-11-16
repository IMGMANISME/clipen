import assert from 'node:assert/strict'
import { cpSync, mkdtempSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import test from 'node:test'
import { PLACES, next, write } from '../../scripts/release.mjs'

test('a release moves the version forward only', () => {
  assert.equal(next('1.2.3', 'patch'), '1.2.4')
  assert.equal(next('1.2.3', 'minor'), '1.3.0')
  assert.equal(next('1.2.3', 'major'), '2.0.0')
  assert.equal(next('1.2.3', '1.10.0'), '1.10.0')
  for (const stale of ['1.2.3', '1.2.2', '0.9.9']) assert.throws(() => next('1.2.3', stale))
  assert.throws(() => next('1.2.3', undefined))
})

test('the version is written everywhere Clipen states it, and only when they agree', () => {
  const root = mkdtempSync(path.join(tmpdir(), 'clipen-release-'))
  for (const [file] of PLACES) cpSync(file, path.join(root, file), { recursive: true })
  const current = JSON.parse(readFileSync('package.json', 'utf8')).version
  assert.equal(write(root, '9.9.9'), current)
  for (const [file, pattern] of PLACES) assert.equal(readFileSync(path.join(root, file), 'utf8').match(pattern)[2], '9.9.9', file)
  // A place left behind on another version stops the release before anything is written.
  cpSync('pyproject.toml', path.join(root, 'pyproject.toml'))
  assert.throws(() => write(root, '10.0.0'), /版本號不一致/)
  assert.match(readFileSync(path.join(root, 'package.json'), 'utf8'), /"version": "9\.9\.9"/)
})
