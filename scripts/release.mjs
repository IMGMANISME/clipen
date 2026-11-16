#!/usr/bin/env node
// `npm run release -- <patch|minor|major|x.y.z>`: gets a new version ready to publish.
// It writes the version everywhere Clipen states it, rebuilds the web app the package ships,
// runs the tests and a dry-run publish. Publishing itself stays a separate `npm publish`,
// which npm asks you to approve in the browser.
import { spawnSync } from 'node:child_process'
import { readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const PACKAGE = '@clipen/clipen-cli'

// Every place the version is written, and how to find it there.
export const PLACES = [
  ['package.json', /^(\s*"version": ")([^"]+)(")/m],
  ['apps/web/package.json', /^(\s*"version": ")([^"]+)(")/m],
  ['pyproject.toml', /^(version = ")([^"]+)(")/m],
  ['src/clipen/__init__.py', /^(__version__ = ")([^"]+)(")/m],
]

/** The version after `bump` (patch, minor, major or an explicit x.y.z) is applied to `current`. */
export function next(current, bump) {
  const parts = current.split('.').map(Number)
  if (parts.length !== 3 || parts.some((n) => !Number.isInteger(n) || n < 0)) throw new Error(`目前的版本號看不懂：${current}`)
  const [major, minor, patch] = parts
  if (bump === 'patch') return `${major}.${minor}.${patch + 1}`
  if (bump === 'minor') return `${major}.${minor + 1}.0`
  if (bump === 'major') return `${major + 1}.0.0`
  if (/^\d+\.\d+\.\d+$/.test(bump ?? '')) {
    const wanted = bump.split('.').map(Number)
    const first = wanted.findIndex((n, i) => n !== parts[i])
    if (first < 0 || wanted[first] < parts[first]) throw new Error(`新版本 ${bump} 要比目前的 ${current} 新。`)
    return bump
  }
  throw new Error('用法：npm run release -- patch｜minor｜major｜x.y.z')
}

/** Writes `version` into every place; refuses if any place disagrees with the others first. */
export function write(root, version) {
  const found = PLACES.map(([file, pattern]) => {
    const text = readFileSync(path.join(root, file), 'utf8')
    const match = text.match(pattern)
    if (!match) throw new Error(`在 ${file} 找不到版本號。`)
    return { file, text, pattern, current: match[2] }
  })
  const versions = [...new Set(found.map((place) => place.current))]
  if (versions.length !== 1) throw new Error(`版本號不一致：${found.map((p) => `${p.file}=${p.current}`).join('、')}`)
  for (const place of found) writeFileSync(path.join(root, place.file), place.text.replace(place.pattern, `$1${version}$3`))
  return versions[0]
}

function run(label, command, args, cwd = ROOT) {
  console.log(`\n▶ ${label}`)
  const result = spawnSync(command, args, { cwd, stdio: 'inherit', env: { ...process.env, PYTHONPATH: 'src:tests/python' } })
  if (result.status !== 0) throw new Error(`${label}沒有通過。版本號已經改了，修好後再跑一次 npm run release，或用 git checkout . 還原。`)
}

function main() {
  const bump = process.argv[2]
  const dirty = spawnSync('git', ['status', '--porcelain'], { cwd: ROOT, encoding: 'utf8' }).stdout.trim()
  if (dirty) throw new Error('還有沒提交的修改，先提交或還原再發布。')
  const current = JSON.parse(readFileSync(path.join(ROOT, 'package.json'), 'utf8')).version
  const version = next(current, bump)
  const published = spawnSync('npm', ['view', `${PACKAGE}@${version}`, 'version'], { cwd: ROOT, encoding: 'utf8' }).stdout.trim()
  if (published) throw new Error(`${PACKAGE}@${version} 已經發布過，同一個版本號不能再用。`)
  write(ROOT, version)
  console.log(`版本號：${current} → ${version}（${PLACES.map(([file]) => file).join('、')}）`)
  run('重建前端', 'npm', ['run', 'build'], path.join(ROOT, 'apps/web'))
  run('前端測試', 'npm', ['test'], path.join(ROOT, 'apps/web'))
  run('套件測試', 'npm', ['test'])
  run('Python 測試', '.venv/bin/python', ['-m', 'unittest', 'discover', '-s', 'tests/python', '-q'])
  run('發布試跑', 'npm', ['publish', '--dry-run'])
  console.log(`\n✓ ${version} 準備好了。接下來：
  1. 提交版本號：git commit -am "chore: release ${version}"，合併進 main
  2. 發布：npm publish（在瀏覽器批准；若進入待審核，到 npmjs.com 的 Staged Packages 按 Approve）
  3. 使用者更新：npx ${PACKAGE}@latest`)
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { main() } catch (error) { console.error(`✗ ${error.message}`); process.exit(1) }
}
