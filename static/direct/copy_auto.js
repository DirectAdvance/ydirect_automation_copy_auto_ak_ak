/* ============================================================================
   copy_auto.js — ТОЛЬКО вкладка «Авто» (templates/direct/copy.html).

   Цель + замена фидов + запуск POST /direct/api/copy_start.
   Общее (Источник, COPY_SELECTED, поллинг, очередь, renderCopyStatus) —
   в copy_common.js. Не копировать сюда поллинг.
   ============================================================================ */

// ── Автоподсказка целевого логина ────────────────────────────────────────────
function copyTargetSuggest(){ _copySuggest('copy-target-login','copy-target-dd'); }
function copyTargetHide(){ _copyHide('copy-target-dd'); }
function copyTargetKey(e){ _copyKey('copy-target-dd',e); }

async function loadCopyTargetPrefill(){
  const login=((document.getElementById('copy-target-login')||{}).value||'').trim();
  const note=document.getElementById('copy-target-note');
  _loadCopyTargetCampaigns(login);   // кампании цели для блока «Существующие кампании в цели»
  if(!login){ if(note) note.textContent=''; return; }
  if(note) note.textContent='Подставляю домен / гео / Метрику…';
  try{
    const j=await (await fetch('/direct/api/copy_target_prefill?login='+encodeURIComponent(login))).json();
    if(j.error){ if(note) note.innerHTML='<span style="color:#e3b341">'+esc(j.error)+'</span>'; return; }
    document.getElementById('copy-target-domain').value=j.domain||'';
    document.getElementById('copy-target-city').value=j.city||'';
    document.getElementById('copy-target-region').value=j.region||'';
    document.getElementById('copy-counter').value=j.counter_id||'';
    document.getElementById('copy-goal').value=j.goal_id||'';
    if(note){
      const warns=(j.warnings||[]).length?(' · ⚠ '+j.warnings.join('; ')):'';
      note.textContent=(j.found===false?'В БД не найден — часть полей нужно заполнить вручную':'Подставлено из БД')+warns;
    }
  }catch(e){ if(note) note.innerHTML='<span style="color:#ff7b72">Ошибка: '+esc(String(e))+'</span>'; }
}

function copyResolveGoal(){ return _copyResolveGoalInto('copy-counter','copy-goal'); }

// ── Очистка целевого аккаунта (перенос из «Прочих»): кампании цели + вкл/выкл кнопок ──
let _COPY_TARGET_CAMPS_INFO = null;

async function _loadCopyTargetCampaigns(login){
  const infoEl=document.getElementById('copy-cleanup-info');
  // Сбрасываем cleanup СРАЗУ: пока грузятся кампании нового аккаунта, delete/archive недоступны и
  // radio на 'none' — иначе клик «Копировать» до ответа применит очистку по устаревшим данным (race).
  _COPY_TARGET_CAMPS_INFO=null;
  _copyUpdateCleanupButtons();
  if(!login){
    if(infoEl) infoEl.textContent='Укажите целевой аккаунт для загрузки статистики.';
    return;
  }
  if(infoEl) infoEl.textContent='Загружаю кампании цели…';
  try{
    const j=await (await fetch('/direct/api/copy_target_campaigns?login='+encodeURIComponent(login))).json();
    // Пользователь мог сменить аккаунт, пока шёл запрос — игнорируем устаревший ответ.
    if((((document.getElementById('copy-target-login')||{}).value||'').trim())!==login) return;
    if(j.error){
      if(infoEl) infoEl.innerHTML='<span style="color:#e3b341">⚠ '+esc(j.error)+'</span>';
      _COPY_TARGET_CAMPS_INFO=null;
    } else {
      _COPY_TARGET_CAMPS_INFO=j;
      const t=j.total||0, d=j.draft_count||0, nd=j.non_draft_count||0;
      if(infoEl) infoEl.textContent=
        t===0 ? 'Кампаний нет — аккаунт чист'
               : 'на аккаунте '+t+' кампаний: '+d+' черновиков, '+nd+' не-черновиков';
    }
  }catch(e){
    if(infoEl) infoEl.innerHTML='<span style="color:#ff7b72">Ошибка: '+esc(String(e))+'</span>';
    _COPY_TARGET_CAMPS_INFO=null;
  }
  _copyUpdateCleanupButtons();
}

// Кнопки «удалить черновики» / «в архив» доступны только если у цели есть соответствующие кампании.
function _copyUpdateCleanupButtons(){
  const info=_COPY_TARGET_CAMPS_INFO;
  const hasDrafts = info && (info.draft_count||0)>0;
  const hasArchiv = info && (info.archivable_count||0)>0;
  const hasAny    = info && (info.total||0)>0;
  const delLabel=document.getElementById('copy-cleanup-del-label');
  const arcLabel=document.getElementById('copy-cleanup-arc-label');
  const delInput=delLabel&&delLabel.querySelector('input');
  const arcInput=arcLabel&&arcLabel.querySelector('input');
  if(delLabel){ const on=hasAny&&hasDrafts; delLabel.classList.toggle('cleanup-disabled',!on); if(delInput) delInput.disabled=!on; }
  if(arcLabel){ const on=hasAny&&hasArchiv; arcLabel.classList.toggle('cleanup-disabled',!on); if(arcInput) arcInput.disabled=!on; }
  // Если выбранная опция стала недоступна — сброс на 'none'
  const sel=document.querySelector('input[name="copy-cleanup"]:checked');
  if(sel&&sel.value==='delete_drafts'&&!(hasAny&&hasDrafts)){
    const none=document.querySelector('input[name="copy-cleanup"][value="none"]'); if(none) none.checked=true;
  }
  if(sel&&sel.value==='archive'&&!(hasAny&&hasArchiv)){
    const none=document.querySelector('input[name="copy-cleanup"][value="none"]'); if(none) none.checked=true;
  }
}

// ── Замена фидов (пофидово: исходный фид → фид нового аккаунта) ──
let COPY_TARGET_FEEDS=[];
let _copySourceFeedsCache = [];    // кэш исходных фидов для автоподбора 1:1

async function loadCopyFeeds(){
  const source_login=((document.getElementById('copy-source-login')||{}).value||'').trim();
  const target_login=((document.getElementById('copy-target-login')||{}).value||'').trim();
  const box=document.getElementById('copy-feeds-box'), note=document.getElementById('copy-feeds-note');
  if(!source_login || !target_login){ alert('Укажите старый и новый аккаунт'); return; }
  note.textContent='Загружаю фиды…'; box.innerHTML='';
  try{
    const j=await (await fetch('/direct/api/copy_feeds_preview',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({source_login, target_login, campaign_ids:Array.from(COPY_SELECTED)})})).json();
    if(j.error){ note.innerHTML='<span class="err">'+esc(j.error)+'</span>'; return; }
    COPY_TARGET_FEEDS=j.target_feeds||[];
    renderCopyFeeds(j.source_feeds||[]);
    note.textContent='Исходных фидов: '+(j.source_feeds||[]).length+' · фидов нового аккаунта: '+COPY_TARGET_FEEDS.length;
  }catch(e){ note.innerHTML='<span class="err">Ошибка: '+esc(String(e))+'</span>'; }
}

// Автоподбор целевого фида 1:1 по совпадению пути URL / имени файла
// (делегирует в _feedMatchFromList из copy_common.js)
function _feedMatchTarget(srcFeed){ return _feedMatchFromList(srcFeed, COPY_TARGET_FEEDS); }
function copyFeedAutoMatch(srcFeedId){
  const sel = document.querySelector('.copy-feed-sel[data-src="'+srcFeedId+'"]');
  if(!sel) return;
  const srcFeed = _copySourceFeedsCache.find(f => String(f.id) === String(srcFeedId));
  if(!srcFeed){ alert('Фид не найден в кэше — обновите страницу фидов'); return; }
  const match = _feedMatchTarget(srcFeed);
  if(match){ sel.value = match; }
  else { alert('Совпадение по URL не найдено — выберите целевой фид вручную'); }
}

// ── UX «фид отсутствует в цели»: replace / upload / skip ─────────────────────
// Хранит выбор пользователя для каждого «отсутствующего» фида.
// Структура: { srcId (string): { mode: 'replace'|'upload'|'skip', tgt_id: int|null } }
const _FEED_MISSING_CHOICES = {};

function _feedMissingInit(srcId){
  if(!_FEED_MISSING_CHOICES[srcId]) _FEED_MISSING_CHOICES[srcId]={mode:'upload',tgt_id:null};
}

// Переключение режима для отсутствующего фида (вызывается radio onchange).
function copyFeedMissingMode(srcId, mode){
  _feedMissingInit(srcId);
  _FEED_MISSING_CHOICES[srcId].mode = mode;
  // Показываем нужный блок
  const rEl=document.getElementById('cfmiss-replace-'+srcId);
  const uEl=document.getElementById('cfmiss-upload-'+srcId);
  const sEl=document.getElementById('cfmiss-skip-'+srcId);
  if(rEl) rEl.style.display = mode==='replace' ? '' : 'none';
  if(uEl) uEl.style.display = mode==='upload'  ? '' : 'none';
  if(sEl) sEl.style.display = mode==='skip'    ? '' : 'none';
}

// Загружает фид в целевой аккаунт через POST /direct/api/copy_feed_upload.
async function copyFeedUploadAction(srcId){
  _feedMissingInit(srcId);
  const source_login=((document.getElementById('copy-source-login')||{}).value||'').trim();
  const target_login=((document.getElementById('copy-target-login')||{}).value||'').trim();
  const target_domain=((document.getElementById('copy-target-domain')||{}).value||'').trim();
  if(!source_login||!target_login||!target_domain){
    alert('Укажите старый аккаунт, новый аккаунт и домен цели перед загрузкой фида');
    return;
  }
  const noteEl=document.getElementById('cfmiss-upload-note-'+srcId);
  const btn=document.getElementById('cfmiss-upload-btn-'+srcId);
  if(noteEl) noteEl.textContent='Загружаю фид…';
  if(btn) btn.disabled=true;
  try{
    const resp=await fetch('/direct/api/copy_feed_upload',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({source_login, target_login,
                           source_feed_id:parseInt(srcId,10), target_domain})
    });
    const j=await resp.json();
    if(j.error){
      if(noteEl) noteEl.innerHTML='<span class="err">'+esc(j.error)+'</span>';
      if(btn) btn.disabled=false;
      return;
    }
    // Успех: сохраняем новый feed_id и показываем результат
    _FEED_MISSING_CHOICES[srcId]={mode:'upload',tgt_id:j.feed_id};
    if(noteEl) noteEl.innerHTML='<span style="color:#3fb950">✓ загружен фид #'+esc(String(j.feed_id))+' · '+esc(j.url||j.name)+'</span>';
    if(btn){ btn.disabled=true; btn.textContent='✓ Загружен'; }
  }catch(e){
    if(noteEl) noteEl.innerHTML='<span class="err">Ошибка: '+esc(String(e))+'</span>';
    if(btn) btn.disabled=false;
  }
}

// Склонение числительного для кол-ва кампаний/групп
function _plural(n, one, few, many){ return n+' '+(n%10===1&&n%100!==11?one:n%10>=2&&n%10<=4&&(n%100<10||n%100>=20)?few:many); }

function renderCopyFeeds(sourceFeeds){
  _copySourceFeedsCache = sourceFeeds;
  // Сброс выборов предыдущего рендера (новые аккаунты / пересчёт фидов)
  Object.keys(_FEED_MISSING_CHOICES).forEach(k=>delete _FEED_MISSING_CHOICES[k]);
  const box=document.getElementById('copy-feeds-box');
  if(!sourceFeeds.length){ box.innerHTML='<div class="copy-note" style="padding:10px 0">У исходного аккаунта нет фидов.</div>'; return; }
  const tgtOpts='<option value="">— выберите фид цели —</option>'
    +COPY_TARGET_FEEDS.map(f=>'<option value="'+esc(String(f.id))+'">'+esc(f.name)+' (#'+esc(String(f.id))+')</option>').join('');
  const head='<div class="copy-feed-row copy-feed-head"><div>Исходный фид</div><div>→ Фид нового аккаунта</div></div>';
  box.innerHTML=head+sourceFeeds.map(f=>{
    const sid=String(f.id);
    const usageParts=[];
    if(f.campaigns) usageParts.push(_plural(f.campaigns,'кампания','кампании','кампаний'));
    if(f.groups) usageParts.push(_plural(f.groups,'группа','группы','групп'));
    const usage=usageParts.length
      ? ' · <span style="color:var(--violet-400,#8b7cff)">'+usageParts.join(' / ')+'</span>'
      : '';
    const infoHtml='<div><b>'+esc(f.name)+'</b><br><span class="copy-note">#'+esc(sid)+usage+'</span></div>';
    const autoMatchResult=_feedMatchFromListDetailed(f, COPY_TARGET_FEEDS);
    if(autoMatchResult){
      // Фид НАЙДЕН в цели: обычная строка с дропдауном, совпадение предвыбрано
      const autoMatch=autoMatchResult.id;
      const ruleLabel=autoMatchResult.rule==='path'?'по пути URL':'по имени файла';
      const preOpts='<option value="">— не менять —</option>'
        +COPY_TARGET_FEEDS.map(tf=>'<option value="'+esc(String(tf.id))+'"'
          +(String(tf.id)===autoMatch?' selected':'')+'>'+esc(tf.name)+' (#'+esc(String(tf.id))+')</option>').join('');
      return '<div class="copy-feed-row">'
        +infoHtml
        +'<div class="copy-feed-actions">'
        +'<button class="copy-11-btn" type="button" onclick="copyFeedAutoMatch(\''+sid+'\')">1:1</button>'
        +'<select class="copy-feed-sel" data-src="'+esc(sid)+'">'+preOpts+'</select>'
        +'<span class="copy-note" style="font-size:10px">совпадение '+ruleLabel+'</span>'
        +'</div>'
        +'</div>';
    }
    // Фид ОТСУТСТВУЕТ в цели: блок выбора replace / upload / skip
    _FEED_MISSING_CHOICES[sid]={mode:'upload', tgt_id:null};
    return '<div class="copy-feed-row cf-missing-row">'
      +infoHtml
      +'<div class="cf-missing-wrap">'
      // Радио-группа
      +'<div class="cf-missing-radios">'
      +'<label><input type="radio" name="cfmiss-'+sid+'" value="replace" onchange="copyFeedMissingMode(\''+sid+'\',\'replace\')"> Заменить на</label>'
      +'<label><input type="radio" name="cfmiss-'+sid+'" value="upload" checked onchange="copyFeedMissingMode(\''+sid+'\',\'upload\')"> Загрузить в цель</label>'
      +'<label><input type="radio" name="cfmiss-'+sid+'" value="skip" onchange="copyFeedMissingMode(\''+sid+'\',\'skip\')"> Пропустить</label>'
      +'</div>'
      // replace: дропдаун целевых фидов
      +'<div id="cfmiss-replace-'+sid+'" style="display:none">'
      +'<select class="copy-feed-sel" data-src="'+esc(sid)+'">'+tgtOpts+'</select>'
      +'</div>'
      // upload: кнопка загрузки
      +'<div id="cfmiss-upload-'+sid+'">'
      +'<button id="cfmiss-upload-btn-'+sid+'" class="da-btn da-btn-sm" type="button" '
      +'onclick="copyFeedUploadAction(\''+sid+'\')">⬆ Загрузить фид</button>'
      +' <span id="cfmiss-upload-note-'+sid+'" class="copy-note"></span>'
      +'</div>'
      // skip: пояснение
      +'<div id="cfmiss-skip-'+sid+'" style="display:none">'
      +'<span class="copy-note">Кампании с этим фидом будут пропущены при копировании</span>'
      +'</div>'
      +'</div>'
      +'</div>';
  }).join('');
}
function buildFeedMap(){
  const map={};
  // Все дропдауны .copy-feed-sel: включаем если нет missing-choice или choice.mode==='replace'
  document.querySelectorAll('.copy-feed-sel').forEach(sel=>{
    const src=sel.getAttribute('data-src');
    const choice=_FEED_MISSING_CHOICES[src];
    if(choice && choice.mode!=='replace') return;  // upload/skip — не берём из дропдауна
    const tgt=(sel.value||'').trim();
    if(src && tgt) map[src]=parseInt(tgt,10);
  });
  // upload с успешным результатом: добавляем новый feed_id
  Object.entries(_FEED_MISSING_CHOICES).forEach(([src,choice])=>{
    if(choice.mode==='upload' && choice.tgt_id) map[src]=choice.tgt_id;
  });
  return map;
}

// ── Запуск копирования «Авто» ────────────────────────────────────────────────
async function startCopyCampaigns(){
  const source_login=((document.getElementById('copy-source-login')||{}).value||'').trim();
  const target_login=((document.getElementById('copy-target-login')||{}).value||'').trim();
  const target_domain=((document.getElementById('copy-target-domain')||{}).value||'').trim();
  const target_city=((document.getElementById('copy-target-city')||{}).value||'').trim();
  const target_region=((document.getElementById('copy-target-region')||{}).value||'').trim();
  const counter_id=((document.getElementById('copy-counter')||{}).value||'').trim();
  const goal_id=((document.getElementById('copy-goal')||{}).value||'').trim();
  const target_cleanup=document.querySelector('input[name="copy-cleanup"]:checked')?.value||'none';
  if(!source_login || !target_login){ alert('Укажите старый и новый аккаунт'); return; }
  if(!COPY_SELECTED.size){ alert('Выберите хотя бы одну кампанию'); return; }
  if(!counter_id){ alert('При пустом счётчике копирование запрещено'); return; }
  if(!goal_id){ alert('Цель «Все формы» обязательна'); return; }
  if(!target_domain){ alert('Укажите домен целевого аккаунта'); return; }
  if(!target_city && !target_region){ alert('Укажите город или регион целевого аккаунта'); return; }

  // Предупреждение: missing-фиды в upload-режиме без загруженного tgt_id будут пропущены
  const _pendingUploads=Object.entries(_FEED_MISSING_CHOICES)
    .filter(([,c])=>c.mode==='upload'&&!c.tgt_id);
  if(_pendingUploads.length){
    if(!confirm('Фидов не загружено: '+_pendingUploads.length+'. Кампании с ними будут пропущены при копировании. Продолжить?')) return;
  }

  // Подтверждение деструктивного действия очистки цели: ЧТО и НА КАКОМ аккаунте.
  // Кастомная модалка (uiConfirm, copy_common.js) вместо нативного confirm(): по центру, стиль сайта.
  // Деструктивное «удалить … необратимо» подсвечивается через opts.warn; архив — обычный текст.
  if(target_cleanup!=='none'){
    const info=_COPY_TARGET_CAMPS_INFO||{};
    let actionDesc='', warn=null;
    if(target_cleanup==='delete_drafts'){
      actionDesc='удалить '+(info.draft_count||'?')+' черновик(ов) — необратимо!';
      warn=actionDesc;
    } else if(target_cleanup==='archive'){
      actionDesc='отправить в архив '+(info.archivable_count||'?')+' кампаний (обратимо через unarchive в интерфейсе).';
    }
    const message='Перед копированием будет выполнено:\n\n'
      +'Аккаунт: '+target_login
      +(warn?'':'\n\nДействие: '+actionDesc);
    const ok=await uiConfirm({title:'Подтвердите действие', message, warn, confirm:'Подтвердить', cancel:'Отмена'});
    if(!ok) return;
  }

  const feed_map=buildFeedMap();
  const body={
    source_login, target_login,
    campaign_ids:Array.from(COPY_SELECTED),
    target_domain, target_city, target_region,
    counter_id, goal_id, feed_map,
    target_cleanup
  };
  _COPY_JOB_TAB='auto';
  document.getElementById('copy-start-btn').disabled=true;
  renderCopyStatus({status:'queued', progress:0, total:COPY_SELECTED.size, log:['ставлю задачу в очередь…']});
  try{
    // Роут «Авто». Он ОТВЕРГАЕТ mode='other' — у «Прочих сфер» свой /direct/api/copy_other_start.
    const j=await (await fetch('/direct/api/copy_start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
    if(j.error){ document.getElementById('copy-start-btn').disabled=false; alert(j.error); return; }
    COPY_JOB_ID=j.job_id;
    // Карточка в общей очереди копирования: агентство copy_status не отдаёт — берём из ответа старта.
    _cqRegister(j.job_id, {source:source_login, login:j.login||target_login, agency:j.agency||'', total:j.total||COPY_SELECTED.size,
                           tab:'auto', ahead:j.ahead||0});
    pollCopyJob();
  }catch(e){ document.getElementById('copy-start-btn').disabled=false; alert('Ошибка запуска: '+e); }
}
