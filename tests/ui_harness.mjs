// Small DOM/fetch fixture for deterministic UI state tests; not a browser substitute.
import fs from 'node:fs';
import vm from 'node:vm';
import {randomUUID} from 'node:crypto';

export function harness(initial, page = 'diagnostics') {
  const nodes = new Map(), calls = [], timers = new Map(), listeners = new Map();
  let timerId = 0, renders = 0, responder;
  class Element {
    constructor(tag = 'div') {
      this.tagName = tag; this.children = []; this.parent = null; this.dataset = {};
      this.attributes = new Map(); this.handlers = new Map(); this.value = ''; this.checked = false;
      this.disabled = false; this.hidden = false; this.inert = false; this._text = '';
      this.classList = {values: new Set(), toggle(name, enabled) { if (enabled) this.values.add(name); else this.values.delete(name); }};
    }
    set textContent(value) { this._text = String(value); this.children.forEach((child) => { child.parent = null; }); this.children = []; }
    get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
    set className(value) { this.classList.values = new Set(value.split(' ')); }
    get className() { return [...this.classList.values].join(' '); }
    append(...children) { children.forEach((child) => this.insertBefore(child, null)); }
    insertBefore(child, before) {
      if (child.tagName === 'fragment') { [...child.children].forEach((item) => this.insertBefore(item, before)); return; }
      child.remove(); const index = before ? this.children.indexOf(before) : this.children.length;
      this.children.splice(index, 0, child); child.parent = this; renders++;
    }
    replaceChildren(...children) { this.children.forEach((child) => { child.parent = null; }); this.children = []; this._text = ''; this.append(...children); }
    replaceWith(node) { const parent = this.parent; if (parent) { parent.insertBefore(node, this); this.remove(); } }
    remove() { if (this.parent) { const parent = this.parent; parent.children.splice(parent.children.indexOf(this), 1); this.parent = null; } }
    addEventListener(type, fn) { this.handlers.set(type, fn); }
    setAttribute(name, value) { this.attributes.set(name, value); }
    getAttribute(name) { return this.attributes.get(name); }
    removeAttribute(name) { this.attributes.delete(name); }
    focus() { document.activeElement = this; }
    querySelectorAll(selector) {
      const result = [];
      for (const child of this.children) {
        if (selector === 'button' && child.tagName === 'button') result.push(child);
        result.push(...child.querySelectorAll(selector));
      }
      return result;
    }
  }
  const root = new Element('root');
  const html = fs.readFileSync(new URL('../prototypes/local_ui/static/' + (page === 'workspace' ? 'index' : 'diagnostics') + '.html', import.meta.url), 'utf8');
  for (const match of html.matchAll(/<(\w+)[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const node = new Element(match[1]); node.id = match[2]; node.hidden = /\bhidden\b/.test(match[0]); nodes.set(node.id, node); root.append(node);
  }
  const nav = ['#main', '#plan', '#activity'].map((href) => { const el = new Element('a'); el.setAttribute('href', href); return el; });
  const document = {
    activeElement: null, hidden: false, title: '',
    getElementById: (id) => { if (!nodes.has(id)) throw Error(`Missing DOM ID ${id}`); return nodes.get(id); },
    createElement: (tag) => new Element(tag), createDocumentFragment: () => new Element('fragment'),
    querySelectorAll: (selector) => selector === '.nav-item' ? nav : root.querySelectorAll('button').filter((button) => button.dataset.action),
    addEventListener: (type, fn) => listeners.set(`document:${type}`, fn),
    contains: (node) => { while (node?.parent) node = node.parent; return node === root; },
  };
  const window = {location: {hash: ''}, confirm: () => false, addEventListener: (type, fn) => listeners.set(type, fn)};
  function response(status, data, etag = '"fixture"') {
    return {status, ok: status >= 200 && status < 300, headers: {get: (name) => name === 'ETag' ? etag : null}, json: async () => data};
  }
  responder = async (path) => response(200, initial);
  const context = vm.createContext({document, window, history: {replaceState() { window.location.hash = ''; }},
    crypto: {randomUUID}, TextEncoder, AbortController, Date, console,
    setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, {fn, delay}); return id; },
    clearTimeout: (id) => timers.delete(id),
    fetch: async (path, options) => { calls.push({path, options}); return responder(path, options); },
  });
  for (const name of (page === 'workspace' ? ['workspace-state.js', 'workspace.js'] : ['ui-state.js', 'app.js'])) vm.runInContext(fs.readFileSync(new URL(`../prototypes/local_ui/static/${name}`, import.meta.url), 'utf8'), context);
  return {nodes, calls, timers, listeners, document, context, response,
    evaluate: (code) => vm.runInContext(code, context),
    respond: (fn) => { responder = fn; },
    flush: async () => { for (let i = 0; i < 8; i++) await new Promise(setImmediate); },
    event: async (id, type) => nodes.get(id).handlers.get(type)?.({preventDefault() {}}),
    renders: () => renders,
  };
}
export function state(waiting = true) {
  return {connection: {transport_connected: true, waiting_for_work: waiting, binding_id: 'synthetic-binding', client: {name: 'synthetic-not-AI', version: '1'}}, jobs: [], events: [], csrf: 'fixture-csrf'};
}
