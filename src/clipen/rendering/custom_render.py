"""Draw frames of an Agent-designed web page in a sealed headless Chrome.

Runs as its own process (``python -m clipen.rendering.custom_render``) so a page
that hangs or crashes can be killed without touching Clipen. It reads one JSON
job on stdin and writes one transparent PNG per requested frame. The page gets
no network: every request is refused except Clipen's own bundled fonts, served
from ``https://clipen.local/fonts/``. Time is Clipen's to set, so the same frame
always draws the same picture.
"""

import json
import os
import sys
from importlib.resources import files
from pathlib import Path

FONT_ORIGIN = "https://clipen.local/fonts/"
FONTS = {
    entry["file"]
    for entry in json.loads(
        files("clipen.rendering").joinpath("fonts", "manifest.json").read_text(encoding="utf-8")
    ).values()
}

# Runs before the page's own scripts: a fixed clock and random sequence.
_PRELUDE = """
(() => {
  let seed = 1, now = 0;
  Math.random = () => { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; };
  performance.now = () => now;
  const epoch = 1767225600000;
  Date.now = () => epoch + now;
  window.__clipenTime = (ms) => { now = ms; };
  window.clipen = Object.freeze(__INFO__);
  // A person's changes to the words and colours the page marked as editable. Words are set as the
  // parser adds each marked element, before the page's own scripts read them; colours win over :root.
  const edits = __EDITS__;
  const texts = edits.texts || {}, colors = edits.colors || {};
  const word = (node) => {
    if (node.nodeType !== 1) return;
    const apply = (el) => { const key = el.getAttribute('data-clipen-text'); if (key in texts && !el.__clipenSet) { el.textContent = texts[key]; el.__clipenSet = true; } };
    if (node.hasAttribute && node.hasAttribute('data-clipen-text')) apply(node);
    node.querySelectorAll && node.querySelectorAll('[data-clipen-text]').forEach(apply);
  };
  if (Object.keys(texts).length) {
    new MutationObserver((records) => records.forEach((r) => r.addedNodes.forEach(word))).observe(document, { childList: true, subtree: true });
  }
  const paint = () => document.documentElement && Object.entries(colors).forEach(([key, value]) => document.documentElement.style.setProperty('--clipen-' + key, value));
  // The page is the picture: no default margins, though the page may set its own. The look it uses
  // (shared fonts and colours as --look-* variables) comes first, so the page's own rules build on it.
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-clipen-text]').forEach(word);
    paint();
    const base = document.createElement('style');
    base.textContent = ':where(html, body) { margin: 0; padding: 0; background: transparent; }\\n' + __LOOK__;
    document.head.prepend(base);
  });
})();
"""

# What the page marked as editable: words in [data-clipen-text] elements, colours in --clipen-* variables.
_FIELDS = """
() => {
  const probe = document.createElement('i');
  document.body.appendChild(probe);
  const hex = (value) => {
    probe.style.color = '';
    probe.style.color = value.trim();
    if (!probe.style.color) return null;
    const m = getComputedStyle(probe).color.match(/rgba?\\(([^)]+)\\)/);
    if (!m) return null;
    const [r, g, b] = m[1].split(',').map((v) => Math.round(parseFloat(v)));
    return '#' + [r, g, b].map((v) => v.toString(16).padStart(2, '0')).join('');
  };
  const texts = [], seen = new Set();
  document.querySelectorAll('[data-clipen-text]').forEach((el) => {
    const key = el.getAttribute('data-clipen-text');
    if (!key || seen.has(key)) return;
    seen.add(key);
    texts.push({ key, label: el.getAttribute('data-clipen-label') || key, value: el.textContent.trim() });
  });
  const colors = [], names = new Set();
  for (const sheet of document.styleSheets) {
    let rules;
    try { rules = sheet.cssRules; } catch { continue; }
    for (const rule of rules) {
      if (!rule.style) continue;
      for (const name of rule.style) if (name.startsWith('--clipen-')) names.add(name);
    }
  }
  const root = getComputedStyle(document.documentElement);
  names.forEach((name) => { const value = hex(root.getPropertyValue(name)); if (value) colors.push({ key: name.slice(9), label: name.slice(9), value }); });
  probe.remove();
  // The typeface each piece of visible text asks for first (fallbacks for missing glyphs are not counted).
  const fonts = new Set();
  document.querySelectorAll('body *').forEach((el) => {
    if (!Array.from(el.childNodes).some((n) => n.nodeType === 3 && n.textContent.trim())) return;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return;
    fonts.add(style.fontFamily.split(',')[0].trim().replace(/^["']|["']$/g, ''));
  });
  return { texts, colors, fonts: Array.from(fonts) };
}
"""

_SEEK = """
async ([frame, fps]) => {
  const ms = frame / fps * 1000;
  window.__clipenTime(ms);
  const hold = () => document.getAnimations().forEach((a) => { a.pause(); a.currentTime = ms; });
  hold();
  if (typeof window.clipenFrame === 'function') await window.clipenFrame(frame, ms / 1000);
  hold();
  await document.fonts.ready;
  await new Promise((done) => requestAnimationFrame(() => done()));
}
"""


def fail(code, detail=""):
    sys.stderr.write(json.dumps({"code": code, "detail": detail[:400]}, ensure_ascii=False))
    sys.exit(2)


def _browser(playwright):
    errors = []
    for options in ({"channel": "chrome"}, {}):
        try:
            return playwright.chromium.launch(headless=True, args=["--disable-gpu", "--no-first-run"], **options)
        except Exception as error:  # Chrome may be missing; try the bundled build next.
            errors.append(str(error).splitlines()[0])
    fail("custom_component_renderer_missing", " / ".join(errors))


PAGE = "https://clipen.local/component.html"


def _route(route, html):
    url = route.request.url
    if url == PAGE:
        return route.fulfill(status=200, body=html, headers={"content-type": "text/html; charset=utf-8"})
    if url.startswith(FONT_ORIGIN) and url[len(FONT_ORIGIN) :] in FONTS:
        path = Path(str(files("clipen.rendering").joinpath("fonts", url[len(FONT_ORIGIN) :])))
        return route.fulfill(
            status=200, body=path.read_bytes(), headers={"content-type": "font/ttf", "access-control-allow-origin": "*"}
        )
    return route.abort()


def render(job):
    from playwright.sync_api import Error, TimeoutError, sync_playwright

    out = Path(job["out"])
    width, height, fps = int(job["width"]), int(job["height"]), float(job["fps"])
    info = {"width": width, "height": height, "fps": fps, "durationFrames": int(job["duration"])}
    with sync_playwright() as playwright:
        browser = _browser(playwright)
        try:
            # A fresh context is a fresh, throw-away profile: nothing of the user's Chrome is visible.
            context = browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=1,
                java_script_enabled=True,
                offline=False,
                service_workers="block",
            )
            context.route("**/*", lambda route: _route(route, job["html"]))
            page = context.new_page()
            page.set_default_timeout(float(job.get("frame_timeout", 10)) * 1000)
            problems = []
            page.on("pageerror", lambda error: problems.append(str(error)))
            page.add_init_script(
                _PRELUDE.replace("__INFO__", json.dumps(info))
                .replace("__EDITS__", json.dumps(job.get("edits") or {}, ensure_ascii=False))
                .replace("__LOOK__", json.dumps(job.get("look") or "", ensure_ascii=False))
            )
            try:
                page.goto(PAGE, wait_until="load")
                page.evaluate("document.fonts.ready.then(() => true)")
                # A picture the sealed page could not fetch is hidden instead of drawn as a broken icon.
                page.evaluate(
                    "document.querySelectorAll('img').forEach((i) => { if (!i.complete || !i.naturalWidth) i.style.visibility = 'hidden' })"
                )
            except TimeoutError:
                fail("custom_component_load_timeout")
            except Error as error:
                fail("custom_component_load_failed", str(error))
            if job.get("describe"):
                (out / "fields.json").write_text(
                    json.dumps(page.evaluate(_FIELDS), ensure_ascii=False), encoding="utf-8"
                )
            for frame in job["frames"]:
                try:
                    page.evaluate(_SEEK, [int(frame), fps])
                    if problems:
                        fail("custom_component_script_error", problems[0])
                    data = page.screenshot(omit_background=True, type="png", animations="allow")
                except TimeoutError:
                    fail("custom_component_frame_timeout", f"frame {frame}")
                except Error as error:
                    fail("custom_component_script_error", problems[0] if problems else str(error))
                target = out / f"f{int(frame):05d}.png"
                temporary = target.with_suffix(".part")
                temporary.write_bytes(data)
                os.replace(temporary, target)
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        render(json.load(sys.stdin))
    except SystemExit:
        raise
    except ImportError as error:
        fail("custom_component_renderer_missing", str(error))
    except Exception as error:
        fail("custom_component_render_failed", f"{type(error).__name__}: {error}")
