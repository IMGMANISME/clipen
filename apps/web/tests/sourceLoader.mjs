import { existsSync, readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import ts from 'typescript'

const require = createRequire(import.meta.url)
export function createSourceLoader(overrides = {}) {
  const cache = new Map()
  function load(name) {
    const base = resolve(import.meta.dirname, '../src', name)
    const file = /\.tsx?$/.test(base) ? base : base + (existsSync(base + '.tsx') ? '.tsx' : '.ts')
    if (cache.has(file)) return cache.get(file)
    const module = { exports: {} }
    cache.set(file, module.exports)
    const { outputText } = ts.transpileModule(readFileSync(file, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
    })
    const localRequire = (specifier) => Object.hasOwn(overrides, specifier) ? overrides[specifier]
      : specifier.startsWith('.') ? load(resolve(dirname(file), specifier)) : require(specifier)
    new Function('require', 'module', 'exports', outputText)(localRequire, module, module.exports)
    return module.exports
  }
  return load
}
