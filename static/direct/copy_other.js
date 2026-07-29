/* ============================================================================
   copy_other.js — ТОЛЬКО вкладка «Прочие сферы» (templates/direct/copy_other.html).

   Цель + очистка цели + гео-дерево + режим картинок + проверка фидов +
   запуск POST /direct/api/copy_other_start.
   Общее (Источник, COPY_SELECTED, поллинг, очередь, renderCopyStatus) —
   в copy_common.js. Не копировать сюда поллинг.
   ============================================================================ */

// ── Автоподсказка целевого логина ────────────────────────────────────────────
function _otherTargetSuggest(){ _copySuggest('other-target-login','other-target-dd'); }
function _otherTargetHide(){ _copyHide('other-target-dd'); }
function _otherTargetKey(e){ _copyKey('other-target-dd',e); }

async function loadOtherTargetPrefill(){
  const login=((document.getElementById('other-target-login')||{}).value||'').trim();
  const note=document.getElementById('other-target-note');
  if(!login){ if(note) note.textContent=''; _loadOtherTargetCampaigns(''); return; }
  if(note) note.textContent='Подставляю домен / Метрику…';
  try{
    const j=await (await fetch('/direct/api/copy_target_prefill?login='+encodeURIComponent(login))).json();
    if(j.error){ if(note) note.innerHTML='<span style="color:#e3b341">'+esc(j.error)+'</span>'; }
    else {
      document.getElementById('other-target-domain').value=j.domain||'';
      document.getElementById('other-counter').value=j.counter_id||'';
      document.getElementById('other-goal').value=j.goal_id||'';
      if(note){
        const warns=(j.warnings||[]).length?(' · ⚠ '+j.warnings.join('; ')):'';
        note.textContent=(j.found===false?'В БД не найден — заполни домен/счётчик/цель вручную':'Подставлено из БД')+warns;
      }
    }
  }catch(e){ if(note) note.innerHTML='<span style="color:#ff7b72">Ошибка: '+esc(String(e))+'</span>'; }
  // Параллельно грузим кол-во кампаний цели для кнопок очистки
  _loadOtherTargetCampaigns(login);
}

function otherResolveGoal(){ return _copyResolveGoalInto('other-counter','other-goal'); }

// ── Кнопки очистки целевого аккаунта ──────────────────────────────────────────
let _OTHER_TARGET_CAMPS_INFO = null;

async function _loadOtherTargetCampaigns(login){
  const infoEl=document.getElementById('other-cleanup-info');
  // Сбрасываем cleanup СРАЗУ: пока грузятся кампании нового аккаунта, delete/archive недоступны и
  // radio на 'none' — иначе клик «Копировать» до ответа применит очистку по устаревшим данным (race).
  _OTHER_TARGET_CAMPS_INFO=null;
  _updateCleanupButtons();
  if(!login){
    if(infoEl) infoEl.textContent='Укажите целевой аккаунт для загрузки статистики.';
    return;
  }
  if(infoEl) infoEl.textContent='Загружаю кампании цели…';
  try{
    const j=await (await fetch('/direct/api/copy_target_campaigns?login='+encodeURIComponent(login))).json();
    // Пользователь мог сменить аккаунт, пока шёл запрос — игнорируем устаревший ответ.
    if((((document.getElementById('other-target-login')||{}).value||'').trim())!==login) return;
    if(j.error){
      if(infoEl) infoEl.innerHTML='<span style="color:#e3b341">⚠ '+esc(j.error)+'</span>';
      _OTHER_TARGET_CAMPS_INFO=null;
    } else {
      _OTHER_TARGET_CAMPS_INFO=j;
      const t=j.total||0, d=j.draft_count||0, nd=j.non_draft_count||0;
      if(infoEl) infoEl.textContent=
        t===0 ? 'Кампаний нет — аккаунт чист'
               : 'на аккаунте '+t+' кампаний: '+d+' черновиков, '+nd+' не-черновиков';
    }
  }catch(e){
    if(infoEl) infoEl.innerHTML='<span style="color:#ff7b72">Ошибка: '+esc(String(e))+'</span>';
    _OTHER_TARGET_CAMPS_INFO=null;
  }
  _updateCleanupButtons();
}

function _updateCleanupButtons(){
  const info=_OTHER_TARGET_CAMPS_INFO;
  const hasDrafts   = info && (info.draft_count||0)>0;
  const hasArchiv   = info && (info.archivable_count||0)>0;
  const hasAny      = info && (info.total||0)>0;

  const delLabel=document.getElementById('other-cleanup-del-label');
  const arcLabel=document.getElementById('other-cleanup-arc-label');
  const delInput=delLabel&&delLabel.querySelector('input');
  const arcInput=arcLabel&&arcLabel.querySelector('input');

  if(delLabel){
    const on=hasAny&&hasDrafts;
    delLabel.classList.toggle('cleanup-disabled',!on);
    if(delInput){ delInput.disabled=!on; }
  }
  if(arcLabel){
    const on=hasAny&&hasArchiv;
    arcLabel.classList.toggle('cleanup-disabled',!on);
    if(arcInput){ arcInput.disabled=!on; }
  }
  // Если выбранная опция стала недоступна — сброс на 'none'
  const sel=document.querySelector('input[name="other-cleanup"]:checked');
  if(sel&&sel.value==='delete_drafts'&&!(hasAny&&hasDrafts)){
    const none=document.querySelector('input[name="other-cleanup"][value="none"]');
    if(none) none.checked=true;
  }
  if(sel&&sel.value==='archive'&&!(hasAny&&hasArchiv)){
    const none=document.querySelector('input[name="other-cleanup"][value="none"]');
    if(none) none.checked=true;
  }
}


// ── UX «фид отсутствует в цели» для вкладки «Прочие сферы» ──────────────────
// _OTHER_TARGET_FEEDS: фиды целевого аккаунта (из copy_feeds_preview)
// _OTHER_AUTO_FEED_MAP: {src_id_str: tgt_id_int} — авто-совпадения для отправки в job
// _OTHER_FEED_MANUAL_MAP: {src_id_str: {mode,tgt_id}} — ручные выборы для unmatched
let _OTHER_TARGET_FEEDS = [];
let _OTHER_AUTO_FEED_MAP = {};
let _OTHER_FEED_MANUAL_MAP = {};

function otherFeedMissingMode(srcId, mode){
  if(!_OTHER_FEED_MANUAL_MAP[srcId]) _OTHER_FEED_MANUAL_MAP[srcId]={mode:'upload',tgt_id:null};
  _OTHER_FEED_MANUAL_MAP[srcId].mode = mode;
  const rEl=document.getElementById('ofmiss-replace-'+srcId);
  const uEl=document.getElementById('ofmiss-upload-'+srcId);
  const sEl=document.getElementById('ofmiss-skip-'+srcId);
  if(rEl) rEl.style.display = mode==='replace'?'':'none';
  if(uEl) uEl.style.display = mode==='upload' ?'':'none';
  if(sEl) sEl.style.display = mode==='skip'   ?'':'none';
}

async function otherFeedUploadAction(srcId){
  if(!_OTHER_FEED_MANUAL_MAP[srcId]) _OTHER_FEED_MANUAL_MAP[srcId]={mode:'upload',tgt_id:null};
  const source_login=((document.getElementById('copy-source-login')||{}).value||'').trim();
  const target_login=((document.getElementById('other-target-login')||{}).value||'').trim();
  const target_domain=((document.getElementById('other-target-domain')||{}).value||'').trim();
  if(!source_login||!target_login||!target_domain){
    alert('Укажите старый аккаунт, новый аккаунт и домен цели перед загрузкой фида');
    return;
  }
  const noteEl=document.getElementById('ofmiss-upload-note-'+srcId);
  const btn=document.getElementById('ofmiss-upload-btn-'+srcId);
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
    _OTHER_FEED_MANUAL_MAP[srcId]={mode:'upload', tgt_id:j.feed_id};
    if(noteEl) noteEl.innerHTML='<span style="color:#3fb950">✓ загружен фид #'+esc(String(j.feed_id))+' · '+esc(j.url||j.name)+'</span>';
    if(btn){ btn.disabled=true; btn.textContent='✓ Загружен'; }
  }catch(e){
    if(noteEl) noteEl.innerHTML='<span class="err">Ошибка: '+esc(String(e))+'</span>';
    if(btn) btn.disabled=false;
  }
}

// ── Запуск копирования «Прочие сферы» ────────────────────────────────────────
async function startOtherCampaigns(){
  const source_login=((document.getElementById('copy-source-login')||{}).value||'').trim();
  const target_login=((document.getElementById('other-target-login')||{}).value||'').trim();
  const target_domain=((document.getElementById('other-target-domain')||{}).value||'').trim();
  const counter_id=((document.getElementById('other-counter')||{}).value||'').trim();
  const goal_id=((document.getElementById('other-goal')||{}).value||'').trim();
  const geo_mode=document.querySelector('input[name="other-geo-mode"]:checked')?.value||'keep';
  const target_cleanup=document.querySelector('input[name="other-cleanup"]:checked')?.value||'none';
  let geo_region_ids=[];
  if(geo_mode==='change'){
    geo_region_ids=_geoGetRegionIds();
  }
  if(!source_login||!target_login){ alert('Укажите старый и новый аккаунт'); return; }
  if(!COPY_SELECTED.size){ alert('Выберите хотя бы одну кампанию'); return; }
  if(!counter_id){ alert('Счётчик Метрики обязателен'); return; }
  if(!goal_id){ alert('Цель «Все формы» обязательна'); return; }
  if(!target_domain){ alert('Укажите домен целевого аккаунта'); return; }
  if(geo_mode==='change'&&!geo_region_ids.filter(x=>x>0).length){ alert('Выберите хотя бы один регион'); return; }

  // Предупреждение: missing-фиды в upload-режиме без загруженного tgt_id будут пропущены
  const _pendingUploads=Object.entries(_OTHER_FEED_MANUAL_MAP)
    .filter(([,c])=>c.mode==='upload'&&!c.tgt_id);
  if(_pendingUploads.length){
    if(!confirm('Фидов не загружено: '+_pendingUploads.length+'. Кампании с ними будут пропущены при копировании. Продолжить?')) return;
  }

  // Подтверждение деструктивного действия: показываем ЧТО и НА КАКОМ аккаунте.
  // Кастомная модалка (uiConfirm, copy_common.js) вместо нативного confirm(): по центру, стиль сайта.
  if(target_cleanup!=='none'){
    const info=_OTHER_TARGET_CAMPS_INFO||{};
    let actionDesc='', warn=null;
    if(target_cleanup==='delete_drafts'){
      const n=info.draft_count||'?';
      actionDesc='удалить '+n+' черновик(ов) — необратимо!';
      warn=actionDesc;
    } else if(target_cleanup==='archive'){
      const n=info.archivable_count||'?';
      actionDesc='отправить в архив '+n+' кампаний (обратимо через unarchive в интерфейсе).';
    }
    const message='Перед копированием будет выполнено:\n\n'
      +'Аккаунт: '+target_login
      +(warn?'':'\n\nДействие: '+actionDesc);
    const ok=await uiConfirm({title:'Подтвердите действие', message, warn, confirm:'Подтвердить', cancel:'Отмена'});
    if(!ok) return;
  }

  // Картинки: хэши шлём ТОЛЬКО в режиме «загружаем новые». В режиме 1в1 image_hashes пуст →
  // движок переносит картинки исходного аккаунта, как раньше.
  const image_mode=otherImgMode();
  if(image_mode==='upload' && !OTHER_IMAGE_HASHES.length){
    alert('Выбран режим «загружаем новые», но ни одна картинка не загружена.\n'
        + 'Загрузите изображения или переключите режим на «копировать 1в1».');
    return;
  }

  // Строим feed_map: авто-совпадения + ручные выборы (replace/upload) без skip
  const feed_map = Object.assign({}, _OTHER_AUTO_FEED_MAP);
  Object.entries(_OTHER_FEED_MANUAL_MAP).forEach(([src, choice])=>{
    if(choice.mode === 'replace'){
      const sel = document.querySelector('.of-feed-sel[data-src="'+src+'"]');
      const v = sel && (sel.value||'').trim();
      if(v) feed_map[src] = parseInt(v, 10);
    } else if(choice.mode === 'upload' && choice.tgt_id){
      feed_map[src] = choice.tgt_id;
    }
    // skip → не включаем → кампании с этим фидом пропустятся (_copy_skip_unmapped_feed_campaigns)
  });
  const body={
    source_login, target_login,
    campaign_ids:Array.from(COPY_SELECTED),
    target_domain, counter_id, goal_id,
    mode:'other',
    geo_mode,
    target_cleanup,
    image_mode,
    image_hashes:(image_mode==='upload'?OTHER_IMAGE_HASHES.slice():[]),
    feed_map,
  };
  if(geo_mode==='change'){ body.geo_region_ids=geo_region_ids; }
  _COPY_JOB_TAB='other';
  document.getElementById('other-start-btn').disabled=true;
  renderCopyStatus({status:'queued',progress:0,total:COPY_SELECTED.size,log:['ставлю задачу в очередь…']});
  try{
    // Свой эндпоинт «Прочих сфер» (у «Авто» — /direct/api/copy_start). Префикс copy_ обязателен:
    // без него nginx не отдаст запрос на :5022 и он уедет в сервис создания.
    const j=await (await fetch('/direct/api/copy_other_start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
    if(j.error){ document.getElementById('other-start-btn').disabled=false; alert(j.error); return; }
    COPY_JOB_ID=j.job_id;
    // Карточка в общей очереди копирования: агентство copy_status не отдаёт — берём из ответа старта.
    _cqRegister(j.job_id, {source:source_login, login:j.login||target_login, agency:j.agency||'', total:j.total||COPY_SELECTED.size,
                           tab:'other', ahead:j.ahead||0});
    pollCopyJob();
  }catch(e){ document.getElementById('other-start-btn').disabled=false; alert('Ошибка запуска: '+e); }
}

// ── Проверка фидов (с UX replace/upload/skip для отсутствующих) ──────────────
async function checkOtherFeeds(){
  const source_login=((document.getElementById('copy-source-login')||{}).value||'').trim();
  const target_login=((document.getElementById('other-target-login')||{}).value||'').trim();
  const note=document.getElementById('other-feed-check-note');
  const result=document.getElementById('other-feed-check-result');
  const btn=document.getElementById('other-feed-check-btn');
  if(!source_login||!target_login){ alert('Укажите старый и новый аккаунт'); return; }
  if(note) note.textContent='Загружаю фиды…';
  if(result) result.style.display='none';
  if(btn) btn.disabled=true;
  // Сброс предыдущих выборов
  _OTHER_TARGET_FEEDS=[]; _OTHER_AUTO_FEED_MAP={}; _OTHER_FEED_MANUAL_MAP={};
  try{
    // copy_feeds_preview возвращает source_feeds+target_feeds — из них делаем
    // и автосопоставление, и рендер replace/upload/skip для unmatched
    const j=await (await fetch('/direct/api/copy_feeds_preview',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({source_login, target_login,
                           campaign_ids:Array.from(COPY_SELECTED)})})).json();
    if(j.error){ if(note) note.innerHTML='<span class="err">'+esc(j.error)+'</span>'; return; }
    _OTHER_TARGET_FEEDS = j.target_feeds||[];
    const srcFeeds = j.source_feeds||[];
    // Клиентское автосопоставление (_feedMatchFromList из copy_common.js)
    const matched=[], unmatched=[];
    srcFeeds.forEach(sf=>{
      const m=_feedMatchFromListDetailed(sf, _OTHER_TARGET_FEEDS);
      if(m){
        const tgtFeed = _OTHER_TARGET_FEEDS.find(t=>String(t.id)===m.id)||{};
        matched.push({sf, tgtId:m.id, tgtName: tgtFeed.name||('фид #'+m.id), rule:m.rule});
        _OTHER_AUTO_FEED_MAP[String(sf.id)] = parseInt(m.id, 10);
      } else {
        unmatched.push(sf);
        _OTHER_FEED_MANUAL_MAP[String(sf.id)] = {mode:'upload', tgt_id:null};
      }
    });
    if(note) note.textContent='Фидов источника: '+srcFeeds.length
      +' · целевых фидов: '+_OTHER_TARGET_FEEDS.length
      +' · авто-совпадений: '+matched.length
      +(unmatched.length?' · не найдено: '+unmatched.length:'');
    _renderOtherFeedsUI(matched, unmatched, result);
  }catch(e){
    if(note) note.innerHTML='<span class="err">Ошибка: '+esc(String(e))+'</span>';
  }finally{
    if(btn) btn.disabled=false;
  }
}

function _renderOtherFeedsUI(matched, unmatched, resultEl){
  if(!resultEl) return;
  if(!matched.length && !unmatched.length){
    resultEl.innerHTML='<div class="copy-note" style="padding:4px 0">У источника нет фидов в выбранных кампаниях.</div>';
    resultEl.style.display='';
    return;
  }
  let html='<div class="feed-check-summary">'
    +'Авто-совпадений: <b class="fct-ok">'+matched.length+'</b>'
    +' · Требуют действия: <b class="fct-miss">'+unmatched.length+'</b></div>';
  if(matched.length){
    html+='<table class="feed-check-table"><thead><tr><th>Исходный фид</th><th>→ Целевой фид (авто)</th><th>Правило</th><th>Кампаний</th></tr></thead><tbody>'
      +matched.map(m=>'<tr>'
        +'<td class="fct-ok">'+esc(m.sf.name||('фид #'+m.sf.id))
        +'<div style="color:var(--text-muted);font-size:10px">#'+m.sf.id+'</div></td>'
        +'<td>'+esc(m.tgtName)+'<div style="color:var(--text-muted);font-size:10px">#'+m.tgtId+'</div></td>'
        +'<td><span class="copy-note">'+(m.rule==='path'?'по пути URL':'по имени файла')+'</span></td>'
        +'<td>'+(m.sf.campaigns||0)+'</td>'
        +'</tr>').join('')
      +'</tbody></table>';
  }
  if(unmatched.length){
    const tgtOpts='<option value="">— выберите фид цели —</option>'
      +_OTHER_TARGET_FEEDS.map(t=>'<option value="'+esc(String(t.id))+'">'+esc(t.name)+' (#'+esc(String(t.id))+')</option>').join('');
    html+='<div class="ofmiss-section">'
      +'<div class="ofmiss-title">Не найдено в цели — выберите действие для каждого фида:</div>'
      +unmatched.map(sf=>{
        const sid=String(sf.id);
        const usageParts=[];
        if(sf.campaigns) usageParts.push(sf.campaigns+' кампаний');
        if(sf.groups) usageParts.push(sf.groups+' групп');
        const usage=usageParts.length?' · '+usageParts.join(' / '):'';
        return '<div class="cf-missing-row" style="margin-bottom:10px;padding:8px;background:var(--bg-card,#1c1c22);border-radius:6px">'
          +'<div style="margin-bottom:6px"><b>'+esc(sf.name||('фид #'+sid))+'</b>'
          +' <span class="copy-note">#'+esc(sid)+esc(usage)+'</span></div>'
          +'<div class="cf-missing-radios">'
          +'<label><input type="radio" name="ofmiss-'+sid+'" value="replace" onchange="otherFeedMissingMode(\''+sid+'\',\'replace\')"> Заменить на</label>'
          +'<label><input type="radio" name="ofmiss-'+sid+'" value="upload" checked onchange="otherFeedMissingMode(\''+sid+'\',\'upload\')"> Загрузить в цель</label>'
          +'<label><input type="radio" name="ofmiss-'+sid+'" value="skip" onchange="otherFeedMissingMode(\''+sid+'\',\'skip\')"> Пропустить</label>'
          +'</div>'
          +'<div id="ofmiss-replace-'+sid+'" style="display:none;margin-top:4px">'
          +'<select class="of-feed-sel" data-src="'+esc(sid)+'">'+tgtOpts+'</select>'
          +'</div>'
          +'<div id="ofmiss-upload-'+sid+'" style="margin-top:4px">'
          +'<button id="ofmiss-upload-btn-'+sid+'" class="da-btn da-btn-sm" type="button" '
          +'onclick="otherFeedUploadAction(\''+sid+'\')">⬆ Загрузить фид</button>'
          +' <span id="ofmiss-upload-note-'+sid+'" class="copy-note"></span>'
          +'</div>'
          +'<div id="ofmiss-skip-'+sid+'" style="display:none;margin-top:4px">'
          +'<span class="copy-note">Кампании с этим фидом будут пропущены при копировании</span>'
          +'</div>'
          +'</div>';
      }).join('')
      +'</div>';
  }
  resultEl.innerHTML=html;
  resultEl.style.display='';
}
