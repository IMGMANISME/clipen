#!/usr/bin/env node
// `npx clipen`: sets Clipen up on an Apple-silicon Mac with nothing installed beforehand.
//
// npx unpacks this package into a cache that may be cleared at any time, so the program is
// copied into Clipen's own folder first; the `clipen` command then runs from there. A Mac's own
// python3 is too old, so when no Python 3.10+ is found a fixed standalone build is downloaded and
// checked. Fonts are not in the package; they are downloaded from the sources pinned in
// fonts/manifest.json and checked against their SHA-256. Then the Python installer takes over.
'use strict'
const { spawnSync } = require('node:child_process')
const crypto = require('node:crypto')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const readline = require('node:readline')

const PACKAGE = path.resolve(__dirname, '..')
const VERSION = require(path.join(PACKAGE, 'package.json')).version
const PYTHON = {
  tag: '3.12.16+20261026',
  url: 'https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.12.16%2B20261026-aarch64-apple-darwin-install_only_stripped.tar.gz',
  sha256: '353d5f1b4fd2715e7053f12f068435d5bbad16b91e057006b5d01e0925709a7e',
  bytes: 25035343,
}
// What the program needs at run time; the rest of the package (this launcher) stays in npx's cache.
const PROGRAM = ['src', 'apps/web/dist', 'requirements', 'pyproject.toml', 'clipen', 'install', 'uninstall', 'README.md']
const FONTS = 'src/clipen/rendering/fonts'

function dataRoot() {
  return path.join(os.homedir(), 'Library', 'Application Support', 'Clipen')
}

function fail(message) {
  console.error(`Clipen 安裝失敗：${message}`)
  process.exit(1)
}

function sha256(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex')
}

async function ask(question) {
  if (!process.stdin.isTTY) fail('第一次設定需要在互動式 Terminal 中確認。')
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout })
  const answer = await new Promise((resolve) => rl.question(question, resolve))
  rl.close()
  return ['', 'y', 'yes'].includes(answer.trim().toLowerCase())
}

/** Download to a file and check it; nothing unchecked is ever kept. */
async function download(url, target, expected) {
  const response = await fetch(url, { redirect: 'follow' })
  if (!response.ok) fail(`無法下載 ${url}（HTTP ${response.status}）`)
  const partial = `${target}.part`
  fs.writeFileSync(partial, Buffer.from(await response.arrayBuffer()))
  if (sha256(partial) !== expected) {
    fs.rmSync(partial, { force: true })
    fail(`${path.basename(target)} 的檢查碼不符，已刪除下載的檔案；請稍後再試或回報問題。`)
  }
  fs.renameSync(partial, target)
}

/** A Python 3.10 or newer already here: one Clipen fetched before, Homebrew's, or one on PATH. */
function findPython() {
  const managed = path.join(dataRoot(), 'python', PYTHON.tag, 'bin', 'python3')
  const candidates = [managed]
  // CLIPEN_PYTHON_SEARCH (directories, colon-separated) narrows the search, e.g. to try a Mac with no Python.
  const dirs = process.env.CLIPEN_PYTHON_SEARCH?.split(':') ?? [...(process.env.PATH || '').split(':'), '/opt/homebrew/bin', '/usr/local/bin']
  for (const dir of dirs) {
    for (const name of ['python3.13', 'python3.12', 'python3.11', 'python3.10', 'python3']) candidates.push(path.join(dir, name))
  }
  for (const candidate of candidates) {
    if (!fs.existsSync(candidate)) continue
    const probe = spawnSync(candidate, ['-c', 'import sys; print("%d %d" % sys.version_info[:2])'], { encoding: 'utf8' })
    const [major, minor] = (probe.stdout || '').trim().split(' ').map(Number)
    if (probe.status === 0 && major === 3 && minor >= 10) return candidate
  }
  return null
}

async function standalonePython() {
  const root = path.join(dataRoot(), 'python')
  const target = path.join(root, PYTHON.tag)
  fs.mkdirSync(root, { recursive: true, mode: 0o700 })
  const archive = path.join(root, `cpython-${PYTHON.tag}.tar.gz`)
  console.log(`正在下載 Python ${PYTHON.tag.split('+')[0]}（約 ${Math.round(PYTHON.bytes / 1e6)} MB）…`)
  await download(PYTHON.url, archive, PYTHON.sha256)
  const staging = `${target}.staging-${process.pid}`
  fs.rmSync(staging, { recursive: true, force: true })
  fs.mkdirSync(staging)
  const unpacked = spawnSync('tar', ['-xzf', archive, '-C', staging, '--strip-components', '1'], { stdio: 'inherit' })
  fs.rmSync(archive, { force: true })
  if (unpacked.status !== 0) fail('Python 解壓縮失敗。')
  fs.renameSync(staging, target)
  console.log('✓ Python 準備完成')
  return path.join(target, 'bin', 'python3')
}

/** This version of the program, copied where it stays; fonts downloaded and checked into it. */
async function program() {
  const root = path.join(dataRoot(), 'app')
  const target = path.join(root, VERSION)
  if (fs.existsSync(path.join(target, '.clipen-npm'))) return target
  fs.mkdirSync(root, { recursive: true, mode: 0o700 })
  const staging = `${target}.staging-${process.pid}`
  fs.rmSync(staging, { recursive: true, force: true })
  for (const entry of PROGRAM) {
    const from = path.join(PACKAGE, entry)
    if (!fs.existsSync(from)) fail(`套件缺少 ${entry}。`)
    fs.cpSync(from, path.join(staging, entry), { recursive: true, filter: (file) => !file.includes('__pycache__') })
  }
  const fonts = path.join(staging, FONTS)
  const manifest = JSON.parse(fs.readFileSync(path.join(fonts, 'manifest.json'), 'utf8'))
  const missing = Object.values(manifest).filter((font) => !fs.existsSync(path.join(fonts, font.file)))
  if (missing.length) {
    console.log(`正在下載 Clipen 內附字型（${missing.length} 套，開源授權）…`)
    for (const font of missing) await download(font.download_url, path.join(fonts, font.file), font.sha256)
    console.log('✓ 字型準備完成')
  }
  fs.writeFileSync(path.join(staging, '.clipen-npm'), `${VERSION}\n`)
  fs.renameSync(staging, target)
  return target
}

function tools() {
  const has = (name) => spawnSync('/bin/sh', ['-c', `command -v ${name}`], { env: { ...process.env, PATH: `${process.env.PATH}:/opt/homebrew/bin:/usr/local/bin` } }).status === 0
  const missing = ['ffmpeg', 'ffprobe'].filter((name) => !has(name))
  if (!missing.length) return
  console.log('\n⚠ 找不到 ffmpeg：Clipen 輸出影片需要它。')
  console.log(has('brew') ? '  安裝方式：brew install ffmpeg' : '  先安裝 Homebrew（https://brew.sh），再執行：brew install ffmpeg')
  console.log('  安裝好之後再執行 clipen codex 或 clipen claude 即可。\n')
}

async function main() {
  if (process.platform !== 'darwin' || process.arch !== 'arm64') fail('目前只支援 Apple 晶片（M 系列）的 Mac。')
  const args = process.argv.slice(2)
  const command = args.length ? args : ['install']
  let python = findPython()
  const app = path.join(dataRoot(), 'app', VERSION)
  const needs = [
    ...(python ? [] : [`Python ${PYTHON.tag.split('+')[0]}（約 ${Math.round(PYTHON.bytes / 1e6)} MB，來自 python-build-standalone，下載後核對檢查碼）`]),
    ...(fs.existsSync(path.join(app, '.clipen-npm')) ? [] : [`Clipen ${VERSION} 程式（放在 ${app}）與內附字型（約 40 MB，開源授權，下載後核對檢查碼）`]),
  ]
  if (needs.length) {
    console.log('Clipen 會先準備：')
    for (const item of needs) console.log(`  • ${item}`)
    console.log('不會修改系統 Python，也不會使用 sudo。')
    if (!(await ask('繼續？[Y/n] '))) { console.log('已取消，沒有下載任何東西。'); return }
  }
  if (!python) python = await standalonePython()
  const target = await program()
  if (command[0] === 'install') tools()
  const run = spawnSync(python, [path.join(target, 'clipen'), ...command], { stdio: 'inherit' })
  process.exit(run.status ?? 1)
}

main().catch((error) => fail(error instanceof Error ? error.message : String(error)))
