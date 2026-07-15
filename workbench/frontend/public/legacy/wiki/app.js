let currentFile = null;
let currentContent = '';   // 当前文件的最新原始 markdown（每次从后端实时拉取）
let currentVersionId = null; // Hub 工作副本的乐观锁版本，防止覆盖其他会话的编辑
let totalCount = 0;        // 全库 md 文件总数
let toastTimer = null;

function showToast(msg) {
  const el = document.getElementById('toast');
  el.innerHTML = msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { el.classList.remove('show'); }, 3000);
}

marked.setOptions({ breaks: true, gfm: true });
marked.use({
  renderer: {
    image({ href, title, text }) {
      if (!href) return '<div style="padding:40px;text-align:center;color:#b3a890;font-size:13px">图片链接为空</div>';
      const h = String(href).replace(/#imgIndex=\d+/, '');
      const alt = (text || '').replace(/"/g, '&quot;');
      const esc = h.replace(/'/g, "\\'");
      return '<div class="img-wrap" data-img-core="' + encodeURIComponent(urlCore(h)) + '">'
        + '<button class="img-hover-del" onclick="event.stopPropagation();inlineDeleteImage(\'' + esc + '\')" title="删除该图片">🗑 删除</button>'
        + '<button class="img-hover-all" onclick="event.stopPropagation();inlineDeleteAll(\'' + esc + '\')" title="全库删除同一图片">⚠ 全部删除</button>'
        + '<span class="img-ref-badge" style="display:none" onclick="event.stopPropagation();showImageRefs(this,\'' + esc + '\')" title="点击查看引用详情">?</span>'
        + '<img src="' + h + '" alt="' + alt + '" loading="lazy" onerror="this.style.display=\'none\'" onclick="openLightbox(\'' + esc + '\')">'
        + '</div>';
    }
  }
});

/* ===== 后端 API ===== */
async function apiList(path) {
  const r = await fetch('/api/list' + (path ? '?path=' + encodeURIComponent(path) : ''));
  if (!r.ok) throw new Error('目录加载失败');
  return await r.json();  // {dirs:[{name,path,count}], files:[{name,path}]}
}
async function apiSearch(q) {
  const r = await fetch('/api/search?q=' + encodeURIComponent(q));
  const d = await r.json();
  return d.files || [];
}
async function apiRead(path) {
  const r = await fetch('/api/v1/wiki/source?source_ref=' + encodeURIComponent(path));
  if (!r.ok) throw new Error('读取失败: ' + path);
  const d = await r.json();
  if (!d.ok || !d.data) throw new Error('读取失败: ' + path);
  currentVersionId = d.data.version_id || null;
  return d.data.body;
}
async function apiSave(path, content) {
  const r = await fetch('/api/v1/wiki/source', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      source_ref: path,
      body: content,
      base_version_id: currentVersionId,
      operator: 'legacy-wiki-ui'
    })
  });
  const d = await r.json();
  if (!r.ok || !d.ok) throw new Error(d.detail || d.error || '保存失败');
  currentVersionId = d.data && d.data.version_id || currentVersionId;
  return d.data;
}

/* ===== 懒加载文件树 ===== */
// 渲染一层目录内容到指定容器
function renderLayer(data, container) {
  let html = '';
  for (const d of data.dirs) {
    const pEsc = d.path.replace(/'/g, "\\'");
    html += '<div class="folder-toggle" data-dir="' + d.path + '" onclick="toggleFolder(this)">'
      + '<span class="arrow">▶</span><span class="fname">' + d.name + '</span>'
      + '<span class="fcount">' + d.count + '</span></div>';
    html += '<div class="folder-content" data-loaded="0" style="display:none"></div>';
  }
  for (const f of data.files) {
    const label = f.name.replace(/\.md$/, '');
    const pEsc = f.path.replace(/'/g, "\\'");
    html += '<div class="file-item" data-path="' + f.path + '" onclick="loadFile(\'' + pEsc + '\')">' + label + '</div>';
  }
  container.innerHTML = html;
}

async function toggleFolder(el) {
  const content = el.nextElementSibling;
  const opening = content.style.display === 'none';
  el.classList.toggle('open', opening);
  content.style.display = opening ? 'block' : 'none';
  // 首次展开才去后端拉这一层
  if (opening && content.dataset.loaded === '0') {
    content.dataset.loaded = '1';
    content.innerHTML = '<div class="tree-loading">加载中…</div>';
    try {
      const data = await apiList(el.dataset.dir);
      renderLayer(data, content);
      if (currentFile) highlightActive();
    } catch (e) {
      content.innerHTML = '<div class="tree-loading" style="color:#c0392b">加载失败</div>';
      content.dataset.loaded = '0';
    }
  }
}

function highlightActive() {
  document.querySelectorAll('.file-item').forEach(e => e.classList.remove('active'));
  const a = document.querySelector('.file-item[data-path="' + currentFile + '"]');
  if (a) a.classList.add('active');
}

async function refreshTree() {
  const root = await apiList('');
  totalCount = root.dirs.reduce((s, d) => s + d.count, 0) + root.files.length;
  renderLayer(root, document.getElementById('file-tree'));
  const meta = document.querySelector('#sidebar-header .meta');
  if (meta) meta.textContent = totalCount + ' 个文件 · 全库';
  // wiki 是精炼库，默认展开
  const wikiToggle = document.querySelector('.folder-toggle[data-dir="wiki"]');
  if (wikiToggle) toggleFolder(wikiToggle);
  if (currentFile) highlightActive();
}

/* ===== 收起/展开侧栏（汉堡按钮） ===== */
function toggleNav() {
  document.body.classList.toggle('nav-collapsed');
}

async function loadFile(path) {
  // 每次都实时读盘 + 刷新列表，反映外部改动
  currentFile = path;
  // 手机端：选中文件后自动收起抽屉
  if (window.matchMedia('(max-width:768px)').matches) {
    document.body.classList.add('nav-collapsed');
  }
  document.querySelectorAll('.file-item').forEach(e => e.classList.remove('active'));
  const a = document.querySelector('.file-item[data-path="' + path + '"]');
  if (a) a.classList.add('active');
  document.getElementById('current-path').textContent = path;
  try {
    currentContent = await apiRead(path);
  } catch (e) {
    document.getElementById('view-mode').innerHTML = '<p style="color:#c0392b">' + e.message + '</p>';
    return;
  }
  renderView(currentContent, path);
  document.getElementById('btn-edit').style.display = 'inline-block';
  document.getElementById('btn-save').style.display = 'none';
  document.getElementById('btn-cancel').style.display = 'none';
  document.getElementById('s-left').textContent = totalCount + ' 文件 · ' + path;
}

function renderView(content, path) {
  let p = content;
  const ocrSlots = [];
  p = p.replace(/<\!--\s*(OCR内容|插图建议)\s*[:？]?\s*([\s\S]*?)-->/g, function (m, kind, body) {
    let txt = body.replace(/<\/?details>/gi, '').replace(/<summary>[\s\S]*?<\/summary>/gi, '').trim();
    if (!txt) return '';
    const isSug = kind === '插图建议';
    const slotIdx = ocrSlots.length;
    ocrSlots.push({ text: txt, isSug });
    return '<div data-ocr-slot="' + slotIdx + '"></div>';
  });
  p = p.replace(/\[\[([^\]]+)\]\]/g, function (m, link) {
    const pp = link.split('|');
    const target = pp[0].trim();
    const label = pp[1] ? pp[1].trim() : target;
    if (target.includes('/')) return '<a onclick="loadFile(\'' + target.replace(/'/g, "\\'") + '.md\')">' + label + '</a>';
    // 懒加载下没有全量列表，点击时用后端搜索解析
    return '<a onclick="resolveWikiLink(\'' + target.replace(/'/g, "\\'") + '\')">' + label + '</a>';
  });
  document.getElementById('view-mode').innerHTML = marked.parse(p);
  // 回填 OCR / 插图建议 block（避开 marked 的 HTML block 在空行处截断 div 的问题）
  ocrSlots.forEach(function (slot, idx) {
    const el = document.querySelector('[data-ocr-slot="' + idx + '"]');
    if (!el) return;
    const cls = slot.isSug ? 'ocr-block ocr-suggest' : 'ocr-block';
    const lbl = slot.isSug ? '📄 插图建议' : '🔑 OCR 识别文字';
    var bodyContent = slot.isSug
      ? slot.text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      : marked.parse(slot.text);
    var bodyCls = slot.isSug ? 'ocr-body' : 'ocr-body ocr-body-md';
    el.outerHTML = '<div class="' + cls + '"><div class="ocr-header">' + lbl + '</div><div class="' + bodyCls + '">' + bodyContent + '</div></div>';
  });
  document.getElementById('view-mode').style.display = 'block';
  document.getElementById('edit-mode').style.display = 'none';
  applyImageRefBadges();
}

/* ===== 图片引用计数徽章 ===== */
let _imageIndex = null;
let _imageIndexLoading = false;

async function loadImageIndex() {
  if (_imageIndex || _imageIndexLoading) return;
  _imageIndexLoading = true;
  try {
    const r = await fetch('/api/image-index');
    const d = await r.json();
    if (d.ok) _imageIndex = d.index;
  } catch (e) { /* 静默失败 */ }
  _imageIndexLoading = false;
}

function applyImageRefBadges() {
  if (!_imageIndex) {
    // 索引还没加载完，先触发加载，完成后再来一次
    loadImageIndex().then(function () { if (_imageIndex) applyImageRefBadges(); });
    return;
  }
  document.querySelectorAll('.img-wrap[data-img-core]').forEach(function (wrap) {
    const core = decodeURIComponent(wrap.dataset.imgCore);
    const info = _imageIndex[core];
    const badge = wrap.querySelector('.img-ref-badge');
    if (badge && info && info.total > 0) {
      badge.textContent = info.total;
      badge.style.display = '';
      if (info.total > 1) {
        badge.classList.add('multi');
      }
    }
  });
}

function showImageRefs(el, imgSrc) {
  const core = urlCore(imgSrc);
  const info = _imageIndex && _imageIndex[core];
  if (!info) {
    alert('未找到引用信息。');
    return;
  }
  const fileList = info.files.map(function (f) {
    const esc = f.path.replace(/'/g, "\\'");
    return '<div class="bulk-file-item bulk-file-link" onclick="jumpFromRefs(\'' + esc + '\')">📄 ' + f.path + ' <span class="bulk-count">' + f.count + ' 张</span></div>';
  }).join('');
  document.getElementById('bulk-delete-info').innerHTML =
    '<div class="bulk-stat">总计 <strong>' + info.total + '</strong> 张引用，分布于 <strong>'
    + info.files.length + '</strong> 个文件</div>'
    + '<div class="bulk-id">图片ID: ' + core.split('/').slice(-2).join('/') + '</div>'
    + '<div class="bulk-files">' + fileList + '</div>';
  document.getElementById('bulk-delete-dialog').setAttribute('data-mode', 'refs');
  document.getElementById('bulk-delete-dialog-box').querySelector('.dlg-actions').style.display = 'none';
  document.getElementById('bulk-delete-dialog').querySelector('h3').innerHTML = '📊 图片引用详情 <span class="refs-close-btn" onclick="bulkDeleteCancel()">✕</span>';
  document.getElementById('bulk-delete-dialog').style.display = 'flex';
}

/* ===== 全文编辑（工具栏按钮） ===== */
let isEditing = false;

function toggleEdit() {
  isEditing = true;
  document.getElementById('editor').value = currentContent;
  document.getElementById('view-mode').style.display = 'none';
  document.getElementById('edit-mode').style.display = 'block';
  document.getElementById('btn-edit').style.display = 'none';
  document.getElementById('btn-save').style.display = 'inline-block';
  document.getElementById('btn-cancel').style.display = 'inline-block';
  document.getElementById('edit-status').textContent = 'Ctrl+S 保存 · Ctrl+E 退出编辑';
}

function cancelEdit() {
  isEditing = false;
  document.getElementById('view-mode').style.display = 'block';
  document.getElementById('edit-mode').style.display = 'none';
  document.getElementById('btn-edit').style.display = 'inline-block';
  document.getElementById('btn-save').style.display = 'none';
  document.getElementById('btn-cancel').style.display = 'none';
}

async function saveFile() {
  const newContent = document.getElementById('editor').value;
  try {
    await apiSave(currentFile, newContent);
    currentContent = newContent;
    renderView(newContent, currentFile);
    cancelEdit();
    const b = document.getElementById('btn-edit');
    b.textContent = '✓ 已保存';
    setTimeout(function () { b.textContent = '✎ 编辑'; }, 2000);
  } catch (e) {
    alert('保存失败：' + e.message);
  }
}

/* ===== 全库文件名搜索（后端） ===== */
let searchTimer = null;
function filterFiles(q) {
  q = q.trim();
  clearTimeout(searchTimer);
  const tree = document.getElementById('file-tree');
  const results = document.getElementById('search-results');
  if (!q) {
    // 清空搜索，恢复懒加载树
    results.style.display = 'none';
    tree.style.display = 'block';
    return;
  }
  searchTimer = setTimeout(async function () {
    const hits = await apiSearch(q);
    tree.style.display = 'none';
    results.style.display = 'block';
    if (hits.length === 0) {
      results.innerHTML = '<div class="tree-loading">无匹配文件</div>';
      return;
    }
    let html = '<div class="search-meta">' + hits.length + ' 个结果</div>';
    for (const path of hits) {
      const label = path.replace(/\.md$/, '');
      const pEsc = path.replace(/'/g, "\\'");
      html += '<div class="file-item search-hit" data-path="' + path + '" onclick="loadFile(\'' + pEsc + '\')" title="' + path + '">' + label + '</div>';
    }
    results.innerHTML = html;
  }, 250);
}

// wiki 双链点击解析：后端搜同名 md，命中则打开
async function resolveWikiLink(target) {
  const hits = await apiSearch(target);
  const exact = hits.find(f => f.endsWith('/' + target + '.md') || f === target + '.md');
  if (exact) { loadFile(exact); return; }
  if (hits.length) { loadFile(hits[0]); return; }
  alert('未找到链接目标：' + target);
}

/* ===== 图片灯箱 ===== */
function openLightbox(src) {
  document.getElementById('lightbox-img').src = src;
  // 填充链接信息
  const urlEl = document.getElementById('lightbox-img-url');
  urlEl.textContent = src;
  urlEl.title = src;
  document.getElementById('lightbox-img-open').href = src;
  document.getElementById('lightbox-ocr-rendered').innerHTML = '加载中…';
  document.getElementById('lightbox-ocr-raw').textContent = '';
  document.getElementById('lightbox-ocr-source').textContent = '';
  // 重置到渲染视图
  document.getElementById('lightbox-ocr-rendered').style.display = '';
  document.getElementById('lightbox-ocr-raw').style.display = 'none';
  document.getElementById('lightbox-ocr-toggle').textContent = '源码';
  document.getElementById('lightbox').style.display = 'flex';
  fetch('/api/ocr?url=' + encodeURIComponent(src))
    .then(r => r.json())
    .then(d => {
      const text = d.ocr || '（无 OCR 记录）';
      document.getElementById('lightbox-ocr-rendered').innerHTML = marked.parse(text);
      document.getElementById('lightbox-ocr-raw').textContent = text;
      if (d.source) document.getElementById('lightbox-ocr-source').textContent = d.source;
    })
    .catch(() => { document.getElementById('lightbox-ocr-rendered').textContent = '（获取失败）'; });
}

function lightboxCopyUrl() {
  const url = document.getElementById('lightbox-img-url').textContent;
  navigator.clipboard.writeText(url).then(function () {
    const btn = document.querySelector('.url-copy');
    const orig = btn.textContent;
    btn.textContent = '✓ 已复制';
    setTimeout(function () { btn.textContent = orig; }, 1500);
  }).catch(function () {
    // fallback
    const ta = document.createElement('textarea');
    ta.value = url;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    alert('已复制链接');
  });
}

function toggleOcrView() {
  const rendered = document.getElementById('lightbox-ocr-rendered');
  const raw = document.getElementById('lightbox-ocr-raw');
  const btn = document.getElementById('lightbox-ocr-toggle');
  const showingRaw = raw.style.display !== 'none';
  rendered.style.display = showingRaw ? '' : 'none';
  raw.style.display = showingRaw ? 'none' : 'block';
  btn.textContent = showingRaw ? '源码' : '渲染';
}

/* ===== OCR 图片操作工具栏（假实现） ===== */
let lightboxImgSrc = '';

function ocrGetCurrentImg() {
  return document.getElementById('lightbox-img').src;
}

function ocrGetCurrentOcr() {
  return document.getElementById('lightbox-ocr-raw').textContent || '';
}

// 1. 一键洗图  2. 一键OCR复原  3. 修改配图建议  5. 删除该图片
async function ocrAction(action) {
  const imgSrc = ocrGetCurrentImg();
  const ocrText = ocrGetCurrentOcr();
  switch (action) {
    case 'regenerate':
      showToast('🔄 一键洗图 · 待开发\n将当前图片通过 AI 重新生成，保留信息但换一种视觉风格，避免被平台判定为转载。');
      break;
    case 'restore':
      showToast('↩ 一键OCR复原 · 待开发\n根据 OCR 识别出的表格和文字结构，反向生成一张干净的标准图片，替换原图。');
      break;
    case 'resuggest':
      showToast('✏ 修改配图建议 · 待开发\n结合全文上下文和当前图片的 OCR 结构，重新生成更精准的配图建议。');
      break;
    case 'rename':
      showToast('🏷 一键修正名称 · 待开发\n香港保险在内地推广时常用化名（如保诚→保C、宏利→宏L）。此功能将基于名称对照数据库，一键把化名替换为正式名称，也支持大模型智能改写。');
      break;
    case 'delete':
      await ocrDeleteImage(imgSrc);
      break;
  }
}

/* ===== 一键删除图片（真实实现） ===== */
function urlCore(url) {
  let u = url.split('#')[0];
  u = u.split('?')[0];
  u = u.replace(/\/\d+$/, '');
  return u;
}

async function ocrDeleteImage(imgSrc) {
  const core = urlCore(imgSrc);
  const lines = currentContent.split('\n');

  // 1. 找到匹配的图片行 ![...](url)
  let imgIdx = -1;
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(/^!\[.*?\]\((.+)\)$/);
    if (m && urlCore(m[1]) === core) {
      imgIdx = i;
      break;
    }
  }
  if (imgIdx === -1) {
    alert('未在当前文件中找到该图片链接，无法删除。');
    return;
  }

  // 2. 确定删除范围：图片行 + 后续 HTML 注释块 + --- 分隔线
  let end = imgIdx + 1;

  // 跳过空行
  while (end < lines.length && lines[end].trim() === '') end++;

  // 检查是否跟着 HTML 注释块（<!-- 插图建议 / OCR内容 ... -->，可能跨行）
  if (end < lines.length && /^<!--\s*(插图建议|OCR内容)/.test(lines[end].trim())) {
    // 找到 --> 结束行
    while (end < lines.length && !lines[end].includes('-->')) end++;
    if (end < lines.length) end++; // 包含 --> 所在行
  }

  // 跳过空行
  while (end < lines.length && lines[end].trim() === '') end++;

  // 检查是否跟着 --- 分隔线
  if (end < lines.length && lines[end].trim() === '---') {
    end++;
  }

  // 清理删除后遗留的多余空行（向前看）
  while (end < lines.length && lines[end].trim() === '') end++;

  // 3. 确认弹窗
  const shortUrl = imgSrc.split('/').pop().split('?')[0];
  if (!confirm('确认删除该图片及其关联信息？\n\n图片: ' + shortUrl + '\n文件: ' + currentFile + '\n\n此操作不可撤销。')) return;

  // 4. 拼接新内容并保存
  const newLines = lines.slice(0, imgIdx).concat(lines.slice(end));
  const newContent = newLines.join('\n');

  try {
    await apiSave(currentFile, newContent);
    currentContent = newContent;
    renderView(newContent, currentFile);
    document.getElementById('lightbox').style.display = 'none';
    document.getElementById('s-left').textContent = '已删除图片 · ' + currentFile;
    showToast('🗑 已删除图片');
  } catch (e) {
    alert('删除失败：' + e.message);
  }
}

// 正文内悬浮删除按钮
async function inlineDeleteImage(imgSrc) {
  await ocrDeleteImage(imgSrc);
}

// 全库删除同一图片（按 core ID 匹配）
let _bulkDeleteCore = '';

async function inlineDeleteAll(imgSrc) {
  const core = urlCore(imgSrc);
  _bulkDeleteCore = core;

  // 1. 扫描全库
  let scanResult;
  try {
    const r = await fetch('/api/scan-image?core=' + encodeURIComponent(core));
    scanResult = await r.json();
  } catch (e) {
    alert('扫描失败：' + e.message);
    return;
  }
  if (!scanResult.ok || scanResult.total === 0) {
    alert('未在全库找到该图片的其他引用。');
    return;
  }

  // 2. 填充弹窗信息并显示
  const fileList = scanResult.files.map(function (f) {
    return '<div class="bulk-file-item">📄 ' + f.path + ' <span class="bulk-count">' + f.count + ' 张</span></div>';
  }).join('');
  document.getElementById('bulk-delete-info').innerHTML =
    '<div class="bulk-stat">总计 <strong>' + scanResult.total + '</strong> 张图片，分布于 <strong>'
    + scanResult.files.length + '</strong> 个文件</div>'
    + '<div class="bulk-id">图片ID: ' + core.split('/').slice(-2).join('/') + '</div>'
    + '<div class="bulk-warn">将删除：图片链接 + 插图建议注释 + OCR内容注释 + 分隔线 + OCR数据库记录</div>'
    + '<div class="bulk-files">' + fileList + '</div>';
  // 全库删除模式：去掉 data-mode，显示确认按钮，恢复标题
  document.getElementById('bulk-delete-dialog').removeAttribute('data-mode');
  document.getElementById('bulk-delete-dialog-box').querySelector('.dlg-actions').style.display = '';
  document.getElementById('bulk-delete-dialog').querySelector('h3').textContent = '⚠ 全库删除确认';
  document.getElementById('bulk-delete-dialog').style.display = 'flex';
}

async function bulkDeleteConfirm() {
  document.getElementById('bulk-delete-dialog').style.display = 'none';
  const core = _bulkDeleteCore;
  if (!core) return;

  // 3. 调用后端批量删除
  try {
    const r = await fetch('/api/bulk-delete-image', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ core: core })
    });
    const d = await r.json();
    if (!d.ok) {
      alert('删除失败：' + (d.error || '未知错误'));
      return;
    }
    // 4. 刷新当前文件
    if (currentFile) {
      currentContent = await apiRead(currentFile);
      renderView(currentContent, currentFile);
    }
    showToast('🗑 已全库删除 ' + d.deleted_images + ' 张图片（' + d.deleted_files + ' 个文件）');
  } catch (e) {
    alert('删除失败：' + e.message);
  }
}

function bulkDeleteCancel() {
  const dlg = document.getElementById('bulk-delete-dialog');
  dlg.style.display = 'none';
  dlg.removeAttribute('data-mode');
  dlg.querySelector('h3').textContent = '⚠ 全库删除确认';
  dlg.querySelector('.dlg-actions').style.display = '';
  _bulkDeleteCore = '';
}

// 从引用详情跳转到文件
function jumpFromRefs(path) {
  bulkDeleteCancel();
  loadFile(path);
}

// 弹窗背景点击关闭（仅引用详情模式）
document.addEventListener('click', function (e) {
  const dlg = document.getElementById('bulk-delete-dialog');
  if (dlg.style.display !== 'flex') return;
  if (dlg.getAttribute('data-mode') !== 'refs') return;
  // 只在点击弹窗背景（不是内容区）时关闭
  if (e.target === dlg) bulkDeleteCancel();
});

// 4. 根据 OCR 结构出图 → 弹 Prompt 弹窗
function ocrOpenGenDialog() {
  const imgSrc = ocrGetCurrentImg();
  const ocrText = ocrGetCurrentOcr();
  document.getElementById('ocr-gen-info').textContent =
    '基于当前图片的 OCR 识别结构，输入微调指令后生成新图。当前图片: ' + imgSrc.split('/').pop();
  document.getElementById('ocr-gen-prompt').value = '';
  document.getElementById('ocr-gen-dialog').style.display = 'flex';
  document.getElementById('ocr-gen-prompt').focus();
}

function ocrGenCancel() {
  document.getElementById('ocr-gen-dialog').style.display = 'none';
}

function ocrGenConfirm() {
  const prompt = document.getElementById('ocr-gen-prompt').value.trim();
  const imgSrc = ocrGetCurrentImg();
  const ocrText = ocrGetCurrentOcr();
  if (!prompt) {
    alert('请输入出图指令。');
    return;
  }
  showToast('🎨 根据OCR出图 · 待开发\n以 OCR 识别出的表格结构为骨架，结合你的微调指令，调用 AI 生成一张全新的图片。');
  document.getElementById('ocr-gen-dialog').style.display = 'none';
}

/* ===== 键盘快捷键 ===== */
document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') {
    if (document.getElementById('bulk-delete-dialog').style.display === 'flex') { bulkDeleteCancel(); return; }
    if (document.getElementById('ocr-gen-dialog').style.display === 'flex') { ocrGenCancel(); return; }
    if (document.getElementById('edit-dialog').style.display === 'flex') dlgCancel();
  }
  if ((e.metaKey || e.ctrlKey) && e.key === 's') { e.preventDefault(); if (isEditing) saveFile(); }
  if ((e.metaKey || e.ctrlKey) && e.key === 'e') { e.preventDefault(); if (currentFile && !isEditing) toggleEdit(); }
});

/* ===== 编辑器选中 → 悬浮按钮 → 局部编辑 ===== */
let selState = { text: '', mdSegment: '', mdStart: -1, mdEnd: -1 };

// 编辑器模式选中文字 → 悬浮按钮
// 监听在 document 上而非 #editor 上：
// 大范围拖选时鼠标松开位置可能在 textarea 外，
// 挂在元素上的 mouseup 不会触发，document 级别的才能捕获到
document.addEventListener('mouseup', function (e) {
  const ed = document.getElementById('editor');
  if (document.getElementById('edit-dialog').style.display === 'flex') return;
  // 只在编辑模式下处理
  if (!isEditing) return;
  if (document.activeElement !== ed) return;
  const mouseX = e.clientX;
  const mouseY = e.clientY;
  setTimeout(function () {
    const text = ed.value.substring(ed.selectionStart, ed.selectionEnd);
    const tb = document.getElementById('sel-toolbar');
    if (text.trim().length >= 1) {
      selState.text = text;
      const idx = currentContent.indexOf(text);
      if (idx >= 0) {
        selState.mdStart = idx;
        selState.mdEnd = idx + text.length;
        selState.mdSegment = text;
      } else {
        selState.mdStart = -1;
        selState.mdEnd = -1;
        selState.mdSegment = text;
      }
      tb.style.display = 'block';
      const x = Math.min(mouseX + 8, window.innerWidth - 90);
      const y = Math.max(mouseY + 30, 8);
      tb.style.left = x + 'px';
      tb.style.top = y + 'px';
    } else {
      tb.style.display = 'none';
    }
  }, 10);
});

// 计算某偏移在原文中的行号（1-based）
function lineOf(md, offset) {
  let n = 1;
  for (let i = 0; i < offset && i < md.length; i++) if (md[i] === '\n') n++;
  return n;
}

// 打开弹窗编辑指定 block，note 是顶部提示
function openBlockEditor(block, note) {
  const dlg = document.getElementById('edit-dialog');
  const ed = document.getElementById('dlg-editor');
  selState.mdStart = block.start;
  selState.mdEnd = block.end;
  selState.mdSegment = block.text;
  ed.value = block.text;
  const l1 = lineOf(currentContent, block.start);
  const l2 = lineOf(currentContent, block.end - 1);
  const range = l1 === l2 ? ('第 ' + l1 + ' 行') : ('第 ' + l1 + '–' + l2 + ' 行');
  document.getElementById('dlg-info').innerHTML =
    currentFile + ' · ' + range + (note ? ' · <span style="color:#c0392b">' + note + '</span>' : '');
  dlg.style.display = 'flex';
  document.getElementById('sel-toolbar').style.display = 'none';
  ed.focus();
}

// 兜底：列出当前文件所有段落，让用户手动挑要编辑哪一段

function openEditDialog() {
  if (selState.mdStart >= 0 && selState.mdEnd >= 0) {
    const block = { start: selState.mdStart, end: selState.mdEnd, text: selState.mdSegment };
    openBlockEditor(block, '');
  } else {
    alert('未能定位选中文字，请重新选择。');
    document.getElementById('sel-toolbar').style.display = 'none';
  }
}

async function dlgConfirm() {
  const ed = document.getElementById('dlg-editor');
  // 段落选择器模式但未选段 → 阻止保存
  if (selState.mdStart === -1 && selState.mdEnd === -1) {
    alert('请先选择要编辑的段落。');
    return;
  }
  // 内容未改动 → 直接关闭，不写盘、不重新渲染
  if (ed.value === selState.mdSegment) {
    document.getElementById('edit-dialog').style.display = 'none';
    return;
  }
  const updated = currentContent.slice(0, selState.mdStart) + ed.value + currentContent.slice(selState.mdEnd);
  try {
    await apiSave(currentFile, updated);
    currentContent = updated;
    // ★ 保存成功后必须重新渲染整篇文章，保持与最新磁盘内容一致
    renderView(updated, currentFile);
    document.getElementById('edit-dialog').style.display = 'none';
  } catch (e) {
    alert('保存失败：' + e.message);
  }
}

function dlgCancel() {
  const ed = document.getElementById('dlg-editor');
  if (ed.value !== selState.mdSegment) {
    if (!confirm('内容已有改动，确定要放弃修改吗？')) return;
  }
  document.getElementById('edit-dialog').style.display = 'none';
}

/* ===== 启动 ===== */
async function initApp() {
  document.getElementById('current-path').textContent = '加载中…';
  await refreshTree();
  // 默认打开 wiki/index.md（精炼库索引），不存在则提示选择
  try {
    await loadFile('wiki/index.md');
  } catch (e) {
    document.getElementById('current-path').textContent = '选择一个文件开始浏览';
  }
}

initApp();
