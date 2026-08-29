'use strict';
const $ = (id) => document.getElementById(id);
const draftRules = globalThis.ClipenPreparation;
const fieldNames = Object.keys(draftRules.fields);
let baseline = null, latest = null, pending = null, csrf = '', etag = '';
let loaded = false, closed = false, saving = false, conflict = false;
let pollTimer = null, refreshInFlight = null;
let material = null, picker = null, capabilities = {}, handledPick = null;
let importStarting = false, pendingImport = null, cancelStarting = false;
let requestedPick = null;
const errors = {
  authentication_required: '開頁權限已失效。請使用目前 AI terminal 顯示的完整連結。',
  bootstrap_expired_or_used: '開頁連結已失效。請使用目前 AI terminal 顯示的新連結。',
  service_stopped: '本機服務已結束。請重新開啟 AI，並使用 terminal 顯示的新連結。',
  draft_version_conflict: '另一個分頁已更新草稿，你的輸入仍保留。',
  invalid_draft_source_directory: '毛片路徑過長或包含換行等無效字元，請檢查。',
  invalid_draft_reference_directory: '參考路徑過長或包含換行等無效字元，請檢查。',
  invalid_draft_brief: '剪輯需求過長或包含無效字元，請縮短後再儲存。',
  material_directory_must_be_absolute: '毛片目錄必須是完整的絕對路徑。',
  invalid_material_directory: '目錄路徑包含無效字元，請重新選擇。',
  material_directory_unavailable: '目錄不存在、無法讀取，或是符號連結。請重新選擇。',
  material_read_consent_required: '請從確認畫面開始讀取素材。',
  material_import_already_running: '已有一個素材檢查正在進行。',
  directory_picker_unavailable: '這個環境無法開啟本機目錄選擇器，仍可貼上完整路徑。',
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
  const picking = picker?.status === 'picking';
  $('pick-source').disabled = !loaded || closed || saving || !!pending || picking || !capabilities.directory_picker;
  $('pick-reference').disabled = $('pick-source').disabled;
  $('pick-source').textContent = picking && picker.role === 'source' ? '選擇中…' : '選擇目錄';
  $('pick-reference').textContent = picking && picker.role === 'reference' ? '選擇中…' : '選擇目錄';
  const activeImport = ['running','cancelling'].includes(material?.status);
  $('review-materials').disabled = !loaded || closed || saving || !!pending || conflict || dirty() ||
    !!invalid || !values().source_directory || activeImport || importStarting;
  $('review-materials').textContent = material && material.status !== 'idle' ? '重新檢查素材' : '檢查素材';
  $('confirm-read').disabled = closed || importStarting || activeImport;
  $('cancel-read').disabled = importStarting;
  $('cancel-import').disabled = closed || cancelStarting || material?.status === 'cancelling';
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
  renderMaterial();
}

const issueText = Object.freeze({unsupported_format:'尚未支援此格式', symlink_skipped:'已略過符號連結',
  unreadable:'無法讀取', directory_unreadable:'無法讀取子目錄', directory_depth_limit:'超過子目錄深度限制',
  metadata_unreadable:'無法讀取媒體資訊', metadata_too_large:'媒體資訊異常', probe_timed_out:'讀取媒體資訊逾時',
  probe_unavailable:'這台電腦找不到 FFprobe', not_regular_file:'不是一般檔案',
  material_file_limit:'超過 500 個媒體項目，請縮小目錄範圍',
  material_node_limit:'目錄內容過多，請縮小目錄範圍',
  material_directory_unavailable:'已保存的目錄目前無法讀取，請重新連接磁碟或選擇目錄',
  material_scan_failed:'本機素材檢查發生錯誤', service_interrupted:'服務中斷，未自動重跑'});
function durationText(seconds) {
  const rounded = Math.round(seconds || 0), minutes = Math.floor(rounded / 60);
  return minutes ? `${minutes} 分 ${rounded % 60} 秒` : `${rounded} 秒`;
}
function renderMaterial() {
  if (!material) return;
  const active = ['running','cancelling'].includes(material.status);
  $('material-progress').hidden = !active;
  $('material-results').hidden = material.status === 'idle' || active;
  if (active) {
    $('read-confirmation').hidden = true;
    $('material-progress-title').textContent = material.status === 'cancelling' ? '正在停止素材檢查…' : '正在檢查素材…';
    $('material-progress-detail').textContent = material.processed ? `已記錄 ${material.processed} 個素材或問題。` : '正在巡覽已確認的本機目錄。';
  }
  if ($('material-results').hidden) return;
  const entries = material.entries || [], usable = entries.filter(item => item.kind === 'media' || item.kind === 'image');
  const issues = entries.filter(item => item.kind === 'issue');
  let summary = material.status === 'cancelled' ? '素材檢查已取消' : material.status === 'interrupted' ? '上次素材檢查未完成' : material.status === 'failed' ? '素材檢查未完成' : `找到 ${usable.length} 個可用素材`;
  $('material-summary').textContent = summary;
  const changed = baseline && (material.roots.source !== baseline.source_directory || material.roots.reference !== baseline.reference_directory);
  let note = usable.length ? `影片與音訊合計 ${durationText(material.duration)}；另有 ${issues.length} 個需要處理的項目。` : '目前沒有可用的支援素材。';
  if (changed) note += ' 目前草稿的目錄已改變，請重新檢查。';
  if (material.issue) note += ' ' + (issueText[material.issue] || '請檢查目錄後重試。');
  $('material-result-note').textContent = note;
  $('material-files').replaceChildren(...entries.slice(0, 80).map((item) => {
    const row = document.createElement('li'), path = document.createElement('span'), meta = document.createElement('span');
    path.className = 'file-path'; path.textContent = `${item.role === 'reference' ? '參考' : '毛片'} · ${item.path}`;
    meta.className = 'file-meta';
    meta.textContent = item.kind === 'issue' ? (issueText[item.issue] || '需要處理') : item.kind === 'image' ? '圖片' : durationText(item.duration);
    row.append(path, meta); return row;
  }));
  if (entries.length > 80) {
    const more = document.createElement('li'); more.textContent = `另有 ${entries.length - 80} 個項目保存在本機清單中。`; $('material-files').append(more);
  }
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
  capabilities = next.capabilities || {};
  material = next.material_import || material;
  picker = next.directory_picker || picker;
  if (picker?.status === 'chosen' && picker.id === requestedPick && picker.id !== handledPick) {
    handledPick = picker.id;
    const field = picker.role === 'source' ? 'source-directory' : 'reference-directory';
    $(field).value = picker.path;
    showError();
    requestedPick = null;
  } else if (picker?.status === 'cancelled' && picker.id === requestedPick) {
    handledPick = picker.id; requestedPick = null;
  } else if (picker?.status === 'failed' && picker.id === requestedPick) {
    handledPick = picker.id; requestedPick = null;
    showError('目錄選擇器未能完成。你仍可貼上完整路徑。');
  }
  if (pendingImport && material?.draft_version === pendingImport.expected_draft_version && material.status !== 'idle') {
    pendingImport = null;
    $('read-confirmation').hidden = true;
  }
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
async function chooseDirectory(role) {
  if (closed || picker?.status === 'picking') return;
  showError();
  try { const result = await api('/api/directory-picker', {role}); requestedPick = result.pick_id; await refresh(); }
  catch (error) { showError(error.message); }
  controls();
}
async function beginImport() {
  if (closed || importStarting || !baseline?.version) return;
  pendingImport ||= {request_id:crypto.randomUUID(), expected_draft_version:baseline.version, consent:true};
  importStarting = true; showError(); controls();
  try {
    await api('/api/material-import', pendingImport);
    pendingImport = null; $('read-confirmation').hidden = true;
  } catch (error) {
    if (error.definitive) pendingImport = null;
    showError(error.definitive ? error.message : '讀取指令的結果尚未確認。正在核對本機狀態，不會自動建立另一個檢查。');
  }
  importStarting = false;
  try { await refresh(); } catch (error) { closeInterface(error.status ? error.message : undefined); }
  controls();
}
async function cancelImport() {
  if (!material?.import_id || cancelStarting) return;
  cancelStarting = true; controls();
  try { await api('/api/material-import/cancel', {request_id:crypto.randomUUID(), import_id:material.import_id}); await refresh(); }
  catch (error) { showError(error.message); }
  cancelStarting = false; controls();
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
$('pick-source').addEventListener('click', () => void chooseDirectory('source'));
$('pick-reference').addEventListener('click', () => void chooseDirectory('reference'));
$('review-materials').addEventListener('click', () => {
  if ($('review-materials').disabled) return;
  $('confirm-source').textContent = baseline.source_directory;
  $('confirm-reference').textContent = baseline.reference_directory || '未提供';
  $('read-confirmation').hidden = false; $('confirm-read').focus();
});
$('cancel-read').addEventListener('click', () => { $('read-confirmation').hidden = true; $('review-materials').focus(); });
$('confirm-read').addEventListener('click', () => void beginImport());
$('cancel-import').addEventListener('click', () => void cancelImport());
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
