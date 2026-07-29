/* ============================================================================
   copy_images_ui.js — загрузка/распаковка картинок вкладки «Прочие».
   Самодостаточный кластер: OTHER_IMAGE_HASHES/_OTHER_IMG_ROWS + otherImg* + upload/drop.
   Вынесено из copy_other.js. Подключать ПЕРЕД copy_other.js (глобальный scope):
   esc/DOM — из copy_common.js; функции зовутся из copy_other.js в рантайме.
   ============================================================================ */

// ── Режим картинок ───────────────────────────────────────────────────────────
// 'copy' — переносим картинки исходного аккаунта 1в1 (по умолчанию);
// 'upload' — ставим загруженные ниже картинки round-robin по объявлениям.
function otherImgMode() {
  return document.querySelector('input[name="other-img-mode"]:checked')?.value || 'copy';
}

function otherImgModeChange() {
  const mode = otherImgMode();
  const wrap = document.getElementById('other-img-upload-wrap');
  if (wrap) wrap.style.display = mode === 'upload' ? '' : 'none';
  const note = document.getElementById('other-img-mode-note');
  if (note) note.textContent = mode === 'upload'
    ? 'Загруженные картинки встанут round-robin на объявления, где картинка есть в источнике.'
    : 'На объявлениях останутся картинки исходного аккаунта — включая изображения его сайта.';
}

// ── Загрузка изображений ─────────────────────────────────────────────────────
let OTHER_IMAGE_HASHES=[];   // накапливаем загруженные хэши
let _OTHER_IMG_ROWS=[];      // строки превью [{name,status,hash}]

function _renderOtherImgList(){
  const box=document.getElementById('other-img-list');
  if(!box) return;
  if(!_OTHER_IMG_ROWS.length){ box.innerHTML=''; return; }
  box.innerHTML=_OTHER_IMG_ROWS.map((r,i)=>{
    const statusHtml=r.hash
      ? '<span class="ihash">'+esc(r.hash.substring(0,16))+'…</span>'
      : (r.error?'<span class="ierr">'+esc(r.error)+'</span>'
        : (r.info?'<span class="ihash">'+esc(r.info)+'</span>'
                 : '<span class="iprog">'+esc(r.stage||'загрузка…')+'</span>'));
    return '<div class="img-upload-row"><span class="iname">'+esc(r.name)+'</span>'+statusHtml+'</div>';
  }).join('');
}

// fetch() не умеет отдавать прогресс ОТПРАВКИ — только XHR (upload.onprogress).
// Возвращает распарсенный JSON; onProgress(pct) зовётся по мере отправки тела,
// после 100% начинается серверная обработка (распаковка+сжатие+заливка в Директ) —
// она может занять минуты, поэтому фаза показывается отдельно.
function _uploadWithProgress(url, formData, onProgress){
  return new Promise((resolve, reject)=>{
    const xhr=new XMLHttpRequest();
    xhr.open('POST', url, true);
    xhr.upload.onprogress=(e)=>{
      if(e.lengthComputable && onProgress) onProgress(Math.round(e.loaded*100/e.total));
    };
    xhr.upload.onload=()=>{ if(onProgress) onProgress(100); };   // тело ушло → ждём ответ
    xhr.onload=()=>{
      let j=null;
      try{ j=JSON.parse(xhr.responseText); }
      catch(_){
        // Не-JSON = ответ прокси (413/504/502), а не нашего сервиса. Показываем внятно,
        // а не «Unexpected token '<'».
        return reject(new Error('сервер вернул не JSON (HTTP '+xhr.status+') — вероятно лимит/таймаут прокси'));
      }
      resolve(j);
    };
    xhr.onerror=()=>reject(new Error('соединение оборвалось при загрузке (HTTP '+xhr.status+')'));
    xhr.ontimeout=()=>reject(new Error('таймаут загрузки'));
    xhr.timeout=15*60*1000;   // 15 мин: заливка каждой картинки в Директ — сетевая операция
    xhr.send(formData);
  });
}

async function _otherUploadFiles(files){
  const target_login=((document.getElementById('other-target-login')||{}).value||'').trim();
  if(!target_login){ alert('Сначала укажите целевой аккаунт'); return; }
  // Схлопывание дублей 1:1 по имени: если файл с таким именем уже залит (есть хэш),
  // повторно не отправляем — иначе лишние units, дубли картинок в аккаунте и перекос
  // round-robin. Дубли внутри одной пачки тоже отсекаем.
  const _already=new Set(_OTHER_IMG_ROWS.filter(r=>r.hash).map(r=>(r.name||'').trim().toLowerCase()));
  const _inBatch=new Set();
  const toSend=[];
  for(const f of files){
    const k=(f.name||'').trim().toLowerCase();
    if(_already.has(k)||_inBatch.has(k)){
      _OTHER_IMG_ROWS.push({name:f.name,status:'dup',hash:null,error:null,info:'дубль — пропущен'});
      continue;
    }
    _inBatch.add(k);
    toSend.push(f);
  }
  if(!toSend.length){ _renderOtherImgList(); return; }

  const fd=new FormData();
  fd.append('target_login',target_login);
  const startIdx=_OTHER_IMG_ROWS.length;
  const inputNames=[]; // имена файлов для строк до завершения (архивы дадут разные имена в результате)
  for(const f of toSend){
    fd.append('images',f,f.name);
    _OTHER_IMG_ROWS.push({name:f.name,status:'uploading',hash:null,error:null});
    inputNames.push(f.name);
  }
  _renderOtherImgList();
  let archiveExtracted=0;
  try{
    const j=await _uploadWithProgress('/direct/api/copy_images_upload',fd,(pct)=>{
      for(let i=startIdx;i<_OTHER_IMG_ROWS.length;i++){
        _OTHER_IMG_ROWS[i].stage = pct<100 ? ('загрузка '+pct+'%')
                                           : 'обработка на сервере…';
      }
      _renderOtherImgList();
    });
    archiveExtracted=j.archive_extracted||0;
    if(j.error){
      for(let i=startIdx;i<_OTHER_IMG_ROWS.length;i++) _OTHER_IMG_ROWS[i].error=j.error;
    } else {
      const results=j.results||[];
      const errors=j.errors||[];
      // Для файлов-не-архивов: матчим по имени как раньше
      for(let i=startIdx;i<_OTHER_IMG_ROWS.length;i++){
        const name=_OTHER_IMG_ROWS[i].name;
        const r=results.find(x=>x.name===name);
        const e=errors.find(x=>x.name===name);
        if(r){ _OTHER_IMG_ROWS[i].hash=r.hash; OTHER_IMAGE_HASHES.push(r.hash); }
        else if(e){ _OTHER_IMG_ROWS[i].error=e.error; }
      }
      // Для архивов: добавляем строки для каждого извлечённого изображения
      // (не совпавшие по имени с входными файлами)
      const inputNameSet=new Set(inputNames);
      for(const r of results){
        if(!inputNameSet.has(r.name)){
          _OTHER_IMG_ROWS.push({name:r.name,status:'ok',hash:r.hash,error:null});
          OTHER_IMAGE_HASHES.push(r.hash);
        }
      }
      for(const e of errors){
        if(!inputNameSet.has(e.name)){
          _OTHER_IMG_ROWS.push({name:e.name,status:'err',hash:null,error:e.error});
        }
      }
      // Строка самого АРХИВА никогда не совпадёт с results (там имена извлечённых картинок),
      // поэтому без этого она висела бы в «загрузка…» вечно даже при успехе.
      const _ARCH=/\.(zip|tar|tgz|tar\.gz)$/i;
      for(let i=startIdx;i<_OTHER_IMG_ROWS.length;i++){
        const row=_OTHER_IMG_ROWS[i];
        if(!row.hash && !row.error && _ARCH.test(row.name)){
          row.info='архив: извлечено '+archiveExtracted;
          row.stage=null;
        }
      }
    }
  }catch(ex){
    for(let i=startIdx;i<_OTHER_IMG_ROWS.length;i++) _OTHER_IMG_ROWS[i].error=String(ex).substring(0,80);
  }
  // Страховка: одна и та же картинка могла приехать из ДВУХ архивов — клиент сверяет
  // только имена входных файлов, а не извлечённых. Дубль хэша перекосил бы round-robin.
  OTHER_IMAGE_HASHES=[...new Set(OTHER_IMAGE_HASHES)];
  _renderOtherImgList();
  // Сводка по архивам
  const summaryEl=document.getElementById('other-img-summary');
  const dups=_OTHER_IMG_ROWS.filter(r=>r.status==='dup').length;
  if(summaryEl&&(archiveExtracted>0||dups>0||OTHER_IMAGE_HASHES.length>0)){
    const ok=_OTHER_IMG_ROWS.filter(r=>r.hash).length;
    const bad=_OTHER_IMG_ROWS.filter(r=>r.error).length;
    let t='Уникальных картинок: '+OTHER_IMAGE_HASHES.length+' · Залито: '+ok;
    if(archiveExtracted>0) t='Извлечено из архивов: '+archiveExtracted+' · '+t;
    if(dups>0) t+=' · Дублей схлопнуто: '+dups;
    if(bad>0) t+=' · Ошибок: '+bad;
    summaryEl.textContent=t;
    summaryEl.style.display='';
  }
}
function _otherImgFiles(files){
  if(files&&files.length) _otherUploadFiles(Array.from(files));
  document.getElementById('other-img-input').value='';
}
function _otherImgDrop(e){
  e.preventDefault();
  document.getElementById('other-img-drop').classList.remove('drag-over');
  // Фильтр по расширению (не по MIME-типу): MIME у zip/tar нестабилен между браузерами и ОС.
  const ALLOWED_EXTS = new Set(['jpg','jpeg','png','webp','gif','bmp','zip','tar','tgz']);
  const all = Array.from(e.dataTransfer.files);
  const ok = all.filter(f => {
    const n = (f.name || '').toLowerCase();
    const ext = n.split('.').pop() || '';
    return ALLOWED_EXTS.has(ext) || n.endsWith('.tar.gz');
  });
  const bad = all.filter(f => !ok.includes(f));
  if(bad.length){
    alert('Отброшено ' + bad.length + ' файл(ов) — неподдерживаемый формат:\n'
      + bad.slice(0,5).map(f=>f.name).join(', ')
      + '\nПоддерживаются: JPG, PNG, WEBP, GIF, BMP, ZIP, TAR, TAR.GZ, TGZ');
  }
  if(ok.length) _otherUploadFiles(ok);
}
