'use strict';
const $ = (id) => document.getElementById(id);
const rules = globalThis.ClipenUI;
const labels = {queued: '等待 AI 取件', delivered: '已送交 AI', completed: '已收到回覆', uncertain: '中斷・待核對', cancelled: '已停止收件'};
const eventLabels = {...labels, recovery_required: '啟動後待核對', connection_lost: '連線中斷・待核對'};
const errors = {
  authentication_required: '本次開頁權限已失效，請使用目前 terminal 顯示的完整連結。',
  bootstrap_expired_or_used: '開頁連結已失效，請使用目前 terminal 顯示的完整連結。',
  agent_not_connected: '尚未連接 AI，草稿尚未送出。',
  explicit_current_agent_consent_required: 'AI 連線已改變，請重新確認資料送出授權。',
  version_conflict: '工作狀態剛剛改變，請核對更新後的狀態再操作。',
  request_id_conflict: '請求識別衝突，未建立重複工作。',
  prototype_job_limit: '原型已達 100 個工作上限。既有紀錄仍保留，請勿刪除資料庫來解除限制。',
  invalid_text: '請輸入有效文字，最多 1,000 字。',
  service_stopped: '本機服務已停止。請重新開啟 AI 並使用新的連結。',
};
let snapshot = null, csrf = '', etag = '', busy = false, pending = null, connected = false, closed = false;
let refreshInFlight = null, pollTimer = null, renderedEvents = '', visibleJobs = 20;
const cards = new Map();

function element(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}
function announce(message) { $('announcement').textContent = message; }
function showError(message = '') { $('error').textContent = message; $('error').hidden = !message; }
function closeInterface(message) {
  if (closed) return;
  closed = true; connected = false; csrf = ''; etag = '';
  clearTimeout(pollTimer);
  $('workspace').inert = true;
  $('workspace').hidden = true;
  $('closed-screen').hidden = false;
  $('closed-reason').textContent = message || 'AI terminal 已結束，或本機服務連線中斷。';
  $('closed-draft').value = $('prompt').value;
  $('draft-rescue').hidden = !$('prompt').value;
  document.title = 'Clipen · 介面已停用';
  controls();
  if (!document.hidden) $('closed-title').focus();
}
async function api(path, body) {
  const options = {cache: 'no-store', credentials: 'same-origin', headers: {}};
  if (body !== undefined) {
    options.method = 'POST';
    options.headers = {'Content-Type': 'application/json', 'X-Clipen-CSRF': csrf};
    options.body = JSON.stringify(body);
  } else if (path === '/api/state' && snapshot && etag) {
    options.headers['If-None-Match'] = etag;
  }
  const controller = new AbortController();
  options.signal = controller.signal;
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch(path, options);
    if (response.status === 304) return null;
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(errors[data.error] || `操作未完成：${data.error || response.status}`);
      // A server/network failure may follow a durable write. Keep the same
      // request ID until its receipt can be reconciled explicitly.
      error.definitive = response.status >= 400 && response.status < 500;
      error.status = response.status;
      throw error;
    }
    if (path === '/api/state') etag = response.headers.get('ETag') || '';
    return data;
  } finally { clearTimeout(timeout); }
}
function controls() {
  const input = rules.input($('prompt').value);
  const available = connected && !closed && rules.ready(snapshot?.connection);
  $('send').disabled = !available || busy || !!pending || !input.valid || !$('consent').checked;
  $('send').textContent = busy ? '正在確認…' : '送給 AI ↗';
  $('retry-request').hidden = closed || !pending;
  $('retry-request').disabled = closed || !connected || busy;
  $('prompt').disabled = closed || busy;
  $('consent').disabled = closed || busy;
  $('count').textContent = `${input.count} / 1000 字`;
  $('count').classList.toggle('invalid', input.count > 1000);
  $('prompt').setAttribute('aria-invalid', String(input.count > 1000));
  document.querySelectorAll('.job-actions button').forEach((button) => {
    button.disabled = busy || !!pending || !connected || closed || (button.dataset.action === 'retry' && !available);
  });
  if (closed) return;
  let hint = '請在專案目錄開啟 Codex 或 Claude 並允許連接。';
  if (connected && snapshot?.connection.transport_connected) {
    hint = available ? 'AI 工具正在等待工作。' : '已連線，但 AI 尚未等待接件；目前不會自動喚醒。';
    if (available && !input.valid) hint = input.count > 1000 ? '文字超過 1,000 字，請縮短後再送出。' : '請先輸入測試需求。';
    else if (available && !$('consent').checked) hint = '送出前請確認文字與服務費用授權。';
  }
  if (pending) hint = '上一筆回應未確認；請先核對同一筆請求，不要建立新工作。';
  $('send-hint').textContent = hint;
}
async function refresh(force = false) {
  if (refreshInFlight) {
    await refreshInFlight;
    if (!force) return;
  }
  if (closed) return;
  refreshInFlight = (async () => {
    const next = await api('/api/state');
    if (closed) return;
    connected = true;
    if (next) {
      if (snapshot && snapshot.connection.binding_id !== next.connection.binding_id) $('consent').checked = false;
      snapshot = next; csrf = next.csrf; render();
    } else controls();
  })();
  try { await refreshInFlight; } finally { refreshInFlight = null; }
}
function acknowledge(request) {
  if (request.path === '/api/jobs') {
    if ($('prompt').value === request.body.prompt) $('prompt').value = '';
    $('consent').checked = false;
    announce('工作已保存，等待 AI 回覆。');
  } else announce('操作已確認，工作狀態已更新。');
}
async function mutate(path, body, retry = false) {
  if (closed || busy || (pending && !retry) || (retry && !pending)) return;
  if (!retry) pending = {path, body: Object.freeze({...body, request_id: crypto.randomUUID()})};
  const request = pending;
  busy = true; showError(); controls();
  try {
    await api(request.path, request.body);
    pending = null;
    acknowledge(request);
  } catch (error) {
    if (error.definitive) pending = null;
    showError(error.definitive ? error.message : '回應未確認，工作可能已受理。請用上方按鈕核對同一筆請求；不會自動重送或建立新工作。');
  }
  try { await refresh(true); } catch { closeInterface(); }
  busy = false; controls();
}
function actionButton(job, action, label, callback) {
  const button = element('button', label, 'secondary');
  button.type = 'button'; button.dataset.job = job.id; button.dataset.action = action;
  button.addEventListener('click', callback);
  return button;
}
function jobCard(job) {
  const card = element('article', undefined, 'panel job');
  card.dataset.job = job.id;
  const head = element('div', undefined, 'job-head');
  const time = element('time', new Date(job.created * 1000).toLocaleString('zh-TW'), 'job-id');
  time.dateTime = new Date(job.created * 1000).toISOString();
  head.append(time);
  const status = element('span', labels[job.status] || job.status, 'job-status');
  status.dataset.status = job.status; head.append(status); card.append(head);
  card.append(element('pre', job.prompt));
  if (job.result !== null) card.append(element('pre', job.result, 'answer'));
  if (['queued', 'delivered', 'uncertain'].includes(job.status)) {
    const actions = element('div', undefined, 'job-actions');
    actions.append(actionButton(job, 'cancel', '停止收件', () => mutate(`/api/jobs/${job.id}/cancel`, {expected_job_version: job.version})));
    if (job.status === 'uncertain') {
      actions.append(actionButton(job, 'retry', '核對後重新送出', () => {
        if (!rules.ready(snapshot?.connection)) { showError('請先讓 AI 等待接件。'); return; }
        if (window.confirm('請先核對先前的工作已停止。這會再次授權目前 AI 處理原文字，可能重複計費。確定重新送出？')) {
          mutate(`/api/jobs/${job.id}/retry`, {expected_job_version: job.version, consent: true, binding_id: snapshot.connection.binding_id});
        }
      }));
    }
    card.append(actions);
  }
  if (job.status === 'delivered' || job.status === 'cancelled') card.append(element('p', '停止收件可拒絕遲到回覆，但不能保證供應商停止運算或計費。', 'job-note'));
  if (job.status === 'uncertain') card.append(element('p', '原工作已保存，未自動重送。請先核對上一個 AI 的處理狀態。', 'job-note'));
  return card;
}
function renderJobs() {
  const jobs = snapshot.jobs.slice(0, visibleJobs);
  const active = document.activeElement;
  const focus = active?.dataset?.action ? {job: active.dataset.job, action: active.dataset.action} : null;
  const list = $('jobs');
  let newReplies = 0;
  if (jobs.length && !cards.size) list.replaceChildren();
  const wanted = new Set(jobs.map((job) => job.id));
  for (const [id, entry] of cards) {
    if (!wanted.has(id)) { entry.node.remove(); cards.delete(id); }
  }
  jobs.forEach((job, index) => {
    const stamp = rules.jobStamp(job);
    let entry = cards.get(job.id);
    if (!entry || entry.stamp !== stamp) {
      if (entry && entry.status !== 'completed' && job.status === 'completed') newReplies++;
      const node = jobCard(job);
      if (entry) entry.node.replaceWith(node);
      entry = {stamp, node, status: job.status}; cards.set(job.id, entry);
    }
    if (list.children[index] !== entry.node) list.insertBefore(entry.node, list.children[index] || null);
  });
  if (focus && !document.contains(active)) {
    const entry = cards.get(focus.job);
    const replacement = entry && Array.from(entry.node.querySelectorAll('button')).find((button) => button.dataset.action === focus.action);
    if (replacement) replacement.focus();
    else $('jobs-title').focus();
  }
  $('job-count').textContent = snapshot.jobs.length;
  if (newReplies) announce(`已收到 ${newReplies} 筆回覆，請在工作與回覆區查看。`);
  $('more-jobs').hidden = visibleJobs >= snapshot.jobs.length;
  $('more-jobs').textContent = `顯示更多工作（還有 ${Math.max(0, snapshot.jobs.length - visibleJobs)} 筆）`;
}
function render() {
  const connection = snapshot.connection;
  $('connection-label').textContent = rules.connectionLabel(connection);
  $('connection-dot').classList.toggle('live', rules.ready(connection));
  $('client').textContent = connection.client ? `${connection.client.name} ${connection.client.version}` : '—';
  $('connection-note').textContent = rules.connectionNote(connection);
  renderJobs();
  const stamp = snapshot.events.map((event) => event.seq).join(',');
  if (stamp !== renderedEvents) {
    renderedEvents = stamp;
    const events = document.createDocumentFragment();
    for (const event of snapshot.events) {
      const row = element('div', undefined, 'event');
      row.append(element('time', new Date(event.created * 1000).toLocaleTimeString('zh-TW')), element('span', eventLabels[event.kind] || event.kind), element('span', event.job_id.slice(-12), 'job-id'));
      events.append(row);
    }
    $('events').replaceChildren(events);
  }
  controls();
}
function schedulePoll() {
  clearTimeout(pollTimer);
  if (!closed) pollTimer = setTimeout(poll, rules.pollDelay(document.hidden));
}
async function poll() {
  if (closed) return;
  try { await refresh(); } catch (error) { closeInterface(error.status === 401 ? error.message : undefined); }
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
  } catch (error) { closeInterface(error.definitive ? error.message : undefined); }
  schedulePoll();
}
$('retry-request').addEventListener('click', () => mutate(null, null, true));
$('prompt').addEventListener('input', controls);
$('consent').addEventListener('change', controls);
$('task-form').addEventListener('submit', (event) => {
  event.preventDefault();
  controls();
  if ($('send').disabled) return;
  mutate('/api/jobs', {prompt: $('prompt').value, consent: true, binding_id: snapshot.connection.binding_id});
});
$('more-jobs').addEventListener('click', () => { visibleJobs += 20; renderJobs(); controls(); });
$('reconnect').addEventListener('click', async () => {
  $('reconnect').disabled = true;
  etag = '';
  try {
    const next = await api('/api/state');
    if (snapshot && snapshot.connection.binding_id !== next.connection.binding_id) $('consent').checked = false;
    snapshot = next; csrf = next.csrf; connected = true; closed = false;
    $('closed-screen').hidden = true; $('workspace').hidden = false; $('workspace').inert = false;
    document.title = 'Clipen · 本機工作台';
    render(); $('prompt').focus(); schedulePoll();
    announce(pending ? '連線已恢復；請先核對上一筆請求。' : '本次服務連線已恢復，草稿未送出。');
  } catch (error) { $('closed-reason').textContent = error.definitive ? error.message : '仍無法連接本次服務。請重新開啟 AI 並使用 terminal 的新連結。'; }
  finally { $('reconnect').disabled = false; }
});
function syncNavigation() {
  const current = window.location.hash || '#main';
  document.querySelectorAll('.nav-item').forEach((link) => {
    const selected = link.getAttribute('href') === current;
    link.classList.toggle('current', selected);
    if (selected) link.setAttribute('aria-current', 'location'); else link.removeAttribute('aria-current');
  });
}
window.addEventListener('hashchange', syncNavigation);
window.addEventListener('beforeunload', (event) => {
  if ($('prompt').value.trim() || pending) { event.preventDefault(); event.returnValue = ''; }
});
document.addEventListener('visibilitychange', () => {
  if (closed) return;
  clearTimeout(pollTimer);
  if (!document.hidden) poll(); else schedulePoll();
});
syncNavigation();
controls();
start();
