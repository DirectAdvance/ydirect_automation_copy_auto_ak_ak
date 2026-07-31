/* ============================================================================
   content_copy.js — вкладка «Копирование» (Авто) в редакторе контента.

   Вызывает существующий copy-сервис (:5022) через те же nginx-проксируемые
   эндпоинты /direct/api/copy_*. Backend НЕ дублируется.

   Пространство имён: CECOPY_* (чтобы не конфликтовать с ACC_ALL и esc()
   из content_editor.js; copy_common.js на этой странице НЕ подключён).

   Для copy-вкладки логины грузятся отдельным backend-scoped списком.
   ============================================================================ */

// ── Состояние ────────────────────────────────────────────────────────────────
let CECOPY_CAMPS = [];
let CECOPY_SELECTED = new Set();
let CECOPY_TARGET_INFO = null;
let CECOPY_JOB_ID = null;
let CECOPY_POLL_TIMER = null;
let _CECOPY_INIT = false;
let CECOPY_ACC_ALL = [];
let CECOPY_JOB_META = {};
const CECOPY_JOB_LS = 'direct_content_copy_job_v1';

// ── Вспомогательные функции ──────────────────────────────────────────────────
function _cecopySrcSuggest() { _cecopyDropdown('cecopy-source-login', 'cecopy-source-dd'); }
function _cecopyTgtSuggest() { _cecopyDropdown('cecopy-target-login', 'cecopy-target-dd'); }
function ceCopySrcSuggest() { _cecopySrcSuggest(); }
function ceCopyTgtSuggest() { _cecopyTgtSuggest(); }
function ceCopySrcHide()    { _cecopyHideDD('cecopy-source-dd'); }
function ceCopyTgtHide()    { _cecopyHideDD('cecopy-target-dd'); }
function ceCopySrcKey(e)    { if (e && e.key === 'Escape') ceCopySrcHide(); }
function ceCopyTgtKey(e)    { if (e && e.key === 'Escape') ceCopyTgtHide(); }

function _cecopyEsc(s) {
  return (s == null ? '' : ('' + s)).replace(/[&<>"']/g, c =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

function _cecopyHideDD(ddId) {
  const dd = document.getElementById(ddId);
  if (dd) dd.style.display = 'none';
}

function _cecopyDropdown(inpId, ddId) {
  const inp = document.getElementById(inpId);
  const dd = document.getElementById(ddId);
  if (!inp || !dd) return;
  const q = (inp.value || '').trim().toLowerCase();
  const rows = CECOPY_ACC_ALL.length
    ? CECOPY_ACC_ALL
    : (typeof ACC_ALL !== 'undefined' ? ACC_ALL : []);
  let filtered = rows;
  if (q) filtered = rows.filter(r =>
    [r.login_key, r.domain, r.salon, r.city].some(v => v && String(v).toLowerCase().includes(q))
  );
  filtered = filtered.slice(0, 40);
  if (!filtered.length) {
    dd.innerHTML = '<div style="padding:8px;color:var(--text-muted)">Нет совпадений</div>';
    dd.style.display = 'block';
    return;
  }
  dd.innerHTML = filtered.map(r => {
    const sub = [r.domain, r.city].filter(v => v && v !== 'Нет').map(_cecopyEsc).join(' · ');
    return '<div class="opt" style="padding:6px 10px;cursor:pointer" data-login="' + _cecopyEsc(r.login_key) + '">' +
      '<b>' + _cecopyEsc(r.login_key) + '</b>' + (sub ? '<div style="font-size:11px;color:var(--text-muted)">' + sub + '</div>' : '') +
      '</div>';
  }).join('');
  dd.querySelectorAll('.opt').forEach(el => el.onmousedown = () => {
    const lg = el.getAttribute('data-login') || '';
    inp.value = lg;
    dd.style.display = 'none';
    if (inpId === 'cecopy-target-login') ceCopyLoadTargetPrefill();
  });
  dd.style.display = 'block';
}

function _cecopyIsArchivedCampaign(c) {
  const state = String((c || {}).state || '').toUpperCase();
  const status = String((c || {}).status || '').toUpperCase();
  return state === 'ARCHIVED' || status === 'ARCHIVED' || (c || {}).archived === true;
}

// ── Инициализатор (вызывается ceSetSection при первом открытии) ───────────────
function _ceCopyTabInit() {
  if (_CECOPY_INIT) return;
  _CECOPY_INIT = true;
  _cecopyEnsureAccounts().catch(() => {});
  // Закрытие дропдаунов кликом вне
  document.addEventListener('click', e => {
    ['cecopy-source-dd', 'cecopy-target-dd'].forEach(id => {
      const dd = document.getElementById(id);
      const inp = document.getElementById(id.replace('-dd', '-login'));
      if (dd && inp && !dd.contains(e.target) && e.target !== inp) {
        dd.style.display = 'none';
      }
    });
  });
  _cecopyRestoreJob();
  if (CECOPY_JOB_ID) _cecopyPollStart();
}

async function _cecopyEnsureAccounts() {
  if (CECOPY_ACC_ALL.length) return CECOPY_ACC_ALL;
  const r = await fetch('/direct/api/content-editor/copy_accounts?status=__all__');
  const j = await r.json();
  CECOPY_ACC_ALL = j.rows || [];
  return CECOPY_ACC_ALL;
}

// ── Загрузка кампаний источника ───────────────────────────────────────────────
async function ceCopyLoadSource() {
  const login = ((document.getElementById('cecopy-source-login') || {}).value || '').trim();
  const box = document.getElementById('cecopy-camps-box');
  if (!login) { alert('Укажите старый аккаунт'); return; }
  if (box) box.innerHTML = '<div class="ce-hint" style="padding:8px">Загрузка…</div>';
  CECOPY_CAMPS = [];
  CECOPY_SELECTED.clear();
  _cecopyUpdateSelectedNote();
  try {
    const r = await fetch('/direct/api/copy_campaigns?login=' + encodeURIComponent(login));
    const j = await r.json();
    if (j.error) {
      if (box) box.innerHTML = '<div style="color:#e3b341;padding:8px">' + _cecopyEsc(j.error) + '</div>';
      return;
    }
    CECOPY_CAMPS = (j.campaigns || []).filter(c => !_cecopyIsArchivedCampaign(c)).slice().sort((a, b) =>
      String(a.name || '').localeCompare(String(b.name || ''), 'ru'));
    ceCopyRenderCamps();
  } catch (e) {
    if (box) box.innerHTML = '<div style="color:#ff7b72;padding:8px">Ошибка: ' + _cecopyEsc(String(e)) + '</div>';
  }
}

function ceCopyRenderCamps() {
  const box = document.getElementById('cecopy-camps-box');
  if (!box) return;
  const q = ((document.getElementById('cecopy-camp-search') || {}).value || '').trim().toLowerCase();
  const rows = q
    ? CECOPY_CAMPS.filter(c => [c.name, String(c.id), c.type].some(v => v && String(v).toLowerCase().includes(q)))
    : CECOPY_CAMPS;
  if (!rows.length) {
    box.innerHTML = '<div class="ce-hint" style="padding:8px">' +
      (CECOPY_CAMPS.length ? 'Нет совпадений' : 'Кампании не загружены') + '</div>';
    _cecopyUpdateSelectedNote();
    return;
  }
  box.innerHTML = rows.map(c =>
    '<label style="display:flex;align-items:center;gap:8px;padding:4px 6px;cursor:pointer">' +
    '<input type="checkbox" ' + (CECOPY_SELECTED.has(String(c.id)) ? 'checked' : '') +
    ' onchange="ceCopyToggle(\'' + String(c.id).replace(/'/g, '') + '\', this.checked)">' +
    '<span><b>' + _cecopyEsc(String(c.id)) + '</b> ' + _cecopyEsc(c.name || '') + '</span>' +
    '</label>'
  ).join('');
  _cecopyUpdateSelectedNote();
}

function ceCopyToggle(id, on) {
  if (on) CECOPY_SELECTED.add(String(id)); else CECOPY_SELECTED.delete(String(id));
  _cecopyUpdateSelectedNote();
}
function ceCopySelectAll(on) {
  const q = ((document.getElementById('cecopy-camp-search') || {}).value || '').trim().toLowerCase();
  const rows = q
    ? CECOPY_CAMPS.filter(c => [c.name, String(c.id), c.type].some(v => v && String(v).toLowerCase().includes(q)))
    : CECOPY_CAMPS;
  rows.forEach(c => on ? CECOPY_SELECTED.add(String(c.id)) : CECOPY_SELECTED.delete(String(c.id)));
  ceCopyRenderCamps();
}
function _cecopyUpdateSelectedNote() {
  const el = document.getElementById('cecopy-selected-note');
  if (el) el.textContent = 'Выбрано: ' + CECOPY_SELECTED.size;
}

// ── Префилл целевого аккаунта ─────────────────────────────────────────────────
async function ceCopyLoadTargetPrefill() {
  const login = ((document.getElementById('cecopy-target-login') || {}).value || '').trim();
  const note = document.getElementById('cecopy-target-note');
  _cecopyLoadTargetInfo(login);
  if (!login) { if (note) note.textContent = ''; return; }
  if (note) note.textContent = 'Подставляю данные…';
  try {
    const r = await fetch('/direct/api/copy_target_prefill?login=' + encodeURIComponent(login));
    const j = await r.json();
    if (j.error) { if (note) note.innerHTML = '<span style="color:#e3b341">' + _cecopyEsc(j.error) + '</span>'; return; }
    document.getElementById('cecopy-target-domain').value = j.domain || '';
    document.getElementById('cecopy-target-city').value = j.city || '';
    document.getElementById('cecopy-target-region').value = j.region || '';
    document.getElementById('cecopy-counter').value = j.counter_id || '';
    document.getElementById('cecopy-goal').value = j.goal_id || '';
    if (note) note.textContent = j.found === false ? 'В БД не найден — заполни вручную' : 'Подставлено из БД';
  } catch (e) {
    if (note) note.innerHTML = '<span style="color:#ff7b72">Ошибка: ' + _cecopyEsc(String(e)) + '</span>';
  }
}

async function _cecopyLoadTargetInfo(login) {
  const info = document.getElementById('cecopy-cleanup-info');
  CECOPY_TARGET_INFO = null;
  _cecopyUpdateCleanupBtns();
  if (!login) { if (info) info.textContent = 'Укажите целевой аккаунт.'; return; }
  if (info) info.textContent = 'Загружаю кампании цели…';
  try {
    const r = await fetch('/direct/api/copy_target_campaigns?login=' + encodeURIComponent(login));
    const j = await r.json();
    const curLogin = ((document.getElementById('cecopy-target-login') || {}).value || '').trim();
    if (curLogin !== login) return; // устарело
    if (j.error) { if (info) info.innerHTML = '<span style="color:#e3b341">⚠ ' + _cecopyEsc(j.error) + '</span>'; return; }
    CECOPY_TARGET_INFO = j;
    const t = j.total || 0, d = j.draft_count || 0, nd = j.non_draft_count || 0;
    if (info) info.textContent = t === 0 ? 'Аккаунт чист' : t + ' кампаний: ' + d + ' черновиков, ' + nd + ' не-черновиков';
  } catch (e) {
    if (info) info.innerHTML = '<span style="color:#ff7b72">Ошибка: ' + _cecopyEsc(String(e)) + '</span>';
  }
  _cecopyUpdateCleanupBtns();
}

function _cecopyUpdateCleanupBtns() {
  const info = CECOPY_TARGET_INFO;
  const hasDrafts = info && (info.draft_count || 0) > 0;
  const hasArchiv = info && (info.archivable_count || 0) > 0;
  const hasAny    = info && (info.total || 0) > 0;
  [
    ['cecopy-cleanup-del-label', hasAny && hasDrafts, 'delete_drafts'],
    ['cecopy-cleanup-arc-label', hasAny && hasArchiv, 'archive'],
  ].forEach(([lId, on, val]) => {
    const lbl = document.getElementById(lId);
    if (!lbl) return;
    lbl.classList.toggle('cleanup-disabled', !on);
    const inp = lbl.querySelector('input');
    if (inp) inp.disabled = !on;
    if (!on) {
      const sel = document.querySelector('input[name="cecopy-cleanup"]:checked');
      if (sel && sel.value === val) {
        const none = document.querySelector('input[name="cecopy-cleanup"][value="none"]');
        if (none) none.checked = true;
      }
    }
  });
}

function ceCopyResolveGoal() {
  const counter = ((document.getElementById('cecopy-counter') || {}).value || '').trim();
  if (!counter) return;
  fetch('/direct/api/goal_for_counter?counter=' + encodeURIComponent(counter))
    .then(r => r.json()).then(j => {
      if (j.goal_id) document.getElementById('cecopy-goal').value = j.goal_id;
    }).catch(() => {});
}

function _cecopyRememberJob(jobId, sourceLogin, targetLogin) {
  if (!jobId) return;
  CECOPY_JOB_META = {
    job_id: jobId,
    source_login: sourceLogin || '',
    target_login: targetLogin || '',
  };
  try { localStorage.setItem(CECOPY_JOB_LS, JSON.stringify(CECOPY_JOB_META)); } catch (e) {}
}

function _cecopyForgetJob(jobId) {
  if (jobId && CECOPY_JOB_ID && jobId !== CECOPY_JOB_ID) return;
  CECOPY_JOB_ID = null;
  CECOPY_JOB_META = {};
  try { localStorage.removeItem(CECOPY_JOB_LS); } catch (e) {}
}

function _cecopyRestoreJob() {
  if (CECOPY_JOB_ID) return;
  try {
    const saved = JSON.parse(localStorage.getItem(CECOPY_JOB_LS) || '{}');
    if (!saved || !saved.job_id) return;
    CECOPY_JOB_ID = String(saved.job_id);
    CECOPY_JOB_META = saved;
    _cecopyRenderJobCard({
      job_id: CECOPY_JOB_ID,
      status: 'queued',
      source_login: saved.source_login || '',
      target_login: saved.target_login || '',
    });
  } catch (e) {}
}

// ── Попап ошибок запуска (Метрика/цель) — стиль сайта вместо alert() ──────────
// Разметки в шаблоне для copy-вкладки нет, поэтому строим .ceimg-modal (те же
// классы, что у "Замены изображения") один раз и переиспользуем узел.
function _cecopyEnsureAlertModal() {
  let el = document.getElementById('cecopy-alert-modal');
  if (el) return el;
  el = document.createElement('div');
  el.className = 'ceimg-modal';
  el.id = 'cecopy-alert-modal';
  el.hidden = true;
  el.innerHTML =
    '<div class="ceimg-modal-back" data-cecopy-alert-close="1"></div>' +
    '<div class="ceimg-modal-box" role="dialog" aria-modal="true" aria-labelledby="cecopy-alert-title" style="width:min(420px,100%)">' +
      '<div class="ceimg-modal-head">' +
        '<h3 id="cecopy-alert-title">Нельзя запустить копирование</h3>' +
        '<button type="button" class="ceimg-modal-x" id="cecopy-alert-close" aria-label="Закрыть окно">' +
          '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">' +
            '<path d="M4 4l8 8M12 4l-8 8"></path>' +
          '</svg>' +
        '</button>' +
      '</div>' +
      '<div class="ceimg-modal-hint" id="cecopy-alert-text" style="margin-top:14px"></div>' +
      '<div class="ceimg-modal-foot">' +
        '<button type="button" class="ce-btn small" id="cecopy-alert-ok">Понятно</button>' +
      '</div>' +
    '</div>';
  document.body.appendChild(el);
  const close = () => { el.hidden = true; };
  el.addEventListener('click', e => {
    if (e.target.closest('[data-cecopy-alert-close]') || e.target.closest('#cecopy-alert-close') || e.target.closest('#cecopy-alert-ok')) close();
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !el.hidden) close(); });
  return el;
}

function _cecopyAlert(message) {
  const el = _cecopyEnsureAlertModal();
  const txt = document.getElementById('cecopy-alert-text');
  if (txt) txt.textContent = message;
  el.hidden = false;
}

// ── Запуск копирования ────────────────────────────────────────────────────────
async function ceCopyStart() {
  const srcLogin = ((document.getElementById('cecopy-source-login') || {}).value || '').trim();
  const tgtLogin = ((document.getElementById('cecopy-target-login') || {}).value || '').trim();
  const domain   = ((document.getElementById('cecopy-target-domain') || {}).value || '').trim();
  const city     = ((document.getElementById('cecopy-target-city') || {}).value || '').trim();
  const region   = ((document.getElementById('cecopy-target-region') || {}).value || '').trim();
  const counter  = ((document.getElementById('cecopy-counter') || {}).value || '').trim();
  const goal     = ((document.getElementById('cecopy-goal') || {}).value || '').trim();
  const cleanup  = (document.querySelector('input[name="cecopy-cleanup"]:checked') || {}).value || 'none';
  if (!srcLogin || !tgtLogin) { alert('Укажите старый и новый аккаунт'); return; }
  if (!CECOPY_SELECTED.size) { alert('Выберите хотя бы одну кампанию'); return; }
  if (!counter) { _cecopyAlert('Укажите счётчик Метрики'); return; }
  if (!goal) { _cecopyAlert('Цель «Все формы» обязательна'); return; }
  if (!domain) { alert('Укажите домен целевого аккаунта'); return; }
  if (!city && !region) { alert('Укажите город или регион целевого аккаунта'); return; }
  const btn = document.getElementById('cecopy-start-btn');
  if (btn) btn.disabled = true;
  const msg = document.getElementById('cecopy-msg');
  if (msg) msg.textContent = 'Запускаю копирование…';
  try {
    const body = {
      source_login: srcLogin,
      target_login: tgtLogin,
      target_domain: domain,
      target_city: city,
      target_region: region,
      counter_id: counter,
      goal_id: goal,
      campaign_ids: Array.from(CECOPY_SELECTED).map(Number).filter(Boolean),
      target_cleanup: cleanup,
    };
    const r = await fetch('/direct/api/copy_start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (j.error) {
      if (msg) msg.innerHTML = '<span style="color:#e3b341">' + _cecopyEsc(j.error) + '</span>';
      return;
    }
    CECOPY_JOB_ID = j.job_id || null;
    if (CECOPY_JOB_ID) _cecopyRememberJob(CECOPY_JOB_ID, srcLogin, tgtLogin);
    if (msg) msg.innerHTML = 'Задание <b>' + _cecopyEsc(j.job_id || '') + '</b> запущено.';
    if (CECOPY_JOB_ID) _cecopyPollStart();
  } catch (e) {
    if (msg) msg.innerHTML = '<span style="color:#ff7b72">Ошибка: ' + _cecopyEsc(String(e)) + '</span>';
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ── Поллинг статуса джобы ─────────────────────────────────────────────────────
function _cecopyPollStart() {
  if (CECOPY_POLL_TIMER) clearInterval(CECOPY_POLL_TIMER);
  CECOPY_POLL_TIMER = setInterval(_cecopyPollTick, 5000);
  _cecopyPollTick();
}

async function _cecopyPollTick() {
  if (!CECOPY_JOB_ID) return;
  try {
    const r = await fetch('/direct/api/copy_status/' + encodeURIComponent(CECOPY_JOB_ID));
    if (r.status === 404) {
      _cecopyForgetJob(CECOPY_JOB_ID);
      if (CECOPY_POLL_TIMER) { clearInterval(CECOPY_POLL_TIMER); CECOPY_POLL_TIMER = null; }
      return;
    }
    const j = await r.json();
    _cecopyRenderJobCard(j);
    if (j.terminal || j.status === 'done' || j.status === 'error' || j.status === 'cancelled') {
      _cecopyForgetJob(CECOPY_JOB_ID);
      if (CECOPY_POLL_TIMER) { clearInterval(CECOPY_POLL_TIMER); CECOPY_POLL_TIMER = null; }
    }
  } catch (e) { /* не блокируем UI */ }
}

// Убрать карточку копирования из общего виджета (без API-вызова — нет cancel у copy).
function _cecopyDismissCard(jobId) {
  const card = document.getElementById('ce-job-copy-' + jobId);
  if (card) card.remove();
  _cecopyForgetJob(jobId);
}

// Рендерит/обновляет карточку копирования в ОБЩЕМ виджете #ce-job-stack
// (те же CSS-классы, что у content-job, id-префикс ce-job-copy- чтобы не конфликтовать).
function _cecopyRenderJobCard(j) {
  const stack = document.getElementById('ce-job-stack');
  if (!stack) return;
  const jobId = j.job_id || CECOPY_JOB_ID;
  if (!jobId) return;
  const cardId = 'ce-job-copy-' + jobId;
  let card = document.getElementById(cardId);
  if (!card) {
    card = document.createElement('div');
    card.id = cardId;
    card.className = 'ce-job-card queued';
    card.innerHTML =
      '<div class="ce-job-head"><span class="ce-job-acc"></span>' +
      '<button class="ce-job-cancel" onclick="_cecopyDismissCard(\'' + jobId.replace(/'/g, '') + '\')">Убрать</button></div>' +
      '<div class="ce-job-progress"><div class="ce-job-progress-bar"></div></div>' +
      '<div class="ce-job-note">в очереди…</div>';
    stack.appendChild(card);
  }
  // Подпись аккаунта
  const srcLogin = j.source_login || CECOPY_JOB_META.source_login || '';
  const tgtLogin = j.target_login || CECOPY_JOB_META.target_login || '';
  const accLabel = (srcLogin && tgtLogin)
    ? (srcLogin + ' → ' + tgtLogin)
    : (srcLogin || tgtLogin || 'аккаунт');
  const accEl = card.querySelector('.ce-job-acc');
  if (accEl) accEl.textContent = accLabel + ' · копирование';

  const st = j.status || j.public_status || 'queued';
  card.className = 'ce-job-card ' + st;

  const bar  = card.querySelector('.ce-job-progress-bar');
  const note = card.querySelector('.ce-job-note');
  const btn  = card.querySelector('.ce-job-cancel');

  const stLabel = {
    queued: 'В очереди', running: 'Выполняется', done: 'Готово',
    error: 'Ошибка', cancelled: 'Отменено', settling: 'Сверка…', repair_pending: 'Ремонт…',
  }[st] || st;

  const created  = j.created  || (j.result_summary || {}).created  || '';
  const expected = j.expected || (j.result_summary || {}).expected || j.total || '';
  const elapsed  = j.elapsed ? (Math.round(Number(j.elapsed)) + ' с') : '';
  const progStr  = (created || expected) ? (' · создано: ' + created + (expected ? '/' + expected : '')) : '';

  if (st === 'queued') {
    if (bar) bar.style.width = '4%';
    if (note) note.textContent = 'в очереди…';
  } else if (st === 'running' || st === 'settling' || st === 'repair_pending') {
    const sec   = Number(j.elapsed || 0);
    const total = Number(j.total || 10);
    const est   = Math.max(60, 30 + total * 20);
    const pct   = 6 + 89 * (1 - Math.exp(-sec / est));
    if (bar) bar.style.width = Math.min(95, pct).toFixed(1) + '%';
    if (note) note.textContent = stLabel + (elapsed ? ' · ' + elapsed : '') + progStr;
  } else {
    // terminal
    if (bar) bar.style.width = '100%';
    if (btn) btn.textContent = 'Убрать';
    let noteText = stLabel;
    if (created || expected) noteText += ' · создано: ' + created + (expected ? '/' + expected : '');
    if (elapsed) noteText += ' · ' + elapsed;
    if (j.error) noteText += ' · ' + _cecopyEsc(String(j.error).slice(0, 100));
    if (note) note.textContent = noteText;
  }
}
