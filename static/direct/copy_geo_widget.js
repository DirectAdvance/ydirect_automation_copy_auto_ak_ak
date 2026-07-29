/* ============================================================================
   copy_geo_widget.js — виджет дерева регионов вкладки «Прочие» (гео-замена).
   Самодостаточный кластер: состояние _GEO_* + _geo* + loadOtherGeoRegions + otherGeoModeChange.
   Вынесено из copy_other.js. Подключать ПЕРЕД copy_other.js (глобальный scope):
   esc/DOM — из copy_common.js; функции зовутся из copy_other.js в рантайме.
   ============================================================================ */

// ── Гео-дерево ────────────────────────────────────────────────────────────────
let _GEO_REGIONS_LOADED = false;
let _GEO_OPEN = false;
let _GEO_SEARCH_Q = '';

// Хранилища состояния дерева
const _GEO_NODES = new Map();    // id → {id, name, type, parent_id}
const _GEO_CHILDREN = new Map(); // parent_id → [child_id,...]
let _GEO_ROOTS = [];             // id узлов верхнего уровня
const _GEO_EXPANDED = new Set(); // раскрытые id
const _GEO_INCLUDES = new Set(); // явно включённые id (положительные)
const _GEO_EXCLUDES = new Set(); // явно исключённые id (минус-регионы)
let _GEO_DIRTY_ANCS = new Set(); // предки всех выбранных/исключённых (для tri-state)

function _geoRecomputeDirtyAncestors() {
  _GEO_DIRTY_ANCS = new Set();
  for (const id of [..._GEO_INCLUDES, ..._GEO_EXCLUDES]) {
    let node = _GEO_NODES.get(id);
    while (node && node.parent_id) {
      _GEO_DIRTY_ANCS.add(node.parent_id);
      node = _GEO_NODES.get(node.parent_id);
    }
  }
}

function _geoHasIncludedAncestor(id) {
  let node = _GEO_NODES.get(id);
  while (node && node.parent_id) {
    const pid = node.parent_id;
    if (_GEO_INCLUDES.has(pid)) return true;
    if (_GEO_EXCLUDES.has(pid)) return false;
    node = _GEO_NODES.get(pid);
  }
  return false;
}

// Возможные состояния: 'checked' | 'implicit' | 'excluded' | 'indeterminate' | 'unchecked'
function _geoCheckboxState(id) {
  if (_GEO_INCLUDES.has(id)) return 'checked';
  if (_GEO_EXCLUDES.has(id)) return 'excluded';
  if (_geoHasIncludedAncestor(id)) return 'implicit';
  if (_GEO_DIRTY_ANCS.has(id)) return 'indeterminate';
  return 'unchecked';
}

function _geoRemoveDescendants(id) {
  const children = _GEO_CHILDREN.get(id) || [];
  for (const cid of children) {
    _GEO_INCLUDES.delete(cid);
    _GEO_EXCLUDES.delete(cid);
    _geoRemoveDescendants(cid);
  }
}

function _geoToggleNode(id) {
  const state = _geoCheckboxState(id);
  const hasAnc = _geoHasIncludedAncestor(id);
  if (state === 'unchecked') {
    _GEO_INCLUDES.add(id);
    _GEO_EXCLUDES.delete(id);
    _geoRemoveDescendants(id); // дети покрыты родителем
  } else if (state === 'checked') {
    _GEO_INCLUDES.delete(id);
    if (hasAnc) {
      _GEO_EXCLUDES.add(id);
      _geoRemoveDescendants(id);
    }
  } else if (state === 'implicit') {
    _GEO_EXCLUDES.add(id);
    _geoRemoveDescendants(id);
  } else if (state === 'excluded') {
    _GEO_EXCLUDES.delete(id);
  } else if (state === 'indeterminate') {
    // Смешанный: при клике включаем весь регион целиком
    _geoRemoveDescendants(id);
    _GEO_INCLUDES.delete(id);
    _GEO_INCLUDES.add(id);
  }
  _geoUpdate();
}

function _geoExpand(id) {
  if (!_GEO_CHILDREN.has(id) || !_GEO_CHILDREN.get(id).length) return;
  if (_GEO_EXPANDED.has(id)) _GEO_EXPANDED.delete(id);
  else _GEO_EXPANDED.add(id);
  _geoRenderTree();
}

function _geoTriggerText() {
  const n = _GEO_INCLUDES.size + _GEO_EXCLUDES.size;
  if (!n) return '— выберите регионы —';
  const p = _GEO_INCLUDES.size, m = _GEO_EXCLUDES.size;
  let t = p + ' ' + (p % 10 === 1 && p % 100 !== 11 ? 'регион' : p % 10 >= 2 && p % 10 <= 4 && (p % 100 < 10 || p % 100 >= 20) ? 'региона' : 'регионов');
  if (m) t += ' − ' + m + ' ' + (m % 10 === 1 && m % 100 !== 11 ? 'исключение' : m % 10 >= 2 && m % 10 <= 4 && (m % 100 < 10 || m % 100 >= 20) ? 'исключения' : 'исключений');
  return t;
}

function _geoRenderChips() {
  const box = document.getElementById('other-geo-chips');
  const trigger = document.getElementById('other-geo-trigger-text');
  if (!box) return;
  const chips = [];
  for (const id of _GEO_INCLUDES) {
    const n = _GEO_NODES.get(id); if (n) chips.push({id, name: n.name, sign: '+'});
  }
  for (const id of _GEO_EXCLUDES) {
    const n = _GEO_NODES.get(id); if (n) chips.push({id, name: n.name, sign: '-'});
  }
  box.innerHTML = chips.map(c =>
    '<span class="geo-chip' + (c.sign === '-' ? ' geo-chip-minus' : '') + '">'
    + (c.sign === '-' ? '− ' : '') + esc(c.name)
    + '<button class="geo-chip-x" type="button" onclick="_geoRemoveChip(' + c.id + ',\'' + c.sign + '\')">×</button>'
    + '</span>'
  ).join('');
  if (trigger) trigger.textContent = _geoTriggerText();
}

function _geoRemoveChip(id, sign) {
  if (sign === '+') { _GEO_INCLUDES.delete(id); _geoRemoveDescendantsExcludes(id); }
  else { _GEO_EXCLUDES.delete(id); }
  _geoUpdate();
}

function _geoRemoveDescendantsExcludes(id) {
  // При удалении включённого региона убираем его минус-потомков (они стали бессмысленными)
  const children = _GEO_CHILDREN.get(id) || [];
  for (const cid of children) {
    _GEO_EXCLUDES.delete(cid);
    _geoRemoveDescendantsExcludes(cid);
  }
}

function _geoNodeHtml(id, depth) {
  const node = _GEO_NODES.get(id); if (!node) return '';
  const children = _GEO_CHILDREN.get(id) || [];
  const hasKids = children.length > 0;
  const expanded = _GEO_EXPANDED.has(id);
  const state = _geoCheckboxState(id);
  const cbCls = state === 'checked' || state === 'implicit' ? 'geo-cb-checked' :
                state === 'excluded' ? 'geo-cb-excluded' :
                state === 'indeterminate' ? 'geo-cb-indet' : 'geo-cb-empty';
  const cbIco = state === 'checked' || state === 'implicit' ? '☑' :
                state === 'excluded' ? '⊟' :
                state === 'indeterminate' ? '⊠' : '☐';
  const expIco = hasKids ? (expanded ? '▾' : '▸') : ' ';
  const expCls = hasKids ? '' : ' geo-expander-empty';
  let html = '<div class="geo-node" style="padding-left:' + (depth * 16 + 4) + 'px">'
    + '<span class="geo-expander' + expCls + '" onclick="' + (hasKids ? '_geoExpand(' + id + ')' : '') + '">' + expIco + '</span>'
    + '<span class="geo-cb ' + cbCls + '" onclick="_geoToggleNode(' + id + ')">' + cbIco + '</span>'
    + '<span class="geo-label" onclick="' + (hasKids ? '_geoExpand(' + id + ')' : '_geoToggleNode(' + id + ')') + '">' + esc(node.name) + '</span>'
    + '</div>';
  if (expanded && hasKids) {
    for (const cid of children) html += _geoNodeHtml(cid, depth + 1);
  }
  return html;
}

function _geoRenderTree() {
  const box = document.getElementById('other-geo-tree'); if (!box) return;
  if (_GEO_SEARCH_Q) { _geoRenderSearch(box); return; }
  if (!_GEO_ROOTS.length) { box.innerHTML = '<div class="geo-search-empty">Загрузка…</div>'; return; }
  let html = '';
  for (const id of _GEO_ROOTS) html += _geoNodeHtml(id, 0);
  box.innerHTML = html;
}

function _geoRenderSearch(box) {
  const q = _GEO_SEARCH_Q.toLowerCase();
  const matches = [];
  for (const [id, node] of _GEO_NODES) {
    if (node.name.toLowerCase().includes(q)) matches.push(id);
  }
  matches.sort((a, b) => (_GEO_NODES.get(a)?.name||'').localeCompare(_GEO_NODES.get(b)?.name||'', 'ru'));
  if (!matches.length) { box.innerHTML = '<div class="geo-search-empty">Ничего не найдено</div>'; return; }
  let html = '';
  for (const id of matches.slice(0, 150)) {
    const node = _GEO_NODES.get(id);
    const state = _geoCheckboxState(id);
    const cbCls = state === 'checked' || state === 'implicit' ? 'geo-cb-checked' :
                  state === 'excluded' ? 'geo-cb-excluded' :
                  state === 'indeterminate' ? 'geo-cb-indet' : 'geo-cb-empty';
    const cbIco = state === 'checked' || state === 'implicit' ? '☑' :
                  state === 'excluded' ? '⊟' :
                  state === 'indeterminate' ? '⊠' : '☐';
    // Путь для контекста
    const path = [];
    let cur = _GEO_NODES.get(id);
    while (cur && cur.parent_id) { const p = _GEO_NODES.get(cur.parent_id); if (p) path.unshift(p.name); cur = p; }
    const pathStr = path.slice(-2).join(' › ');
    html += '<div class="geo-node" style="padding-left:8px">'
      + '<span class="geo-cb ' + cbCls + '" onclick="_geoToggleNode(' + id + ')">' + cbIco + '</span>'
      + '<span class="geo-label" onclick="_geoToggleNode(' + id + ')">' + esc(node.name)
      + (pathStr ? '<span class="geo-path">' + esc(pathStr) + '</span>' : '')
      + '</span></div>';
  }
  if (matches.length > 150) html += '<div class="geo-search-empty" style="font-size:11px">... ещё ' + (matches.length - 150) + ' — уточните запрос</div>';
  box.innerHTML = html;
}

function _geoSearch(q) {
  _GEO_SEARCH_Q = (q || '').trim();
  _geoRenderTree();
}

function _geoUpdate() {
  _geoRecomputeDirtyAncestors();
  _geoRenderChips();
  _geoRenderTree();
}

function _geoTreeToggle(e) {
  if (e) e.stopPropagation();
  _GEO_OPEN = !_GEO_OPEN;
  const panel = document.getElementById('other-geo-panel');
  const trigger = document.getElementById('other-geo-trigger');
  if (panel) panel.style.display = _GEO_OPEN ? '' : 'none';
  if (trigger) trigger.classList.toggle('open', _GEO_OPEN);
  if (_GEO_OPEN) {
    const searchEl = document.getElementById('other-geo-search');
    if (searchEl) setTimeout(() => searchEl.focus(), 50);
  }
}

// Закрываем при клике вне виджета.
// ⚠️ «Внутри» определяем по e.composedPath(), а НЕ по wrap.contains(e.target):
// инлайн-onclick узлов дерева (_geoExpand / _geoToggleNode) перерисовывает
// #other-geo-tree целиком через innerHTML, поэтому к моменту всплытия клика до
// document кликнутый <span> уже detached — contains() даёт false и панель
// ошибочно закрывалась как «клик снаружи». composedPath() — снимок пути,
// сделанный в момент dispatch: он содержит wrap даже для удалённого узла.
// contains() остаётся фолбэком для сред без composedPath.
document.addEventListener('click', function(e) {
  if (!_GEO_OPEN) return;
  const wrap = document.getElementById('other-geo-change-wrap');
  if (!wrap) return;
  const path = (typeof e.composedPath === 'function') ? e.composedPath() : null;
  const inside = path ? path.indexOf(wrap) !== -1 : wrap.contains(e.target);
  if (!inside) {
    _GEO_OPEN = false;
    const panel = document.getElementById('other-geo-panel');
    const trigger = document.getElementById('other-geo-trigger');
    if (panel) panel.style.display = 'none';
    if (trigger) trigger.classList.remove('open');
  }
});

async function loadOtherGeoRegions() {
  const triggerText = document.getElementById('other-geo-trigger-text');
  if (triggerText) triggerText.textContent = 'Загрузка регионов…';
  try {
    const j = await (await fetch('/direct/api/copy_geo_regions')).json();
    const regions = j.regions || [];
    _GEO_NODES.clear(); _GEO_CHILDREN.clear(); _GEO_ROOTS = [];
    for (const r of regions) {
      _GEO_NODES.set(r.id, r);
    }
    for (const r of regions) {
      if (r.parent_id && _GEO_NODES.has(r.parent_id)) {
        if (!_GEO_CHILDREN.has(r.parent_id)) _GEO_CHILDREN.set(r.parent_id, []);
        _GEO_CHILDREN.get(r.parent_id).push(r.id);
      } else {
        _GEO_ROOTS.push(r.id);
      }
    }
    // Сортируем детей и корни по алфавиту
    for (const [, children] of _GEO_CHILDREN) {
      children.sort((a, b) => (_GEO_NODES.get(a)?.name||'').localeCompare(_GEO_NODES.get(b)?.name||'', 'ru'));
    }
    _GEO_ROOTS.sort((a, b) => (_GEO_NODES.get(a)?.name||'').localeCompare(_GEO_NODES.get(b)?.name||'', 'ru'));
    // Авто-раскрыть Россию (225) как основной случай использования
    if (_GEO_NODES.has(225)) _GEO_EXPANDED.add(225);
    _GEO_REGIONS_LOADED = true;
    if (triggerText) triggerText.textContent = _geoTriggerText();
    _geoUpdate();
  } catch(e) {
    const box = document.getElementById('other-geo-tree');
    if (box) box.innerHTML = '<div class="geo-search-empty" style="color:#ff7b72">Ошибка загрузки: ' + esc(String(e)) + '</div>';
    if (triggerText) triggerText.textContent = 'Ошибка загрузки регионов';
  }
}

// Собираем geo_region_ids для передачи в API: положительные + отрицательные
function _geoGetRegionIds() {
  const ids = [];
  for (const id of _GEO_INCLUDES) ids.push(id);
  for (const id of _GEO_EXCLUDES) ids.push(-id);
  return ids;
}

function otherGeoModeChange() {
  const mode = document.querySelector('input[name="other-geo-mode"]:checked')?.value || 'keep';
  const wrap = document.getElementById('other-geo-change-wrap');
  if (wrap) wrap.style.display = mode === 'change' ? '' : 'none';
  if (mode === 'change' && !_GEO_REGIONS_LOADED) loadOtherGeoRegions();
}
