/* ============================================================================
   copy_common.js — ОБЩИЙ код обеих вкладок /direct/automation/copy.

   ЕДИНСТВЕННЫЙ источник правды для:
     • списка аккаунтов и автоподсказки логинов (ACC_ALL, _copySuggest);
     • карточки «Источник»: COPY_CAMPAIGNS / COPY_SELECTED / loadCopySourceCampaigns();
     • поллинга джоб (_cqTick) и ОЧЕРЕДИ копирования (#copy-job-stack) — единственного
       места показа статуса: прогресс, лог и пропущенные кампании рисует _cqUpdateCard.
       Отдельной панели статуса в карточке «Цель» больше нет (убрана 2026-07-17).

   ⛔ Не дублировать это в copy_auto.js / copy_other.js — две расходящиеся копии
   поллинга уже были причиной рассинхрона вкладок.

   Вкладочно-специфичное: copy_auto.js (фиды, startCopyCampaigns),
   copy_other.js (гео, картинки, startOtherCampaigns).
   ============================================================================ */

// Мини-хелпер экранирования (перенос из index.html).
function esc(s){ return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

// Список аккаунтов для автоподсказки логинов. /direct/api/accounts не является copy-роутом —
// через nginx запрос уходит на основной сервис (5020). Если недоступен, поля можно заполнить вручную.
let ACC_ALL=[];
async function loadAccounts(){
  try{
    const r=await fetch('/direct/api/accounts?status=__all__');
    const j=await r.json(); ACC_ALL=j.rows||[];
  }catch(e){ ACC_ALL=[]; }
}

// ── Состояние копирования (общее для вкладок) ────────────────────────────────
let COPY_CAMPAIGNS=[], COPY_SELECTED=new Set();
// COPY_JOB_ID / _COPY_JOB_TAB — legacy: их пишут copy_auto.js и copy_other.js сразу после
// старта. Общий код ими БОЛЬШЕ НЕ пользуется (статус показывает очередь, а её состав и
// вкладка джобы живут в COPY_QUEUE и переживают F5, чего эти переменные не умели).
// Объявления оставлены, чтобы присваивание во вкладках не создавало неявную глобаль.
let COPY_JOB_ID=null, _COPY_JOB_TAB='auto';

// ── Автоподсказка логинов ────────────────────────────────────────────────────
function _copySuggest(inpId, ddId){
  const inp=document.getElementById(inpId), dd=document.getElementById(ddId); if(!inp||!dd) return;
  const q=(inp.value||'').trim().toLowerCase();
  let rows=ACC_ALL||[];
  if(q) rows=rows.filter(r=>[r.login_key,r.domain,r.salon,r.city].some(v=>v&&String(v).toLowerCase().includes(q)));
  rows=rows.slice(0,40);
  if(!rows.length){
    const msg=(ACC_ALL&&ACC_ALL.length)
      ? 'Совпадений нет. Можно оставить введённый логин и нажать «Обновить».'
      : 'Загрузка аккаунтов…';
    dd.innerHTML='<div class="empty">'+msg+'</div>';
    dd.classList.add('open');
    return;
  }
  dd.innerHTML=rows.map(r=>{
    const sub=[r.domain,r.city].filter(v=>v&&v!=='Нет').map(esc).join(' · ');
    return '<div class="opt" data-login="'+esc(r.login_key)+'"><b>'+esc(r.login_key)+'</b>'+(sub?'<span class="sub">'+sub+'</span>':'')+'</div>';
  }).join('');
  dd.querySelectorAll('.opt').forEach(el=>el.onmousedown=()=>_copyPick(inpId,ddId,el.getAttribute('data-login')||''));
  dd.classList.add('open');
}
function _copyPick(inpId, ddId, login){
  const inp=document.getElementById(inpId), dd=document.getElementById(ddId); if(inp) inp.value=login; if(dd) dd.classList.remove('open');
  // Префилл живёт в JS своей вкладки — зовём через guard, чтобы common не зависел от порядка загрузки.
  if(inpId==='copy-target-login' && typeof loadCopyTargetPrefill==='function') loadCopyTargetPrefill();
  if(inpId==='other-target-login' && typeof loadOtherTargetPrefill==='function') loadOtherTargetPrefill();
}
function _copyHide(ddId){ const dd=document.getElementById(ddId); if(dd) dd.classList.remove('open'); }
function _copyKey(ddId, e){ if(e&&e.key==='Escape') _copyHide(ddId); }
// Источник — общий для обеих вкладок
function copySourceSuggest(){ _copySuggest('copy-source-login','copy-source-dd'); }
function copySourceHide(){ _copyHide('copy-source-dd'); }
function copySourceKey(e){ _copyKey('copy-source-dd',e); }

// ── Карточка «Источник» ──────────────────────────────────────────────────────
function copyBadge(state){
  const s=String(state||'').toUpperCase();
  if(s==='ON') return '<span class="copy-badge ok">идёт</span>';
  if(s==='SUSPENDED'||s==='OFF') return '<span class="copy-badge stop">остановлена</span>';
  return '<span class="copy-badge">'+esc(s||'—')+'</span>';
}
function updateCopySelectedNote(){
  const el=document.getElementById('copy-selected-note'); if(el) el.textContent='Выбрано: '+COPY_SELECTED.size;
}
function filteredCopyCampaigns(){
  const q=((document.getElementById('copy-campaign-search')||{}).value||'').trim().toLowerCase();
  if(!q) return COPY_CAMPAIGNS.slice();
  return COPY_CAMPAIGNS.filter(c=>[c.name,c.id,c.type].some(v=>v&&String(v).toLowerCase().includes(q)));
}
function renderCopySourceCampaigns(){
  const box=document.getElementById('copy-campaigns-box'); if(!box) return;
  const rows=filteredCopyCampaigns();
  if(!COPY_CAMPAIGNS.length){ box.innerHTML='<div class="copy-note" style="padding:12px">Кампании ещё не загружены.</div>'; updateCopySelectedNote(); return; }
  const head='<div class="copy-camp-row copy-camp-head"><div></div><div>ID</div><div>Название</div><div>Статус</div></div>';
  box.innerHTML=head+rows.map(c=>'<label class="copy-camp-row">'
      +'<input type="checkbox" '+(COPY_SELECTED.has(String(c.id))?'checked':'')+' onchange="copyToggleCampaign(\''+String(c.id).replace(/'/g,'')+'\', this.checked)">'
      +'<b>'+esc(String(c.id))+'</b>'
      +'<div><b>'+esc(c.name||'')+'</b><div class="copy-note">'+esc(c.type||'')+'</div></div>'
      +copyBadge(c.state||c.status)
      +'</label>').join('');
  updateCopySelectedNote();
}
function copyToggleCampaign(id, on){ if(on) COPY_SELECTED.add(String(id)); else COPY_SELECTED.delete(String(id)); updateCopySelectedNote(); }
function copyToggleAllVisible(on){ filteredCopyCampaigns().forEach(c=>on?COPY_SELECTED.add(String(c.id)):COPY_SELECTED.delete(String(c.id))); renderCopySourceCampaigns(); }

async function loadCopySourceCampaigns(){
  const login=((document.getElementById('copy-source-login')||{}).value||'').trim();
  const box=document.getElementById('copy-campaigns-box'), note=document.getElementById('copy-source-note');
  if(!login){ alert('Укажите старый аккаунт'); return; }
  if(box) box.innerHTML='<div class="copy-note" style="padding:12px">Загрузка кампаний…</div>';
  if(note) note.textContent='';
  try{
    const j=await (await fetch('/direct/api/copy_campaigns?login='+encodeURIComponent(login))).json();
    if(j.error){ if(box) box.innerHTML='<div class="err" style="padding:12px">'+esc(j.error)+'</div>'; return; }
    COPY_CAMPAIGNS=(j.campaigns||[]).slice().sort((a,b)=>String(a.name||'').localeCompare(String(b.name||''),'ru'));
    COPY_SELECTED.clear();
    if(note) note.textContent='Загружено кампаний: '+COPY_CAMPAIGNS.length;
    renderCopySourceCampaigns();
  }catch(e){ if(box) box.innerHTML='<div class="err" style="padding:12px">Ошибка: '+esc(String(e))+'</div>'; }
}

// Общий резолв цели «Все формы» по счётчику: вкладки отличаются только id полей.
async function _copyResolveGoalInto(counterInputId, goalInputId){
  const counter=((document.getElementById(counterInputId)||{}).value||'').trim();
  if(!counter) return;
  try{
    const j=await (await fetch('/direct/api/goal_for_counter?counter='+encodeURIComponent(counter))).json();
    if(j.goal_id) document.getElementById(goalInputId).value=j.goal_id;
  }catch(e){}
}

// ── Кнопки «Копировать РК» ───────────────────────────────────────────────────
// Кнопка вкладки заблокирована ровно пока у ЭТОЙ вкладки есть незавершённая джоба.
// Считаем от очереди, а не от COPY_JOB_ID: после F5 переменная теряется, а очередь
// восстанавливается (_cqRestore) — иначе кнопка была бы активна при идущем копировании.
function _cqStartBtnId(tab){ return tab==='other' ? 'other-start-btn' : 'copy-start-btn'; }
function _cqSyncStartBtns(){
  ['auto','other'].forEach(tab=>{
    const btn=document.getElementById(_cqStartBtnId(tab)); if(!btn) return;
    btn.disabled=[...COPY_QUEUE.values()].some(j=>!j.terminal && (j.tab||'auto')===tab);
  });
}

/* ════════════════════════════════════════════════════════════════════════════
   ОЧЕРЕДЬ КОПИРОВАНИЯ (#copy-job-stack)

   Эталон — «Очередь создания» в templates/direct/index.html (JOBS/_jobRenderCard/
   _jobUpdateCard): та же структура карточки, те же классы, те же цвета.

   Отличия от эталона продиктованы API direct-copy.service (:5022), а НЕ вкусом:
   • нет серверного списка джоб (эндпоинта /direct/api/copy_jobs нет) → состав очереди
     восстанавливается из localStorage, а факты — из GET /direct/api/copy_status/<job_id>;
   • нет /direct/api/copy_cancel → кнопка «Отмена» у ИДУЩЕЙ джобы задизаблена (честно:
     остановить копирование сейчас нечем). Завершённая → «Убрать» (снимает карточку);
   • агентство copy_status не отдаёт — берём из ответа copy_start/copy_other_start и
     храним в localStorage вместе с логином.
   ════════════════════════════════════════════════════════════════════════════ */
const COPY_QUEUE = new Map();          // jobId → {jobId, login, agency, total, tab, status, terminal, progressPct, runningFrom}
const COPY_QUEUE_LS = 'direct_copy_jobs_v1';
const COPY_QUEUE_COLLAPSE_LS = 'direct_copy_jobstack_collapsed';
const _CQ_TIMERS = new Map();          // jobId → intervalId живого таймера ⏱
let _CQ_POLL = null;
const _CQ_TERMINAL = ['done','error','cancelled','interrupted'];

function _cqFmtElapsed(ms){ const s=Math.max(0,Math.floor(ms/1000)); return String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0'); }
function _cqCard(jobId){ return document.getElementById('cjob-'+jobId); }

// Шапка стека: «Очередь копирования · N активн. / M» + «Очистить» / «Свернуть».
function _cqChrome(){
  const stack=document.getElementById('copy-job-stack'); if(!stack) return;
  let bar=document.getElementById('copy-job-stack-bar');
  if(COPY_QUEUE.size===0){ if(bar) bar.remove(); return; }
  if(!bar){
    bar=document.createElement('div'); bar.id='copy-job-stack-bar'; bar.className='job-stack-bar';
    bar.innerHTML='<span class="job-stack-title" id="copy-job-stack-title"></span>'+
      '<span class="job-stack-actions">'+
        '<button class="job-stack-clear" onclick="clearFinishedCopyJobs()" id="copy-job-stack-clear" title="Убрать завершённые задачи">Очистить</button>'+
        '<button class="job-stack-toggle" onclick="toggleCopyJobStack()" id="copy-job-stack-toggle"></button>'+
      '</span>';
    stack.insertBefore(bar, stack.firstChild);     // шапка всегда сверху
  }
  const active=[...COPY_QUEUE.values()].filter(j=>!j.terminal).length;
  const finished=COPY_QUEUE.size-active;
  document.getElementById('copy-job-stack-title').textContent='📋 Очередь копирования · '+active+' активн. / '+COPY_QUEUE.size;
  const collapsed=stack.classList.contains('collapsed');
  document.getElementById('copy-job-stack-toggle').textContent = collapsed ? '▸ Развернуть' : '▾ Свернуть';
  const clearBtn=document.getElementById('copy-job-stack-clear');
  if(clearBtn) clearBtn.classList.toggle('visible', finished>0);
}
function toggleCopyJobStack(){
  const stack=document.getElementById('copy-job-stack'); if(!stack) return;
  const c=stack.classList.toggle('collapsed');
  try{ localStorage.setItem(COPY_QUEUE_COLLAPSE_LS, c?'1':'0'); }catch(e){}
  _cqChrome();
}
// Раскрыть стек: после F5 он свёрнут (_cqRestore), а стартующую джобу надо показать сразу.
function _cqExpand(){
  const stack=document.getElementById('copy-job-stack'); if(!stack) return;
  stack.classList.remove('collapsed');
  try{ localStorage.setItem(COPY_QUEUE_COLLAPSE_LS,'0'); }catch(e){}
  _cqChrome();
}
// Лог джобы: свёрнут по умолчанию, состояние держим на джобе — иначе поллинг (2.5 с)
// схлопывал бы раскрытый лог на каждом тике.
function toggleCopyJobLog(jobId){
  const j=COPY_QUEUE.get(jobId), card=_cqCard(jobId); if(!j||!card) return;
  j.logOpen=!j.logOpen;
  _cqApplyLogOpen(j, card);
}
function toggleCopyJobChecks(jobId){
  const j=COPY_QUEUE.get(jobId), card=_cqCard(jobId); if(!j||!card) return;
  j.checksOpen=!j.checksOpen;
  _cqApplyChecksOpen(j, card);
}
function _cqApplyLogOpen(j, card){
  const pre=card.querySelector('.job-log'), tog=card.querySelector('.job-log-toggle');
  if(!pre||!tog) return;
  pre.classList.toggle('open', !!j.logOpen);
  tog.textContent=(j.logOpen?'▾':'▸')+' Лог копирования';
  tog.setAttribute('aria-expanded', j.logOpen?'true':'false');
  if(j.logOpen) pre.scrollTop=pre.scrollHeight;   // следим за хвостом лога
}
function _cqApplyChecksOpen(j, card){
  const box=card.querySelector('.job-checks'), tog=card.querySelector('.job-checks-toggle');
  if(!box||!tog) return;
  box.classList.toggle('open', !!j.checksOpen);
  tog.textContent=(j.checksOpen?'▾':'▸')+' Проверки';
  tog.setAttribute('aria-expanded', j.checksOpen?'true':'false');
}

// Совместимость: обе вкладки зовут renderCopyStatus() ДО того, как известен job_id
// (copy_auto.js / copy_other.js — «ставлю задачу в очередь…»). Отдельной панели статуса
// больше нет: всё показывает карточка очереди (_cqUpdateCard), а карточка появится
// через _cqRegister сразу после ответа старта. Здесь только раскрываем стек, чтобы
// прогресс стартующей джобы был виден без лишнего клика.
// ⛔ Не возвращать сюда рендер статуса: одно место показа — очередь.
function renderCopyStatus(){ _cqExpand(); }
function clearFinishedCopyJobs(){
  // Сервер copy-очередь сам чистит по TTL; здесь снимаем только карточки (эндпоинта
  // отмены/удаления у :5022 нет — см. комментарий к блоку).
  [...COPY_QUEUE.values()].filter(j=>j.terminal).forEach(j=>_cqDropJob(j.jobId));
  _cqSave(); _cqChrome();
}
function _cqDropJob(jobId){
  const c=_cqCard(jobId); if(c) c.remove();
  if(_CQ_TIMERS.has(jobId)){ clearInterval(_CQ_TIMERS.get(jobId)); _CQ_TIMERS.delete(jobId); }
  COPY_QUEUE.delete(jobId);
  _cqSyncStartBtns();     // сняли карточку живой джобы (404 от сервиса) → кнопка снова активна
}
function _cqSave(){
  try{
    const arr=[...COPY_QUEUE.values()].filter(j=>!j.terminal)
      .map(j=>({jobId:j.jobId, source:j.source, login:j.login, agency:j.agency, total:j.total, tab:j.tab}));
    localStorage.setItem(COPY_QUEUE_LS, JSON.stringify(arr));
  }catch(e){}
}

// Регистрация джобы после ответа copy_start / copy_other_start.
function _cqRegister(jobId, data){
  if(!jobId) return;
  if(!COPY_QUEUE.has(jobId)) COPY_QUEUE.set(jobId, Object.assign({jobId, terminal:false, progressPct:0}, data||{}));
  else Object.assign(COPY_QUEUE.get(jobId), data||{});
  _cqRenderCard(jobId);
  _cqUpdateCard(jobId, {status:'queued', total:(data&&data.total)||0, ahead:(data&&data.ahead)||0});
  _cqExpand();                       // старт → прогресс виден сразу, без перезагрузки
  _cqSave(); _cqPollStart();
}

function _cqRenderCard(jobId){
  const j=COPY_QUEUE.get(jobId); if(!j) return;
  let card=_cqCard(jobId);
  if(!card){
    const stack=document.getElementById('copy-job-stack'); if(!stack) return;
    card=document.createElement('div'); card.className='job-card queued'; card.id='cjob-'+jobId;
    card.innerHTML='<div class="job-head"><span class="job-acc"></span>'+
      '<button class="job-cancel" onclick="cancelCopyJob(\''+jobId+'\')">✕ Отмена</button></div>'+
      '<div class="job-agency"></div>'+
      '<div class="da-progress"><div class="da-progress-bar" style="width:0"></div></div>'+
      '<div class="job-note">в очереди…</div>'+
      '<span class="job-timer" style="display:none"></span>'+
      '<div class="job-verify"></div>'+
      '<div class="job-skipped"></div>'+
      '<button class="job-checks-toggle visible" type="button" aria-expanded="false" '+
        'onclick="toggleCopyJobChecks(\''+jobId+'\')">▸ Проверки</button>'+
      '<div class="job-checks" aria-label="Проверки копирования"></div>'+
      '<button class="job-log-toggle" type="button" aria-expanded="false" '+
        'onclick="toggleCopyJobLog(\''+jobId+'\')">▸ Лог копирования</button>'+
      '<pre class="job-log" aria-label="Лог копирования"></pre>';
    stack.appendChild(card);
  }
  // Направление копирования: source→target. Логины уже с префиксом porg-. Пока source
  // неизвестен (до первого поллинга / регистрации без source) — показываем только target.
  const _tgt=j.login||'аккаунт';
  card.querySelector('.job-acc').textContent='🏢 '+(j.source? (j.source+'→'+_tgt) : _tgt);
  const ag=card.querySelector('.job-agency');
  if(ag) ag.textContent='🔑 агентство: '+(j.agency? j.agency : (j.agency===''?'автоподбор':'—'));
  _cqChrome();
}

// Шкала никогда не едет назад (как в эталоне: сервер может пересчитать total вниз).
function _cqSetProgress(j, bar, requested){
  const pct=Math.max(0,Math.min(100,Number(requested)||0));
  j.progressPct=Math.max(Number(j.progressPct)||0,pct);
  if(bar) bar.style.width=j.progressPct+'%';
}

// «Создано N/M»: N честно считаем по result.results (ok===true) — он появляется только
// у завершённой джобы. Пока идёт работа, точного счётчика в copy_status НЕТ, поэтому
// показываем ту же оценку done=total*progress/100, что сервер сам пишет в карточку
// очереди создания (copy_engine._copy_mirror_create_job) — цифры не разойдутся.
function _cqCreated(s){
  const rows=((s.result||{}).results)||[];
  if(rows.length) return rows.filter(r=>r&&r.ok).length;
  return null;
}
function _cqFailed(s){
  const rows=((s.result||{}).results)||[];
  return rows.filter(r=>r&&r.ok===false).length;
}

function _cqUpdateCard(jobId, s){
  const j=COPY_QUEUE.get(jobId), card=_cqCard(jobId); if(!j||!card) return;
  // source мог не дойти при регистрации (форма шлёт только target) — берём из статуса
  // (api_copy_status отдаёт source_login) и перерисовываем шапку с направлением source→target.
  if(s.source_login && !j.source){ j.source=s.source_login; _cqRenderCard(jobId); }
  const bar=card.querySelector('.da-progress-bar'), note=card.querySelector('.job-note'), btn=card.querySelector('.job-cancel');
  const tot=Number(s.total||j.total||0);
  const prog=Number(s.progress||0);
  const st=s.status||'queued';
  j.status=st; j.total=tot||j.total;
  card.className='job-card '+st;
  if(!_CQ_TERMINAL.includes(st)){
    j.terminal=false;
    if(btn){ btn.disabled=true; btn.textContent='✕ Отмена';
             btn.title='Остановить идущее копирование пока нельзя: у direct-copy нет эндпоинта отмены'; }
  }
  if(st==='queued'){
    _cqRenderVerify(card, null);
    _cqRenderChecks(card, j, null, st);
    _cqSetProgress(j,bar,4);
    note.textContent = (s.ahead>0) ? ('в очереди — перед вами '+s.ahead) : 'в очереди…';
  } else if(st==='running'){
    // Таймер якорим от СЕРВЕРНОГО created_at (unix, сек) — ⏱ не сбрасывается при F5.
    if(!j.runningFrom) j.runningFrom = s.created_at ? s.created_at*1000 : Date.now();
    if(!_CQ_TIMERS.has(jobId)){
      const te=card.querySelector('.job-timer');
      if(te){
        te.style.display='inline';
        te.textContent='⏱ '+_cqFmtElapsed(Date.now()-j.runningFrom);
        _CQ_TIMERS.set(jobId, setInterval(()=>{ te.textContent='⏱ '+_cqFmtElapsed(Date.now()-j.runningFrom); }, 1000));
      }
    }
    _cqRenderVerify(card, null);
    _cqRenderChecks(card, j, null, st);
    // Копирование — линейная шкала 0..100 по прогрессу движка (фазы финализации, как у
    // создания РК, здесь нет: прогресс уже учитывает докрутку Метрики/Grid).
    _cqSetProgress(j,bar, Math.max(2, prog));
    const done = tot ? Math.min(tot, Math.round(tot*prog/100)) : 0;
    const failed=_cqFailed(s);
    note.textContent='⧉ копирование кампаний: '+done+'/'+tot+(failed?(' · ❌ '+failed):'');
  } else { // done | error | cancelled | interrupted
    // Если done + (settling || repair_pending): добивка ещё идёт — НЕ финализируем.
    // settling=true  → in-memory _copy_delayed_reverify (heal ключей/sitelinks) ещё работает.
    // repair_pending=true → persistent direct_delayed_repairs (content_repair) ещё не завершилась:
    //   её выполняет демон direct-create-worker (WRONG_AUTOTARGET/COMPANY_INFO и др.).
    // Оба условия независимы; финальный рендер — только когда ОБА false.
    if(st==='done' && (s.settling || s.repair_pending)){
      j.terminal=false;
      card.className='job-card running';  // визуально «идёт» (не зелёная «готово»)
      const _repairTip = s.repair_pending
        ? 'Persistent добивка ещё идёт (content_repair — WRONG_AUTOTARGET/COMPANY_INFO)'
        : 'Добивка ещё идёт — идёт сверка и лечение ключей';
      if(btn){ btn.disabled=true; btn.textContent='✕ Отмена'; btn.title=_repairTip; }
      _cqSetProgress(j,bar,99);
      note.textContent=s.repair_pending
        ? '⏳ идёт добивка: постпроверка и ремонт контента…'
        : '⏳ идёт добивка: сверка и лечение ключей…';
      // Таймер: не останавливаем; если после F5 интервал потерян — пересоздаём от created_at.
      if(!j.runningFrom) j.runningFrom = s.created_at ? s.created_at*1000 : Date.now();
      if(!_CQ_TIMERS.has(jobId)){
        const te=card.querySelector('.job-timer');
        if(te){
          te.style.display='inline';
          te.textContent='⏱ '+_cqFmtElapsed(Date.now()-j.runningFrom);
          _CQ_TIMERS.set(jobId, setInterval(()=>{ te.textContent='⏱ '+_cqFmtElapsed(Date.now()-j.runningFrom); }, 1000));
        }
      }
      _cqRenderVerify(card, null);   // верификация покажется в финале
      _cqRenderChecks(card, j, s.result||null, 'settling');
    } else {
      // Настоящий финал: добивки нет или она завершилась.
      if(_CQ_TIMERS.has(jobId)){ clearInterval(_CQ_TIMERS.get(jobId)); _CQ_TIMERS.delete(jobId); }
      const te=card.querySelector('.job-timer'); if(te) te.style.display='none';
      j.terminal=true;
      if(btn){ btn.disabled=false; btn.textContent='✕ Убрать'; btn.title='Убрать карточку из очереди'; }
      _cqSetProgress(j,bar,100);
      const elapsed=(s.created_at&&s.updated_at)?_cqFmtElapsed((s.updated_at-s.created_at)*1000):'';
      const suffix=elapsed?(' · ⏱ '+elapsed):'';
      if(st==='done'){
        const created=_cqCreated(s);
        const failed=_cqFailed(s);
        note.innerHTML='✅ создано кампаний: '+(created==null?tot:created)+'/'+tot+
          (failed?(' · ❌ '+failed):'')+suffix+_cqExtras(s);
      } else if(st==='error'){
        note.innerHTML='❌ ошибка: '+esc(String(s.error||'—')).slice(0,180)+suffix;
      } else {
        note.textContent=(st==='cancelled'?'⛔ отменено':'⚠ прервано (рестарт сервиса)')+suffix;
      }
      _cqRenderVerify(card, s.result||null);
      _cqRenderChecks(card, j, s.result||null, st);
    }
  }
  _cqRenderLog(card, j, s);
  _cqRenderSkipped(card, s.result||null);
  _cqSyncStartBtns();
  _cqSave(); _cqChrome();
}

// Хвост done-ноты: замена фидов и итог докрутки Метрики — тоже переехали из панели
// «Цель» (renderCopyStatus в HEAD показывал их bits'ами). Оба факта живут в
// завершённой джобе: feed_map на верхнем уровне статуса, metrika — в result.
function _cqExtras(s){
  let out='';
  const fm=s.feed_map||(s.result&&s.result.feed_map);
  if(fm && Object.keys(fm).length) out+=' · замена фидов: '+Object.keys(fm).length;
  const m=s.result&&s.result.metrika;
  if(m) out+=' · метрика: '+esc(String(m.updated||0))+' ok / '+esc(String(m.warned||0))+' warn';
  return out;
}

// Лог копирования (переехал из панели «Цель»): copy_status отдаёт j.log — массив строк.
// Кнопку показываем, только когда лог непустой, чтобы карточка очереди не обрастала
// мёртвым контролом у джобы, которая ещё ничего не написала.
function _cqRenderLog(card, j, s){
  const pre=card.querySelector('.job-log'), tog=card.querySelector('.job-log-toggle');
  if(!pre||!tog) return;
  const text=(s.log||[]).join('\n');
  tog.classList.toggle('visible', !!text);
  if(!text){ pre.classList.remove('open'); pre.textContent=''; return; }
  if(pre.textContent!==text){
    const atBottom=(pre.scrollHeight-pre.scrollTop-pre.clientHeight)<24;
    pre.textContent=text;
    if(j.logOpen && atBottom) pre.scrollTop=pre.scrollHeight;
  }
  _cqApplyLogOpen(j, card);
}

// Пропущенные кампании (фид не подобран) + итог очистки целевого аккаунта —
// тоже переехали из панели «Цель». Оба факта живут в result завершённой джобы.
function _cqRenderSkipped(card, result){
  const box=card.querySelector('.job-skipped'); if(!box) return;
  const skipped=(result&&result.skipped_campaigns)||[];
  const cleanupR=(result&&result.cleanup)||null;
  let html='';
  if(skipped.length){
    html+='⚠ Пропущено '+skipped.length+' кампаний (фид не подобран): '
      +skipped.slice(0,5).map(s=>'<b>'+esc(s.name||String(s.id))+'</b>').join(', ')
      +(skipped.length>5?'…':'');
  }
  if(cleanupR){
    const parts=[];
    if(cleanupR.deleted) parts.push('удалено черновиков: <b>'+cleanupR.deleted+'</b>');
    if(cleanupR.archived) parts.push('заархивировано: <b>'+cleanupR.archived+'</b>');
    if(cleanupR.skipped_non_draft) parts.push('пропущено не-черновиков: '+cleanupR.skipped_non_draft);
    if(cleanupR.skipped_draft) parts.push('пропущено DRAFT (нельзя архивировать): '+cleanupR.skipped_draft);
    if(cleanupR.errors&&cleanupR.errors.length) parts.push('<span class="job-skipped-err">ошибок очистки: '+cleanupR.errors.length+'</span>');
    if(parts.length) html+=(html?'<br>':'')+'Очистка: '+parts.join(' · ');
  }
  box.innerHTML=html;
  box.style.display = html ? 'block' : 'none';
}

// Блок «Проверка» под карточкой. Источник — result.live_verification:
// у copy-джобы он приходит в ДВУХ формах:
//   • create_set-стиль {status:'pass'|'warn'|'fail'|'error', summary:{errors,warnings}, issues:[]}
//     (copy_engine._copy_grid_unified_steps → _create_set_live_verification);
//   • uac-стиль {status:'ok'|'warning', created, expected} (copy_engine:1316).
// Нормализуем оба в pass/warn/fail — иначе класс не подцепится и блок будет бесцветным.
function _cqVerifyStatus(v){
  const s=String((v&&v.status)||'').toLowerCase();
  if(s==='ok'||s==='pass') return 'pass';
  if(s==='warning'||s==='warn') return 'warn';
  if(s==='fail') return 'fail';
  if(s==='error') return 'error';
  return '';
}
function _cqVerifyCount(v, key){
  const s=(v&&v.summary)||{};
  if(s[key]!=null) return Number(s[key])||0;
  if(Array.isArray(v&&v.issues)) return v.issues.filter(x=>x&&x.severity===(key==='errors'?'error':'warn')).length;
  return 0;
}
function _cqDetailedVerify(result){
  const agg=_cvAggregate(result);
  if(!agg) return null;
  if(agg.error) return {st:'error', label:'❌ ошибка проверки', detail:String(agg.error).slice(0,160)};
  if(!agg.totalResults) return {st:'warn', label:'⚠ нет детальных строк', detail:'copy_verify не вернул результаты'};
  const s=agg.summary||{};
  const mismatch=Number(s.mismatch||0);
  const missing=Number(s.missing||0);
  const unreadable=Number(s.unreadable||0);
  const repairErrors=(agg.repairErrors||[]).length;
  if(mismatch||missing||repairErrors){
    const parts=[];
    if(mismatch) parts.push('расхождений '+mismatch);
    if(missing) parts.push('не создано '+missing);
    if(repairErrors) parts.push('ошибок ремонта '+repairErrors);
    return {st:'fail', label:'❌ нужна добивка', detail:parts.join(' · ')};
  }
  if(unreadable){
    return {st:'warn', label:'⚠ частично проверена', detail:'не прочитано '+unreadable};
  }
  return {st:'pass', label:'✅ пройдена', detail:'детальная сверка совпала'};
}
function _cqRenderVerify(card, result){
  const box=card&&card.querySelector('.job-verify'); if(!box) return;
  const v=result&&result.live_verification;
  const detailed=_cqDetailedVerify(result);
  if(!v&&!detailed){ box.style.display='none'; box.textContent=''; return; }
  const st=detailed ? detailed.st : _cqVerifyStatus(v);
  const errors=_cqVerifyCount(v,'errors'), warnings=_cqVerifyCount(v,'warnings');
  const label = detailed ? detailed.label
              : st==='pass' ? '✅ пройдена'
              : st==='warn' ? '⚠ есть предупреждения'
              : st==='fail' ? '❌ нужна добивка'
              : st==='error' ? '❌ ошибка проверки' : 'статус неизвестен';
  let html='Проверка: <b>'+label+'</b>';
  if(detailed&&detailed.detail) html+=' · '+esc(detailed.detail);
  if(errors||warnings) html+=' · live: ошибок '+errors+' · предупреждений '+warnings;
  // uac-форма несёт created/expected вместо summary — показываем факт по кабинету.
  if(v&&v.created!=null&&v.expected!=null) html+=' · создано '+v.created+' из '+v.expected;
  html+='<small>Факт сверялся по кабинету после копирования. Детальный чеклист ниже имеет приоритет над общей live-проверкой.</small>';
  box.className='job-verify '+st;
  box.innerHTML=html;
  box.style.display='block';
}

// ── #12: живой отчёт сверки источник↔цель (copy_verify) + авто-ремонт (copy_repair) ──
// В отличие от live_verification (общий pass/fail от direct_copy), это 19-мерная
// посёлочная сверка из copy_verify.py. Рендерим на карточке завершённой джобы и
// показываем факты в карточке очереди, во вкладке «Проверки».
const _CV_LABELS={
  campaign_exists:'Кампания создана', adgroup_count:'Количество групп',
  keyword_count:'Ключевые слова', campaign_neg_count:'Минус-слова кампании',
  group_neg_count:'Минус-слова группы', shared_set_count:'Библиотеки минус-слов',
  promo_attached:'Промоакции', adaptive_titles_count:'Заголовки',
  adaptive_bodies_count:'Тексты', callout_count:'Уточнения',
  sitelinks_present:'Быстрые ссылки',
  sitelinks_campaign_level_present:'Быстрые ссылки (кампания)',
  sitelinks_ad_level_count:'Быстрые ссылки (объявления)',
  ads_with_images:'Изображения',
  audiences:'Аудитории', bid_modifiers:'Корректировки ставок',
  strategy_name:'Стратегия', ads_with_video:'Видео', button_cta:'Кнопки (CTA)',
  ad_price:'Цена (adPrice)', utm_tracking:'UTM-метка',
  site_monitoring:'Мониторинг сайта', minus_places:'Минус-площадки',
  shopping_filter_count:'Товарные объявления',
  listing_filter_count:'Каталожные объявления',
  shopping_filter_signature:'Фильтры товарных объявлений',
  listing_filter_signature:'Фильтры каталожных объявлений',
  geo_kw_source_residual:'Гео в ключах (остаток источника)',
  geo_neg_target_blocked:'Гео в минусах (целевой город)',
};
// [glyph, css-класс, подпись]
const _CV_ST={ ok:['✓','ok','совпадает'], mismatch:['✗','bad','расхождение'],
  missing:['✗','bad','не создано'], unreadable:['?','warn','не прочитано'],
  excluded_intentional:['—','muted','намеренно не сверяется'] };
const _CV_RANK={ mismatch:4, missing:4, unreadable:2, ok:1, excluded_intentional:0 };
// связь пункта статичного чеклиста → dimension (для аннотации кнопки-чеклиста)
const _CV_ITEM_DIM={
  'Количество групп':'adgroup_count', 'Ключевые слова':'keyword_count',
  'Минус-слова (кампания и группа)':['campaign_neg_count','group_neg_count'],
  'Библиотеки минус-слов':'shared_set_count',
  'Заголовки':'adaptive_titles_count', 'Тексты':'adaptive_bodies_count',
  'Уточнения':'callout_count',
  'Быстрые ссылки':['sitelinks_present','sitelinks_campaign_level_present','sitelinks_ad_level_count'],
  'Изображения':'ads_with_images', 'Видео':'ads_with_video',
  'Кнопки (CTA)':'button_cta', 'Стратегия (тип и настройки)':'strategy_name',
  'Корректировки ставок':'bid_modifiers', 'UTM-метка':'utm_tracking',
  'Мониторинг сайта':'site_monitoring', 'Минус-площадки':'minus_places',
  'Промоакции':'promo_attached',
  'Фильтры товарных и каталожных объявлений':[
    'shopping_filter_count','listing_filter_count',
    'shopping_filter_signature','listing_filter_signature'
  ],
  'Фильтры товарных':['shopping_filter_count','listing_filter_count','shopping_filter_signature','listing_filter_signature'],
  'Аудитории':'audiences',
};
function _cvItemDims(text){
  let dim=_CV_ITEM_DIM[text];
  if(!dim){
    for(const k in _CV_ITEM_DIM){ if(text.indexOf(k)===0){ dim=_CV_ITEM_DIM[k]; break; } }
  }
  if(!dim) return [];
  return Array.isArray(dim)?dim:[dim];
}
function _cvGroupForDims(agg, dims){
  if(!agg||!dims||!dims.length) return null;
  const out={ok:0,mismatch:0,missing:0,unreadable:0,excluded_intentional:0,worst:'excluded_intentional'};
  let found=false;
  dims.forEach(d=>{
    const g=agg.byDim[d]; if(!g) return;
    found=true;
    ['ok','mismatch','missing','unreadable','excluded_intentional'].forEach(k=>{ out[k]+=(g[k]||0); });
    if((_CV_RANK[g.worst]||0)>(_CV_RANK[out.worst]||0)) out.worst=g.worst;
  });
  return found?out:null;
}
function _cvRepairCountForDims(agg, dims){
  if(!agg||!dims||!dims.length) return 0;
  return dims.reduce((n,d)=>n+(agg.repairedDims[d]||0),0);
}
function _cvChecklistHtml(result, phase){
  const agg=_cvAggregate(result);
  const pending=phase==='queued'?'ожидает очереди':(phase==='running'?'копирование ещё идёт':'ожидает проверки');
  return (_COPY_CHECKLIST||[]).map(([group,items])=>{
    const rows=(items||[]).map(text=>{
      const dims=_cvItemDims(text);
      const g=_cvGroupForDims(agg, dims);
      if(!g){
        return '<div class="copy-check-item"><span class="mk muted">·</span><span>'+esc(text)+' <small>('+esc(pending)+')</small></span></div>';
      }
      const st=_CV_ST[g.worst]||_CV_ST.unreadable;
      const rep=_cvRepairCountForDims(agg, dims);
      const tail=rep?' <span class="cv-rep">отремонтировано '+rep+'</span>':'';
      return '<div class="copy-check-item"><span class="mk '+st[1]+'">'+st[0]+'</span><span>'+esc(text)+' <small>('+esc(st[2])+')</small>'+tail+'</span></div>';
    }).join('');
    return '<div class="copy-check-grp">'+esc(group)+'</div>'+rows;
  }).join('');
}
function _cqRenderChecks(card, j, result, phase){
  const box=card&&card.querySelector('.job-checks');
  const tog=card&&card.querySelector('.job-checks-toggle');
  if(!box||!tog) return;
  tog.classList.add('visible');
  box.innerHTML=_cvChecklistHtml(result, phase);
  _cqApplyChecksOpen(j, card);
}
// Агрегация построчных результатов по dimension (по всем кампаниям): худший статус + счётчики.
function _cvAggregate(result){
  if(!result) return null;
  // copy_verify/copy_repair кладутся движком в result.cookie_postprocess (ЕПК/cookie-путь);
  // на верхнем уровне их обычно нет. Берём из обоих мест (верх приоритетнее для совместимости).
  const cpp=result.cookie_postprocess||{};
  const cv=result.copy_verify||cpp.copy_verify; if(!cv||!cv.results) return null;
  const rep=result.copy_repair||cpp.copy_repair||{};
  const repairedDims={};
  ((rep.repairs)||[]).forEach(r=>{ if(r&&r.dimension) repairedDims[r.dimension]=(repairedDims[r.dimension]||0)+1; });
  const byDim={};
  cv.results.forEach(r=>{
    const d=r.dimension; if(!d) return;
    const st=r.status||'unreadable';
    if(!byDim[d]) byDim[d]={ok:0,mismatch:0,missing:0,unreadable:0,excluded_intentional:0,worst:'excluded_intentional'};
    byDim[d][st]=(byDim[d][st]||0)+1;
    if((_CV_RANK[st]||0)>(_CV_RANK[byDim[d].worst]||0)) byDim[d].worst=st;
  });
  return { byDim, repairedDims, summary:cv.summary||{}, repairErrors:(rep.errors||[]),
           error:cv.error||'', totalResults:cv.results.length };
}
async function cancelCopyJob(jobId){
  const j=COPY_QUEUE.get(jobId); if(!j) return;
  // Завершённая → «Убрать»: просто снимаем карточку (серверная джоба уже не работает).
  if(j.terminal){ _cqDropJob(jobId); _cqSave(); _cqChrome(); return; }
  // Идущая: отменять нечем — кнопка задизаблена, сюда попасть нельзя. Guard на всякий.
  alert('Остановить идущее копирование пока нельзя: у сервиса нет эндпоинта отмены.');
}

// Один поллер на все джобы очереди. Очередь — единственный потребитель copy_status:
// карточка сама показывает прогресс, лог и пропущенные кампании.
async function _cqTick(){
  const ids=[...COPY_QUEUE.values()].filter(j=>!j.terminal).map(j=>j.jobId);
  if(!ids.length){ _cqPollStop(); return; }
  for(const id of ids){
    try{
      const r=await fetch('/direct/api/copy_status/'+encodeURIComponent(id));
      if(r.status===404){ _cqDropJob(id); _cqSave(); _cqChrome(); continue; }
      _cqUpdateCard(id, await r.json());
    }catch(e){}
  }
  if(![...COPY_QUEUE.values()].some(j=>!j.terminal)) _cqPollStop();
}
function _cqPollStart(){ if(_CQ_POLL) return; _CQ_POLL=setInterval(_cqTick, 2500); _cqTick(); }
function _cqPollStop(){ if(_CQ_POLL){ clearInterval(_CQ_POLL); _CQ_POLL=null; } }
// Совместимость: обе вкладки зовут pollCopyJob() сразу после запуска джобы.
function pollCopyJob(){ return _cqTick(); }

function _cqRestore(){
  const stack=document.getElementById('copy-job-stack'); if(!stack) return;
  // Безопасный UI-дефолт (как в эталоне): после reload очередь компактна и не перекрывает форму.
  stack.classList.add('collapsed');
  try{ localStorage.setItem(COPY_QUEUE_COLLAPSE_LS,'1'); }catch(e){}
  try{
    const arr=JSON.parse(localStorage.getItem(COPY_QUEUE_LS)||'[]');
    arr.forEach(d=>{
      if(!d||!d.jobId||COPY_QUEUE.has(d.jobId)) return;
      COPY_QUEUE.set(d.jobId, Object.assign({terminal:false, progressPct:0}, d));
      _cqRenderCard(d.jobId);
    });
  }catch(e){}
  // Кнопка «Копировать РК» вкладки с живой джобой должна остаться заблокированной и после F5.
  _cqSyncStartBtns();
  _cqChrome();
  if(COPY_QUEUE.size) _cqPollStart();   // живые джобы догрузят факты; мёртвые (404) снимутся сами
}

// ── Вкладки ───────────────────────────────────────────────────────────────────
// Обе вкладки физически живут в РАЗНЫХ шаблонах (copy.html / copy_other.html), но
// рендерятся в один документ: у direct-copy.service единственный роут /automation/copy
// (nginx отдаёт на :5022 только ^~ /direct/automation/copy и ^~ /direct/api/copy_),
// отдельного URL под «Прочие сферы» нет. Переключение остаётся клиентским.
function _switchTab(tab){
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.toggle('active',b.getAttribute('data-tab')===tab));
  const autoCard=document.getElementById('auto-right-card');
  const otherCard=document.getElementById('other-right-card');
  const feedsCard=document.getElementById('copy-feeds-card');
  if(autoCard) autoCard.style.display = tab==='auto'?'':'none';
  if(otherCard) otherCard.style.display = tab==='other'?'':'none';
  if(feedsCard) feedsCard.style.display = tab==='auto'?'':'none';
  if(tab==='other' && typeof _GEO_REGIONS_LOADED!=='undefined' && !_GEO_REGIONS_LOADED
     && typeof loadOtherGeoRegions==='function') loadOtherGeoRegions();
  // применяем режим картинок (по умолчанию 1в1 → зона загрузки скрыта)
  if(tab==='other' && typeof otherImgModeChange==='function') otherImgModeChange();
}

loadAccounts();
document.addEventListener('DOMContentLoaded', _cqRestore);

// ── Хелпер сопоставления фидов (доступен в обеих вкладках) ──────────────────
// Зеркало server-side _feed_auto_match_one (copy_other.py). Правило 1: полный путь
// URL без домена; правило 2: имя файла без query. Возвращает String(tgt.id) или null.
function _feedMatchFromList(srcFeed, tgtList){
  const srcUrl = srcFeed.name || '';
  const srcPath = srcUrl.replace(/^https?:\/\/[^/]+/, '').toLowerCase();
  const srcFile = (srcUrl.split('/').pop() || '').split('?')[0].toLowerCase();
  for(const t of tgtList){
    const tp = (t.name || '').replace(/^https?:\/\/[^/]+/, '').toLowerCase();
    if(srcPath && tp && srcPath === tp) return String(t.id);
  }
  for(const t of tgtList){
    const tf = ((t.name || '').split('/').pop() || '').split('?')[0].toLowerCase();
    if(srcFile && tf && srcFile === tf) return String(t.id);
  }
  return null;
}

// Как _feedMatchFromList, но возвращает {id, rule} где rule='path'|'file'. Для отображения
// правила совпадения в UI (колонка «Правило» в таблице фидов обеих вкладок).
function _feedMatchFromListDetailed(srcFeed, tgtList){
  const srcUrl = srcFeed.name || '';
  const srcPath = srcUrl.replace(/^https?:\/\/[^/]+/, '').toLowerCase();
  const srcFile = (srcUrl.split('/').pop() || '').split('?')[0].toLowerCase();
  for(const t of tgtList){
    const tp = (t.name || '').replace(/^https?:\/\/[^/]+/, '').toLowerCase();
    if(srcPath && tp && srcPath === tp) return {id:String(t.id), rule:'path'};
  }
  for(const t of tgtList){
    const tf = ((t.name || '').split('/').pop() || '').split('?')[0].toLowerCase();
    if(srcFile && tf && srcFile === tf) return {id:String(t.id), rule:'file'};
  }
  return null;
}

// ── Чеклист «что проверяется при копировании» (view-only, обе вкладки) ────────
// Сверка «источник ↔ цель» по неизменяемым параметрам после копирования. Гео/домен/
// фиды/Метрика меняются намеренно и в сверку не входят. Список — для просмотра.
// При изменении copy_verify/copy_postprocess обновлять _COPY_CHECKLIST, _COPY_CHANGELIST
// и _CV_ITEM_DIM вместе: статичная справка и чеклист очереди должны описывать один код.
const _COPY_CHECKLIST=[
  ['Структура', ['Количество групп','Ключевые слова','Минус-слова (кампания и группа)','Библиотеки минус-слов (созданы до кампаний и привязаны)','Промоакции (созданы до кампаний и привязаны)']],
  ['Контент объявлений', ['Заголовки','Тексты','Уточнения','Быстрые ссылки: на уровне кампании и на уровне объявлений','Изображения','Видео','Кнопки (CTA)']],
  ['Настройки кампании', ['Стратегия (тип и настройки)','Корректировки ставок','UTM-метка','Мониторинг сайта','Минус-площадки']],
  ['Товарные и динамические', ['Фильтры товарных и каталожных объявлений','Аудитории']],
];
const _COPY_CHANGELIST=[
  ['Идентичность копии', ['Создаются новые кампании, группы, объявления и связанные сущности в целевом аккаунте','Имена кампаний нормализуются под целевой аккаунт и кодер','ID всех объектов меняются на новые ID цели']],
  ['Целевой сайт и гео', ['Домен, ссылки и UTM приводятся к целевому сайту','Гео меняется на выбранный город/регион или сохраняется без изменения','Тексты и ключевые фразы проходят гео-морфологическую замену, если выбран режим смены гео']],
  ['Метрика, фиды и библиотеки', ['Счётчик Метрики и цель «Все формы» ставятся целевые','Фиды заменяются на выбранные целевые фиды или общий фид аккаунта','Библиотеки минус-слов и промоакции заранее создаются в целевом аккаунте и затем привязываются к новым кампаниям','Цена adPrice пересчитывается заново из фида целевого аккаунта']],
  ['Стандарты после копирования', ['Возрастные корректировки <18 и 18–24 ставятся -100%','Инварианты поиска и часть настроек, доступных только через редактор Директа, дозакрываются постпроцессом']],
];
// Цена (adPrice) НЕ сверяется источник↔цель — ставится заново из фида целевого аккаунта
// (как в сервисе создания РК). Показываем это отдельной сноской, не пунктом сверки.
const _COPY_CHECK_NOTE='Цена (adPrice) не сверяется — устанавливается заново из фида целевого аккаунта, как при создании кампаний.';
const _COPY_CHANGE_NOTE='Список отражает намеренные изменения copy-пайплайна: эти пункты не считаются ошибками в проверке source↔target.';
function _showCopyInfoModal(title, subTxt, groups, note, annotate){
  const ov=document.createElement('div');
  ov.className='copy-check-ov';
  ov.onclick=(e)=>{ if(e.target===ov) ov.remove(); };
  const html=(groups||[]).map(([g,items])=>
    '<div class="copy-check-grp">'+esc(g)+'</div>'+
    items.map(t=>'<div class="copy-check-item">'+annotate(t)+'</div>').join('')
  ).join('');
  ov.innerHTML='<div class="copy-check-modal" role="dialog" aria-modal="true">'
    +'<h3>'+esc(title)+'</h3>'
    +'<div class="sub">'+esc(subTxt)+'</div>'
    +html
    +'<div class="copy-check-foot">'+esc(note)+'</div>'
    +'<button type="button" class="copy-check-close" onclick="this.closest(\'.copy-check-ov\').remove()">Закрыть</button>'
    +'</div>';
  document.body.appendChild(ov);
}
function showCopyChecklist(){
  const annotate=(t)=>'<span class="mk">✓</span><span>'+esc(t)+'</span>';
  const subTxt='Сверка «источник ↔ цель» по неизменяемым параметрам. Гео, домен, фиды и Метрика меняются намеренно — в сверку не входят.';
  _showCopyInfoModal(
    'Что проверяется при копировании',
    subTxt, _COPY_CHECKLIST, _COPY_CHECK_NOTE, annotate
  );
}
function showCopyChanges(){
  _showCopyInfoModal(
    'Что меняется при копировании',
    'Эти изменения выполняются намеренно, чтобы копия работала в новом аккаунте и на новом сайте.',
    _COPY_CHANGELIST,
    _COPY_CHANGE_NOTE,
    (t)=>'<span class="mk">✓</span><span>'+esc(t)+'</span>'
  );
}

// ── Модалка подтверждения (центр экрана, стиль сайта) — заменяет нативный confirm() ──
// Переиспользует канонический компонент .modal/.modal-box/.modal-actions/.btn из style.css
// (спец. DESIGN.md → «Модалки»). opts.warn — подсвеченный блок деструктивного действия
// («удалить … необратимо», стиль — .modal-warn в copy_common.css на токенах --warning-*).
// Возвращает Promise<bool>: true = подтвердить, false = отмена/Esc/клик по фону.
function _cpModal(opts){
  return new Promise(resolve=>{
    const isConfirm = opts.cancel !== null;
    const ov=document.createElement('div'); ov.className='modal';
    const box=document.createElement('div'); box.className='modal-box'; box.setAttribute('role','dialog'); box.setAttribute('aria-modal','true');
    const h=document.createElement('h3'); h.textContent=opts.title||(isConfirm?'Подтвердите действие':'Уведомление'); box.appendChild(h);
    const msg=document.createElement('div'); msg.className='modal-msg'; msg.textContent=opts.message; box.appendChild(msg);
    if(opts.warn){ const w=document.createElement('div'); w.className='modal-warn'; w.textContent='⚠ '+opts.warn; box.appendChild(w); }
    const act=document.createElement('div'); act.className='modal-actions';
    let done=false;
    function close(val){ if(done) return; done=true; document.removeEventListener('keydown',onKey); ov.remove(); resolve(val); }
    if(isConfirm){ const c=document.createElement('button'); c.type='button'; c.className='btn'; c.textContent=opts.cancel||'Отмена'; c.onclick=()=>close(false); act.appendChild(c); }
    const ok=document.createElement('button'); ok.type='button'; ok.className='btn btn-primary'; ok.textContent=opts.confirm||'OK'; ok.onclick=()=>close(true); act.appendChild(ok);
    box.appendChild(act); ov.appendChild(box); document.body.appendChild(ov);
    ov.addEventListener('mousedown',e=>{ if(e.target===ov) close(isConfirm?false:true); });   // клик по фону = отмена
    function onKey(e){ if(e.key==='Escape'){ close(isConfirm?false:true); } else if(e.key==='Enter'){ e.preventDefault(); close(true); } }
    document.addEventListener('keydown',onKey);
    setTimeout(()=>{ try{ ok.focus(); }catch(_){} },30);
  });
}
function uiConfirm(opts){ opts=opts||{}; if(typeof opts==='string') opts={message:opts}; return _cpModal({title:opts.title, message:opts.message, warn:opts.warn, confirm:opts.confirm||'Да', cancel:opts.cancel||'Отмена'}); }
