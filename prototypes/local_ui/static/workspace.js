'use strict';
const $ = (id) => document.getElementById(id);
const draftRules = globalThis.ClipenPreparation;
const fieldNames = Object.keys(draftRules.fields);
let baseline = null, latest = null, pending = null, csrf = '', etag = '';
let loaded = false, closed = false, saving = false, conflict = false;
let pollTimer = null, refreshInFlight = null;
const errors = {
  authentication_required: '開頁權限已失效。請使用目前 AI terminal 顯示的完整連結。',
  bootstrap_expired_or_used: '開頁連結已失效。請使用目前 AI terminal 顯示的新連結。',
  service_stopped: '本機服務已結束。請重新開啟 AI，並使用 terminal 顯示的新連結。',
  draft_version_conflict: '另一個分頁已更新草稿，你的輸入仍保留。',
  invalid_draft_source_directory: '毛片路徑過長或包含換行等無效字元，請檢查。',
  invalid_draft_reference_directory: '參考路徑過長或包含換行等無效字元，請檢查。',
  invalid_draft_brief: '剪輯需求過長或包含無效字元，請縮短後再儲存。',
};
function values() {
  return Object.fromEntries(fieldNames.map((name) => [name, $(draftRules.fields[name]).value]));
}
function dirty() { return loaded && !draftRules.same(values(), baseline); }
function showError(message = '') { $('error').textContent = message; $('error').hidden = !message; }
function hydrate(draft) {
  fieldNames.forEach((name) => { $(draftRules.fields[name]).value = draft[name]; });
  baseline = draft; conflict = false;
}
function controls() {
  const invalid = draftRules.invalid(values());
  for (const name of fieldNames) {
    const input = $(draftRules.fields[name]);
    input.disabled = !loaded || closed || saving || !!pending;
    input.setAttribute('aria-invalid', String(invalid === name));
  }
  $('save-draft').disabled = !loaded || closed || saving || !!pending || conflict || !dirty() || !!invalid;
  $('save-draft').hidden = !!pending;
  $('save-draft').textContent = saving ? '正在儲存…' : '儲存準備草稿';
  $('check-save').hidden = !pending;
  $('check-save').disabled = closed || saving;
  $('conflict').hidden = !conflict || closed || !!pending;
  $('save-mine').disabled = closed || saving || !!pending || !latest || !!invalid;
  $('load-latest').disabled = closed || saving || !!pending || !latest;
  $('draft-rescue').hidden = !(closed && (dirty() || pending || conflict));
  if (!$('draft-rescue').hidden) $('rescue-text').value = draftRules.display(values());
  if (conflict && latest) $('latest-draft').value = draftRules.display(latest);
  if (!loaded || closed) return;
  let status = baseline.version ? '已載入本機草稿' : '還沒有已保存的草稿';
  if (baseline.version && baseline.updated) status = '已保存於 ' + new Date(baseline.updated * 1000).toLocaleString('zh-TW');
  if (dirty()) status = '有尚未儲存的修改';
  if (conflict) status = '另一個分頁更新了草稿，請先比較內容';
  if (pending) status = '儲存結果尚未確認；請確認同一筆儲存';
  if (saving) status = '正在儲存到本機…';
  if ($('save-status').textContent !== status) $('save-status').textContent = status;
  $('save-hint').textContent = invalid ? errors['invalid_draft_' + invalid] : '只儲存在這台電腦，不會讀取素材或送給 AI。';
}
function closeInterface(message) {
  closed = true; csrf = ''; etag = '';
  clearTimeout(pollTimer);
  $('workspace').hidden = true; $('workspace').inert = true;
  $('closed-screen').hidden = false;
  $('local-status').textContent = '本機服務已中斷';
  $('closed-reason').textContent = message || '請重新檢查連線；若 AI terminal 已關閉，請重新開啟 AI 並使用新連結。';
  document.title = 'Clipen · 連線已中斷';
  controls();
  if (!document.hidden) $('closed-title').focus();
}
async function api(path, body) {
  const options = {cache:'no-store', credentials:'same-origin', headers:{}};
  if (body !== undefined) {
    options.method = 'POST';
    options.headers = {'Content-Type':'application/json', 'X-Clipen-CSRF':csrf};
    options.body = JSON.stringify(body);
  } else if (etag && loaded) options.headers['If-None-Match'] = etag;
  const controller = new AbortController();
  options.signal = controller.signal;
  const timer = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch(path, options);
    if (response.status === 304) return null;
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(errors[data.error] || '操作未完成，請保留內容後重試。');
      error.code = data.error;
      error.status = response.status;
      error.definitive = response.status >= 400 && response.status < 500;
      throw error;
    }
    if (body === undefined) etag = response.headers.get('ETag') || '';
    return data;
  } finally { clearTimeout(timer); }
}
function receive(next) {
  if (!next) return;
  csrf = next.csrf;
  const draft = next.draft;
  // An older GET can finish after a save. It must not roll back a newer receipt.
  if (draft.version < Math.max(baseline?.version || 0, latest?.version || 0)) return;
  latest = draft;
  if (!loaded) { hydrate(draft); loaded = true; }
  else if (!pending && draft.version !== baseline.version) {
    if (dirty() || conflict) conflict = true;
    else hydrate(draft);
  } else if (!pending && draft.version === baseline.version) {
    baseline = draft;
  }
  controls();
}
async function refresh() {
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = (async () => { receive(await api('/api/preparation')); })();
  try { await refreshInFlight; } finally { refreshInFlight = null; }
}
async function save(retry = false) {
  if (closed || saving || !loaded || (pending && !retry) || (retry && !pending)) return;
  if (!retry) {
    if (conflict || !dirty() || draftRules.invalid(values())) return;
    pending = Object.freeze({...values(), request_id:crypto.randomUUID(), expected_version:baseline.version});
  }
  const submitted = pending;
  saving = true; showError(); controls();
  try {
    const receipt = await api('/api/preparation', submitted);
    baseline = {...submitted, version:receipt.draft_version, updated:null};
    pending = null;
    conflict = !!latest && latest.version > receipt.draft_version && !draftRules.same(values(), latest);
    // Do not clear fields: even newer local input must survive an acknowledgement.
  } catch (error) {
    if (error.definitive) pending = null;
    if (error.code === 'draft_version_conflict') conflict = true;
    showError(error.definitive ? error.message : '儲存回應未確認。你的內容仍保留，請確認儲存結果；不會建立 AI 工作。');
    if (error.status === 401 || error.code === 'service_stopped') closeInterface(error.message);
  }
  saving = false;
  if (!closed) {
    // Drain a GET that may have started before the write, then read current state.
    try { if (refreshInFlight) await refreshInFlight; await refresh(); }
    catch (error) { closeInterface(error.status ? error.message : undefined); }
  }
  controls();
}
function schedulePoll() {
  clearTimeout(pollTimer);
  if (!closed) pollTimer = setTimeout(poll, draftRules.pollDelay(document.hidden));
}
async function poll() {
  if (closed) return;
  try { await refresh(); } catch (error) { closeInterface(error.status ? error.message : undefined); }
  schedulePoll();
}
async function start() {
  const token = window.location.hash.slice(1);
  history.replaceState(null, '', '/');
  try {
    if (token) {
      try { await api('/api/session', {token}); }
      catch (error) { await refresh().catch(() => { throw error; }); }
    }
    await refresh();
    $('local-status').textContent = '本機草稿已就緒';
    controls();
  } catch (error) { closeInterface(error.status ? error.message : undefined); }
  schedulePoll();
}
fieldNames.forEach((name) => $(draftRules.fields[name]).addEventListener('input', () => { showError(); controls(); }));
$('preparation-form').addEventListener('submit', (event) => { event.preventDefault(); void save(); });
$('check-save').addEventListener('click', () => save(true));
$('load-latest').addEventListener('click', () => {
  if (closed || saving || pending || !latest) return;
  if (dirty() && !window.confirm('改用本機已保存版本？目前分頁尚未儲存的修改會被取代。')) return;
  hydrate(latest); showError(); controls(); $('brief').focus();
});
$('save-mine').addEventListener('click', () => {
  if (closed || saving || pending || !latest || draftRules.invalid(values())) return;
  // The user explicitly chooses their content after seeing the latest version.
  baseline = latest; conflict = false; controls(); void save();
});
$('reconnect').addEventListener('click', async () => {
  $('reconnect').disabled = true; etag = '';
  try {
    if (refreshInFlight) await refreshInFlight.catch(() => {});
    await refresh();
    closed = false; $('closed-screen').hidden = true;
    $('workspace').hidden = false; $('workspace').inert = false;
    $('local-status').textContent = '本機草稿已就緒';
    document.title = 'Clipen · 準備作品';
    controls(); $('brief').focus(); schedulePoll();
  } catch (error) { $('closed-reason').textContent = error.status ? error.message : '仍無法連接本次服務。若 AI terminal 已關閉，請重新開啟 AI 並使用新連結。'; }
  finally { $('reconnect').disabled = false; }
});
window.addEventListener('beforeunload', (event) => {
  if (dirty() || pending || conflict) { event.preventDefault(); event.returnValue = ''; }
});
document.addEventListener('visibilitychange', () => {
  clearTimeout(pollTimer);
  if (closed) return;
  if (document.hidden) schedulePoll(); else void poll();
});
void start();
