(function () {
  const DATA_URL = '/api/monitor-data';
  const CONTENT_API_URL = '/api/article-content';
  const state = { filter: 'all', keyword: null, turnover: null, articleById: new Map() };

  if (window.marked) {
    window.marked.setOptions({
      gfm: true,
      breaks: true
    });
  }

  function $(id) {
    return document.getElementById(id);
  }

  function esc(value) {
    return TurnoverViz.escapeHtml(value);
  }

  function pct(value, digits = 0) {
    return `${(Number(value || 0) * 100).toFixed(digits)}%`;
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, Number(value || 0)));
  }

  function shareText(rate) {
    const value = Math.round(Number(rate || 0) * 100);
    if (value <= 0) return '几乎没换';
    if (value < 10) return '不到1成';
    if (value >= 45 && value <= 55) return '约一半';
    if (value > 90) return '几乎全换';
    return `约${Math.round(value / 10)}成`;
  }

  function shortDate(date) {
    return String(date || '').slice(5);
  }

  function hasRealUrl(url) {
    return !!url && !String(url).startsWith('placeholder://');
  }

  function articleHitDetailHref(article) {
    if (article?.article_id) return `/article-hit-detail?article_id=${encodeURIComponent(article.article_id)}`;
    if (hasRealUrl(article?.url)) return `/article-hit-detail?url=${encodeURIComponent(article.url)}`;
    return '/article-hit-detail';
  }

  function normalizeTitleForCompare(text) {
    return String(text || '')
      .normalize('NFKC')
      .toLowerCase()
      .replace(/\s+/g, '')
      .replace(/[·•|｜:：,，。！？!?—\-_\[\]【】()（）<>《》“”"'‘’`]+/g, '');
  }

  function preprocessArticleMarkdown(md, title) {
    const lines = String(md || '')
      .replace(/\u0000/g, '')
      .replace(/\r\n/g, '\n')
      .replace(/\r/g, '\n')
      .split('\n');
    const out = [];
    const titleKey = normalizeTitleForCompare(title);
    let skippedIntroHeading = false;
    let skippedLinkLine = false;

    for (const rawLine of lines) {
      const line = rawLine.replace(/\u200b/g, '').trimEnd();
      const trimmed = line.trim();
      if (!trimmed) {
        if (out.length && out[out.length - 1] !== '') out.push('');
        continue;
      }
      if (trimmed === 'StartFragment' || trimmed === 'EndFragment') continue;
      if (!skippedLinkLine && /^链接[:：]\s*https?:\/\//.test(trimmed)) {
        skippedLinkLine = true;
        continue;
      }
      if (!skippedIntroHeading && trimmed.startsWith('#')) {
        const headingText = trimmed.replace(/^#+\s*/, '');
        if (normalizeTitleForCompare(headingText) === titleKey) {
          skippedIntroHeading = true;
          continue;
        }
      }
      out.push(line);
    }

    while (out.length && out[0] === '') out.shift();
    while (out.length && out[out.length - 1] === '') out.pop();
    return out.join('\n');
  }

  function runLabel(item) {
    return `${item.date} ${item.time || ''}`.trim();
  }

  function runPairText(item) {
    const sameDay = item.currDate === item.prevDate;
    const curr = `${item.currDate}${item.time ? ` ${item.time}` : ''}`;
    const prev = `${item.prevDate}${item.prevTime ? ` ${item.prevTime}` : ''}`;
    return sameDay ? `同一天两次抓取：${curr} vs ${prev}` : `${curr} vs ${prev}`;
  }

  function stabilityLabel(value) {
    return {
      '常驻': '常驻',
      '活跃': '经常出现',
      '闪现': '偶尔出现'
    }[value] || value;
  }

  function articlePresenceText(article) {
    return `有${article.day_count || 0}天上榜 · 抓到${article.run_appearances || 0}次`;
  }

  function articlePresenceTip(article) {
    const days = state.turnover?.windowDays || 0;
    return `过去${days}天里，这篇文章有${article.day_count || 0}天出现在搜索结果；因为一天可能抓多次，所以系统一共看见${article.run_appearances || 0}次。点击可查看正文。`;
  }

  function articleActionText(article) {
    if (article.content_path) return '看正文';
    if (hasRealUrl(article.url)) return '看原文';
    return '看详情';
  }

  function compactText(value, max = 12) {
    const text = String(value || '').replace(/\s+/g, '');
    return text.length > max ? `${text.slice(0, max)}…` : text;
  }

  function articleRankText(article) {
    const rank = article.rank || article.best_rank || article.latest_rank;
    return rank ? `第${rank}` : '在榜';
  }

  function clearCityStage(stage) {
    if (!stage) return;
    if (typeof stage.__turnoverCleanup === 'function') {
      stage.__turnoverCleanup();
      stage.__turnoverCleanup = null;
    }
    stage.innerHTML = '';
  }

  function renderCityFallback(message) {
    const stage = $('turnoverCity3d');
    clearCityStage(stage);
    if (stage) stage.innerHTML = `<div class="turnover-city-fallback">${esc(message)}</div>`;
  }

  function setupCityStage() {
    const stage = $('turnoverCity3d');
    if (!stage || !window.THREE) return null;
    clearCityStage(stage);

    const THREE = window.THREE;
    const compact = stage.getBoundingClientRect().width < 560;
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0xeaf3ff);
    const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 140);
    camera.position.set(0, compact ? 12.4 : 11.2, compact ? 21 : 19);
    camera.lookAt(0, 0.45, 0);

    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    stage.appendChild(renderer.domElement);

    const labelLayer = document.createElement('div');
    labelLayer.className = 'turnover-city-label-layer';
    stage.appendChild(labelLayer);

    const group = new THREE.Group();
    group.rotation.y = compact ? -0.22 : -0.34;
    scene.add(group);

    const labels = [];
    const interactables = [];
    scene.add(new THREE.HemisphereLight(0xffffff, 0x8aa5bf, 1.08));
    const key = new THREE.DirectionalLight(0xffffff, 1.18);
    key.position.set(-4, 8, 6);
    key.castShadow = true;
    scene.add(key);
    const rim = new THREE.DirectionalLight(0x9fd0ff, 0.45);
    rim.position.set(5, 5, -4);
    scene.add(rim);

    const tooltip = document.createElement('div');
    tooltip.className = 'turnover-city-tooltip';
    stage.appendChild(tooltip);

    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    let hovered = null;
    let rafId = 0;
    let dragging = false;
    let lastX = 0;
    let lastY = 0;
    let targetRotX = group.rotation.x;
    let targetRotY = group.rotation.y;

    function resize() {
      const rect = stage.getBoundingClientRect();
      const width = Math.max(320, Math.floor(rect.width || 760));
      const height = Math.max(360, Math.floor(rect.height || 520));
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
    }

    function setTooltip(event, text) {
      if (!text) {
        tooltip.classList.remove('show');
        tooltip.textContent = '';
        return;
      }
      tooltip.textContent = text;
      tooltip.style.left = `${event.offsetX + 14}px`;
      tooltip.style.top = `${event.offsetY + 14}px`;
      tooltip.classList.add('show');
    }

    function onMove(event) {
      if (dragging) {
        const dx = event.clientX - lastX;
        const dy = event.clientY - lastY;
        lastX = event.clientX;
        lastY = event.clientY;
        targetRotX = clamp(targetRotX + dy * 0.004, -0.5, 0.46);
        targetRotY += dx * 0.006;
      }
      const rect = renderer.domElement.getBoundingClientRect();
      pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
      const hits = raycaster.intersectObjects(interactables, true).filter(hit => hit.object.userData?.tip);
      hovered = hits[0]?.object || null;
      setTooltip(event, hovered?.userData?.tip || '');
    }

    function onDown(event) {
      dragging = true;
      lastX = event.clientX;
      lastY = event.clientY;
      stage.classList.add('is-dragging');
    }

    function onUp() {
      dragging = false;
      stage.classList.remove('is-dragging');
    }

    function onLeave() {
      hovered = null;
      dragging = false;
      setTooltip({ offsetX: 0, offsetY: 0 }, '');
      stage.classList.remove('is-dragging');
    }

    function onClick() {
      const articleId = hovered?.userData?.articleId;
      if (articleId) openTurnoverArticle(articleId);
    }

    renderer.domElement.addEventListener('pointermove', onMove);
    renderer.domElement.addEventListener('pointerdown', onDown);
    window.addEventListener('pointerup', onUp);
    renderer.domElement.addEventListener('pointerleave', onLeave);
    renderer.domElement.addEventListener('click', onClick);

    const resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(stage);
    resize();

    function updateLabels() {
      const rect = stage.getBoundingClientRect();
      const world = new THREE.Vector3();
      labels.forEach(item => {
        item.object.getWorldPosition(world);
        world.project(camera);
        const x = (world.x * 0.5 + 0.5) * rect.width;
        const y = (-world.y * 0.5 + 0.5) * rect.height;
        item.el.style.transform = `translate(-50%, -100%) translate(${x}px, ${y}px)`;
        item.el.style.opacity = world.z < -1 || world.z > 1 ? '0' : item.opacity;
      });
    }

    function animate() {
      rafId = window.requestAnimationFrame(animate);
      group.rotation.x += (targetRotX - group.rotation.x) * 0.05;
      group.rotation.y += (targetRotY - group.rotation.y) * 0.05;
      updateLabels();
      renderer.render(scene, camera);
    }
    animate();

    stage.__turnoverCleanup = () => {
      window.cancelAnimationFrame(rafId);
      resizeObserver.disconnect();
      renderer.domElement.removeEventListener('pointermove', onMove);
      renderer.domElement.removeEventListener('pointerdown', onDown);
      window.removeEventListener('pointerup', onUp);
      renderer.domElement.removeEventListener('pointerleave', onLeave);
      renderer.domElement.removeEventListener('click', onClick);
      scene.traverse(obj => {
        if (obj.geometry) obj.geometry.dispose();
        if (obj.material) {
          if (Array.isArray(obj.material)) obj.material.forEach(item => item.dispose());
          else obj.material.dispose();
        }
      });
      renderer.dispose();
    };

    return { THREE, scene, camera, renderer, group, labels, labelLayer, interactables, compact };
  }

  function addCityLabel(ctx, object, html, className, opacity = 1) {
    const el = document.createElement('div');
    el.className = `turnover-city-label ${className || ''}`.trim();
    el.innerHTML = html;
    ctx.labelLayer.appendChild(el);
    ctx.labels.push({ object, el, opacity });
    return el;
  }

  function addCityAnchor(ctx, x, y, z) {
    const object = new ctx.THREE.Object3D();
    object.position.set(x, y, z);
    ctx.group.add(object);
    return object;
  }

  function addCityBox(ctx, { x, y, z, width, height, depth, color, tip, articleId, opacity = 1 }) {
    const geometry = new ctx.THREE.BoxGeometry(width, height, depth);
    const material = new ctx.THREE.MeshStandardMaterial({
      color,
      roughness: 0.52,
      metalness: 0.08,
      emissive: color,
      emissiveIntensity: 0.045,
      transparent: opacity < 1,
      opacity
    });
    const mesh = new ctx.THREE.Mesh(geometry, material);
    mesh.position.set(x, y + height / 2, z);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    mesh.userData = { tip, articleId };
    ctx.group.add(mesh);
    if (tip || articleId) ctx.interactables.push(mesh);
    return mesh;
  }

  function addCityLine(ctx, from, to, color, radius = 0.028, opacity = 0.85) {
    const THREE = ctx.THREE;
    const start = new THREE.Vector3(from.x, from.y, from.z);
    const end = new THREE.Vector3(to.x, to.y, to.z);
    const dir = new THREE.Vector3().subVectors(end, start);
    const mesh = new THREE.Mesh(
      new THREE.CylinderGeometry(radius, radius, dir.length(), 16),
      new THREE.MeshStandardMaterial({ color, transparent: opacity < 1, opacity, roughness: 0.42 })
    );
    mesh.position.copy(start).add(end).multiplyScalar(0.5);
    mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir.clone().normalize());
    mesh.castShadow = true;
    ctx.group.add(mesh);
    return mesh;
  }

  function addCityFloor(ctx, dates) {
    const THREE = ctx.THREE;
    const floor = new THREE.Mesh(
      new THREE.BoxGeometry(17.8, 0.06, 11.6),
      new THREE.MeshStandardMaterial({ color: 0xd8e7f7, roughness: 0.9 })
    );
    floor.position.set(0, -0.03, 0);
    floor.receiveShadow = true;
    ctx.group.add(floor);
    const grid = new THREE.GridHelper(17.8, 22, 0x7f9fbd, 0xb6c8d9);
    grid.position.y = 0.01;
    ctx.group.add(grid);
    const labels = [
      { z: -4.15, text: '常驻高楼' },
      { z: -1.35, text: '活跃街区' },
      { z: 1.35, text: '短暂出现' },
      { z: 4.15, text: '离场/历史' }
    ];
    labels.forEach(item => {
      addCityLine(ctx, { x: -8.1, y: 0.05, z: item.z }, { x: 8.1, y: 0.05, z: item.z }, '#94a3b8', 0.012, 0.32);
      addCityLabel(ctx, addCityAnchor(ctx, ctx.compact ? -6.6 : -8.2, 0.45, item.z), `<strong>${item.text}</strong>`, 'lane-label', 0.8);
    });
    if (dates.length) {
      [0, Math.floor((dates.length - 1) / 2), dates.length - 1].forEach(idx => {
        const x = -7.6 + (idx / Math.max(1, dates.length - 1)) * 15.2;
        addCityLine(ctx, { x, y: 0.06, z: -5.05 }, { x, y: 0.06, z: 5.1 }, '#bfdbfe', 0.01, 0.58);
        addCityLabel(ctx, addCityAnchor(ctx, x, 0.42, 5.35), `<b>${shortDate(dates[idx])}</b>`, 'date-label', 0.85);
      });
    }
  }

  function buildLatestFlow() {
    const latest = state.turnover.latest || {};
    const runs = state.turnover.windowRuns || [];
    const currRun = runs.find(run => run.id === latest.currRunId) || runs[runs.length - 1] || {};
    const prevRun = runs.find(run => run.id === latest.prevRunId) || runs[runs.length - 2] || {};
    const currArticles = currRun.articles || [];
    const prevArticles = prevRun.articles || [];
    const currMap = new Map(currArticles.map(article => [TurnoverViz.articleKey(article), article]));
    const prevMap = new Map(prevArticles.map(article => [TurnoverViz.articleKey(article), article]));

    function normalize(article) {
      const id = TurnoverViz.articleKey(article);
      const known = state.articleById.get(id);
      const item = {
        ...(known || {}),
        ...article,
        article_id: id,
        latest_rank: article.rank || article.best_rank || known?.latest_rank || null,
        best_rank: article.rank || article.best_rank || known?.best_rank || null
      };
      if (id && !state.articleById.has(id)) state.articleById.set(id, item);
      return item;
    }

    function byRank(a, b) {
      return Number(a.rank || a.best_rank || 99) - Number(b.rank || b.best_rank || 99);
    }

    return {
      latest,
      currRun,
      prevRun,
      stayed: currArticles.filter(article => prevMap.has(TurnoverViz.articleKey(article))).sort(byRank).map(normalize),
      incoming: currArticles.filter(article => !prevMap.has(TurnoverViz.articleKey(article))).sort(byRank).map(normalize),
      outgoing: prevArticles.filter(article => !currMap.has(TurnoverViz.articleKey(article))).sort(byRank).map(normalize)
    };
  }

  function cityStatus(article, flow) {
    if (flow.incoming.some(item => item.article_id === article.article_id)) return 'incoming';
    if (flow.outgoing.some(item => item.article_id === article.article_id)) return 'outgoing';
    if (flow.stayed.some(item => item.article_id === article.article_id)) return 'current';
    return 'history';
  }

  function cityColor(status, stability) {
    if (status === 'incoming') return '#f59e0b';
    if (status === 'outgoing') return '#e11d48';
    if (status === 'current') return '#10b981';
    if (stability === '常驻') return '#2563eb';
    if (stability === '活跃') return '#0ea5e9';
    return '#64748b';
  }

  function buildCityArticles(flow) {
    const priority = { incoming: 5, outgoing: 4, current: 3, history: 1 };
    return state.turnover.articles.map(article => {
      const status = cityStatus(article, flow);
      const activeDays = article.active_days || [];
      const first = article.first_seen || activeDays[0] || state.turnover.windowStart;
      const last = article.last_seen || activeDays[activeDays.length - 1] || first;
      return {
        ...article,
        city_status: status,
        city_priority: priority[status] || 1,
        city_first: first,
        city_last: last
      };
    }).sort((a, b) => (
      b.city_priority - a.city_priority ||
      b.day_count - a.day_count ||
      b.run_appearances - a.run_appearances ||
      a.title.localeCompare(b.title, 'zh-CN')
    ));
  }

  function cityLaneKey(article) {
    if (article.city_status === 'outgoing') return 'outgoing';
    if (article.stability === '常驻') return 'stable';
    if (article.stability === '活跃') return 'active';
    return 'flash';
  }

  function cityLaneBase(key) {
    return ({ stable: -4.15, active: -1.35, flash: 1.35, outgoing: 4.15 })[key] ?? 1.35;
  }

  function assignCityLayout(articles, dates, compact) {
    const groups = new Map();
    const statusCounts = {};
    articles.forEach((article, displayIdx) => {
      const firstIdx = Math.max(0, dates.indexOf(article.city_first));
      const rawLastIdx = dates.indexOf(article.city_last);
      const lastIdx = rawLastIdx >= 0 ? rawLastIdx : firstIdx;
      const statusKey = article.city_status || 'history';
      article.city_display_idx = displayIdx;
      article.city_status_order = statusCounts[statusKey] || 0;
      statusCounts[statusKey] = article.city_status_order + 1;
      article.city_first_idx = Math.min(firstIdx, lastIdx);
      article.city_last_idx = Math.max(firstIdx, lastIdx);
      article.city_lane_key = cityLaneKey(article);
      if (!groups.has(article.city_lane_key)) groups.set(article.city_lane_key, []);
      groups.get(article.city_lane_key).push(article);
    });

    groups.forEach(group => {
      const tracks = [];
      group
        .sort((a, b) => (
          a.city_first_idx - b.city_first_idx ||
          a.city_last_idx - b.city_last_idx ||
          b.city_priority - a.city_priority
        ))
        .forEach(article => {
          let trackIdx = tracks.findIndex(endIdx => article.city_first_idx > endIdx + 1);
          if (trackIdx < 0) {
            trackIdx = tracks.length;
            tracks.push(-1);
          }
          tracks[trackIdx] = article.city_last_idx;
          article.city_track = trackIdx;
        });

      const laneCount = Math.max(1, tracks.length);
      const spread = compact ? 1.35 : 2.15;
      const trackGap = laneCount <= 1 ? 0 : Math.min(compact ? 0.42 : 0.56, spread / (laneCount - 1));
      group.forEach(article => {
        article.city_lane_count = laneCount;
        article.city_z = cityLaneBase(article.city_lane_key) + (article.city_track - (laneCount - 1) / 2) * trackGap;
      });
    });

    return articles.sort((a, b) => a.city_display_idx - b.city_display_idx);
  }

  function addCityBuilding(ctx, article, idx, dates, maxRuns) {
    const firstIdx = article.city_first_idx ?? Math.max(0, dates.indexOf(article.city_first));
    const lastIdx = article.city_last_idx ?? firstIdx;
    const daySpan = Math.max(1, lastIdx - firstIdx + 1);
    const xScale = 15.2 / Math.max(1, dates.length - 1);
    const width = Math.max(0.28, daySpan * xScale * 0.68);
    const x = -7.6 + ((firstIdx + lastIdx) / 2) * xScale;
    const z = Number.isFinite(article.city_z) ? article.city_z : cityLaneBase(cityLaneKey(article));
    const height = 0.18 + Math.min(1.65, Number(article.run_appearances || 1) / Math.max(1, maxRuns) * 1.65);
    const color = cityColor(article.city_status, article.stability);
    addCityBox(ctx, {
      x,
      y: 0,
      z,
      width,
      height,
      depth: 0.24,
      color,
      articleId: article.article_id,
      opacity: article.city_status === 'history' ? 0.76 : 0.96,
      tip: `${article.title || '未知文章'} · ${article.account || '未知账号'} · ${article.city_first}→${article.city_last} · 抓到${article.run_appearances || 0}次`
    });
    if (article.city_status === 'incoming' || article.city_status === 'outgoing') {
      const beaconColor = article.city_status === 'incoming' ? '#f59e0b' : '#ef4b63';
      const beaconX = -7.6 + lastIdx * xScale;
      addCityBox(ctx, {
        x: beaconX,
        y: height + 0.04,
        z,
        width: 0.32,
        height: 0.72,
        depth: 0.32,
        color: beaconColor,
        articleId: article.article_id,
        tip: `${article.city_status === 'incoming' ? '最新新进' : '刚刚掉出'} · ${article.title || '未知文章'}`
      });
    }
    const eventLabelLimit = ctx.compact ? 1 : 2;
    const currentLabelLimit = ctx.compact ? 1 : 2;
    const important = ((article.city_status === 'incoming' || article.city_status === 'outgoing') && article.city_status_order < eventLabelLimit)
      || (article.city_status === 'current' && article.city_status_order < currentLabelLimit);
    if (important) {
      addCityLabel(
        ctx,
        addCityAnchor(ctx, x, height + 0.5, z),
        `<b>${esc(compactText(article.title || '未知文章', ctx.compact ? 9 : 12))}</b><span>${esc(compactText(article.account || '未知账号', 7))} · ${article.day_count || 0}天</span>`,
        `is-${article.city_status}`
      );
    }
  }

  function renderCityReadout(flow, articles) {
    const tallest = [...articles].sort((a, b) => Number(b.run_appearances || 0) - Number(a.run_appearances || 0))[0];
    const longest = [...articles].sort((a, b) => Number(b.day_count || 0) - Number(a.day_count || 0))[0];
    $('turnoverCityStat').textContent = `${articles.length}栋楼 · 新${flow.incoming.length} · 掉${flow.outgoing.length}`;
    $('turnoverCityReadout').innerHTML = `
      <div><span>最高楼</span><strong>${tallest ? esc(compactText(tallest.title, 14)) : '暂无'}</strong><small>${tallest ? `抓到 ${tallest.run_appearances || 0} 次 · ${esc(tallest.account || '未知账号')}` : ''}</small></div>
      <div><span>最长街区</span><strong>${longest ? `${longest.day_count || 0}天` : '暂无'}</strong><small>${longest ? esc(compactText(longest.title, 18)) : ''}</small></div>
      <div><span>最新新进</span><strong>${flow.incoming[0] ? esc(compactText(flow.incoming[0].title, 14)) : '暂无'}</strong><small>${flow.incoming[0] ? esc(flow.incoming[0].account || '未知账号') : '最近一次没有新进文章'}</small></div>
      <div><span>刚刚掉出</span><strong>${flow.outgoing[0] ? esc(compactText(flow.outgoing[0].title, 14)) : '暂无'}</strong><small>${flow.outgoing[0] ? esc(flow.outgoing[0].account || '未知账号') : '最近一次没有掉出文章'}</small></div>`;
  }

  function renderCity3d() {
    if (!window.THREE) {
      renderCityFallback('3D 组件加载失败，二维列表仍可正常查看。');
      return;
    }
    if (!state.turnover?.latest) {
      renderCityFallback('暂无足够快照生成文章城市。');
      return;
    }
    const ctx = setupCityStage();
    if (!ctx) return;
    const flow = buildLatestFlow();
    const articles = assignCityLayout(buildCityArticles(flow).slice(0, ctx.compact ? 20 : 32), state.turnover.dates || [], ctx.compact);
    const maxRuns = Math.max(1, ...articles.map(article => Number(article.run_appearances || 0)));
    addCityFloor(ctx, state.turnover.dates || []);
    articles.forEach((article, idx) => addCityBuilding(ctx, article, idx, state.turnover.dates || [], maxRuns));
    renderCityReadout(flow, articles);
  }

  async function load() {
    const params = new URLSearchParams(window.location.search);
    const keywordId = params.get('keyword_id') || '';
    const keywordText = params.get('keyword') || '';

    try {
      const resp = await fetch(DATA_URL, { cache: 'no-store' });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const payload = await resp.json();
      const keywords = payload.keywords || [];
      const keyword = keywords.find(item => item.keyword_id === keywordId)
        || keywords.find(item => item.keyword === keywordText)
        || keywords[0];

      if (!keyword) {
        renderError('没有找到任何关键词数据');
        return;
      }

      const turnover = TurnoverViz.calc(keyword.runs || []);
      if (!turnover) {
        renderError(`关键词「${keyword.keyword || ''}」还没有足够快照，暂时无法计算轮换率`);
        return;
      }

      state.keyword = keyword;
      state.turnover = turnover;
      state.articleById = new Map(turnover.articles.map(article => [article.article_id, article]));
      renderAll(payload);
    } catch (err) {
      renderError(`加载失败：${err.message || err}`);
    }
  }

  function renderError(message) {
    $('turnoverHero').innerHTML = `<div class="turnover-loading is-error">${esc(message)}</div>`;
    $('turnoverCalendar').innerHTML = '';
    $('turnoverHandoff').innerHTML = '';
    $('turnoverCity3d').innerHTML = '';
    $('turnoverCityReadout').innerHTML = '';
    $('turnoverLifeGrid').innerHTML = '';
  }

  function renderAll(payload) {
    const { keyword, turnover } = state;
    const back = $('turnoverBackLink');
    const keywordParam = encodeURIComponent(keyword.keyword || '');
    back.href = `/?keyword=${keywordParam}`;
    $('turnoverTopMeta').textContent = payload.generated_at
      ? `数据生成：${payload.generated_at}`
      : `${turnover.snapshotCount} 次快照 · ${turnover.numComparisons} 次对比`;

    renderHero();
    renderCalendar();
    renderHandoff();
    renderCity3d();
    bindCityFullscreen();
    renderLifeFilters();
    renderLife();
  }

  function renderHero() {
    const { keyword, turnover } = state;
    const meta = TurnoverViz.statusMeta ? TurnoverViz.statusMeta(turnover) : TurnoverViz.levelMeta(turnover.rate);
    const mature = TurnoverViz.isMature ? TurnoverViz.isMature(turnover) : true;
    const counts = turnover.stabilityCounts || {};
    const averageShare = shareText(turnover.rate);
    const explain = mature
      ? `${pct(turnover.rate)} 的意思：过去${turnover.windowDays}天里，平均每次抓取后，${averageShare}上榜笔记和上一次不一样。它不是点赞或收藏量，也不是热度，只看“榜单里的笔记有没有换人”。`
      : `当前原始换新率是 ${pct(turnover.rate)}，但${meta.reason || '样本还在积累'}，所以暂不归入“换得很快/换得明显/小幅换新/基本没变”四档。`;
    $('turnoverHero').innerHTML = `
      <div class="turnover-hero-main">
        <div class="turnover-hero-rate" style="color:${meta.color}">${(turnover.rate * 100).toFixed(0)}</div>
        <div class="turnover-hero-copy">
          <div class="turnover-hero-kicker">关键词</div>
          <h1>${esc(keyword.keyword || '')}</h1>
          <div class="turnover-hero-level" style="color:${meta.color}">${meta.label}</div>
          <p>${esc(explain)}</p>
        </div>
      </div>
      <div class="turnover-hero-stats">
        <div><span>观察时间</span><strong>${shortDate(turnover.windowStart)} → ${shortDate(turnover.windowEnd)}</strong></div>
        <div><span>出现过的文章</span><strong>${turnover.distinctArticles} / ${turnover.distinctArticlesTotal}</strong></div>
        <div><span>常驻 / 经常 / 偶尔</span><strong>${counts['常驻'] || 0} / ${counts['活跃'] || 0} / ${counts['闪现'] || 0}</strong></div>
        <div><span>最近一次</span><strong>${shareText(turnover.lastRate)}变了</strong></div>
      </div>`;
  }

  function renderCalendar() {
    const { turnover } = state;
    const colCount = turnover.dates.length;
    const labelEvery = colCount > 24 ? 5 : colCount > 14 ? 3 : 2;
    $('turnoverCalendarStat').textContent = `比了 ${turnover.numComparisons} 次`;

    const header = [
      '<div class="turnover-cal-label"></div>',
      ...turnover.dates.map((date, idx) => (
        `<div class="turnover-cal-label">${idx % labelEvery === 0 || idx === colCount - 1 ? shortDate(date) : ''}</div>`
      ))
    ].join('');

    const cells = [
      '<div class="turnover-cal-label row-label">换新</div>',
      ...turnover.dayRates.map(day => {
        if (!day.count) return '<div class="turnover-cal-cell empty"></div>';
        const title = `${day.date} · ${day.count}次对比 · 最高${shareText(day.maxRate)}文章变了 · 当天平均${pct(day.avgRate, 1)}`;
        return `<div class="turnover-cal-cell" title="${esc(title)}" style="background:${TurnoverViz.rateColor(day.maxRate)}"><span>${Math.round(day.maxRate * 100)}</span></div>`;
      })
    ].join('');

    const runs = [
      '<div class="turnover-cal-label row-label">快照</div>',
      ...turnover.dates.map(date => {
        const count = turnover.daySnapshotCounts[date] || 0;
        const cls = count ? 'has-runs' : 'empty';
        return `<div class="turnover-cal-run ${cls}" title="${esc(`${date} · ${count}次快照`)}">${count || ''}</div>`;
      })
    ].join('');

    $('turnoverCalendar').style.setProperty('--cols', colCount);
    $('turnoverCalendar').innerHTML = header + cells + runs;
    $('turnoverLegend').innerHTML = `
      <span>绿=变化少</span>
      <i style="background:#1a8754"></i>
      <i style="background:#86d49b"></i>
      <i style="background:#f5d36e"></i>
      <i style="background:#f59e0b"></i>
      <i style="background:#ef4444"></i>
      <i style="background:#b91c1c"></i>
      <span>红=换得多</span>`;
  }

  function renderHandoffColumn(title, items, tone, emptyText) {
    const visible = items.slice(0, 5);
    const rows = visible.map(article => `
      <button class="turnover-handoff-card is-${tone}" type="button" data-article-id="${esc(article.article_id)}">
        <b>${esc(compactText(article.title || '未知文章', 24))}</b>
        <span>${esc(article.account || '未知账号')} · ${esc(articleRankText(article))}</span>
      </button>`).join('');
    const more = items.length > visible.length ? `<div class="turnover-handoff-more">还有 ${items.length - visible.length} 篇</div>` : '';
    return `
      <div class="turnover-handoff-col">
        <div class="turnover-handoff-title is-${tone}"><strong>${title}</strong><span>${items.length}篇</span></div>
        ${rows || `<div class="turnover-handoff-empty">${emptyText}</div>`}
        ${more}
      </div>`;
  }

  function renderHandoff() {
    const flow = buildLatestFlow();
    $('turnoverHandoff').innerHTML = [
      renderHandoffColumn('新进榜', flow.incoming, 'incoming', '这次没有新文章冲进来'),
      renderHandoffColumn('刚掉出', flow.outgoing, 'outgoing', '这次没有文章掉出'),
      renderHandoffColumn('继续占位', flow.stayed, 'current', '没有文章连续留在榜单')
    ].join('');
    $('turnoverHandoff').querySelectorAll('[data-article-id]').forEach(card => {
      card.addEventListener('click', () => openTurnoverArticle(card.dataset.articleId));
    });
  }

  function bindCityFullscreen() {
    const button = $('turnoverCityFullscreen');
    const section = $('turnoverCitySection');
    if (!button || !section || button.dataset.bound === '1') return;
    button.dataset.bound = '1';
    button.addEventListener('click', async () => {
      try {
        if (document.fullscreenElement) {
          await document.exitFullscreen();
        } else if (section.requestFullscreen) {
          await section.requestFullscreen();
        } else {
          section.classList.toggle('is-expanded');
        }
      } catch (err) {
        section.classList.toggle('is-expanded');
      }
      button.textContent = section.classList.contains('is-expanded') || document.fullscreenElement === section ? '缩小' : '最大化';
      window.setTimeout(renderCity3d, 160);
    });
    document.addEventListener('fullscreenchange', () => {
      const active = document.fullscreenElement === section;
      section.classList.toggle('is-expanded', active);
      button.textContent = active ? '缩小' : '最大化';
      window.setTimeout(renderCity3d, 160);
    });
  }

  function renderLifeFilters() {
    const counts = { all: state.turnover.articles.length, ...(state.turnover.stabilityCounts || {}) };
    $('turnoverLifeFilters').innerHTML = ['all', '常驻', '活跃', '闪现'].map(filter => {
      const label = filter === 'all' ? '全部' : stabilityLabel(filter);
      const active = state.filter === filter ? 'active' : '';
      return `<button class="turnover-filter-pill ${active}" data-filter="${filter}">${label} ${counts[filter] || 0}</button>`;
    }).join('');

    $('turnoverLifeFilters').querySelectorAll('button').forEach(button => {
      button.addEventListener('click', () => {
        state.filter = button.dataset.filter || 'all';
        renderLifeFilters();
        renderLife();
      });
    });
  }

  function renderLife() {
    const { turnover } = state;
    const dates = turnover.dates;
    const visible = turnover.articles.filter(article => state.filter === 'all' || article.stability === state.filter);
    const labelEvery = dates.length > 24 ? 5 : dates.length > 14 ? 3 : 2;

    const headCells = dates.map((date, idx) => (
      `<div class="turnover-life-cell head">${idx % labelEvery === 0 || idx === dates.length - 1 ? shortDate(date) : ''}</div>`
    )).join('');

    const rows = visible.map(article => {
      const presence = turnover.articlePresence[article.article_id] || {};
      const runMap = turnover.articleDayRuns[article.article_id] || {};
      const cells = dates.map((date, idx) => {
        const present = presence[date] === 1;
        const prevPresent = idx > 0 ? presence[dates[idx - 1]] === 1 : false;
        const nextPresent = idx < dates.length - 1 ? presence[dates[idx + 1]] === 1 : false;
        const runCount = runMap[date] || 0;
        const stateName = TurnoverViz.getLifeState({
          present,
          prevPresent,
          nextPresent,
          runCount,
          firstSeen: article.first_seen,
          date
        });
        const title = present
          ? `${article.title} · ${article.account} · ${date} · ${TurnoverViz.stateLabel(stateName)} · 当日命中${runCount}次`
          : `${date} · 当天没上榜`;
        return `<div class="turnover-life-cell ${stateName}" title="${esc(title)}"></div>`;
      }).join('');
      const rowTip = `${article.title} · ${article.account || '未知账号'} · ${articlePresenceTip(article)}`;
      const rowLabel = `${article.title}，${articlePresenceText(article)}，${articleActionText(article)}`;

      return `
        <div class="turnover-life-row is-clickable" role="button" tabindex="0" aria-label="${esc(rowLabel)}" data-article-id="${esc(article.article_id)}" style="--cols:${dates.length}" title="${esc(rowTip)}">
          <div class="turnover-life-name">
            <span class="turnover-stability ${article.stability}">${stabilityLabel(article.stability)}</span>
            <span class="turnover-life-text">
              <b>${esc(article.title)}</b>
              <small>${esc(articlePresenceText(article))}</small>
            </span>
            <span class="turnover-life-open">${articleActionText(article)}</span>
          </div>
          ${cells}
        </div>`;
    }).join('');

    $('turnoverLifeGrid').innerHTML = `
      <div class="turnover-life-row head" style="--cols:${dates.length}">
        <div class="turnover-life-name muted">文章（点一行看正文） · ${visible.length} 篇</div>
        ${headCells}
      </div>
      ${rows || '<div class="turnover-empty">当前筛选下暂无文章</div>'}`;
    bindLifeRows();

    $('turnoverStateLegend').innerHTML = [
      ['state-new', '第一次看到'],
      ['state-return', '消失后又出现'],
      ['state-short', '短暂停留'],
      ['state-stable', '连续在榜'],
      ['state-core', '同一天多次抓到'],
      ['absent', '当天没上榜']
    ].map(([cls, label]) => `<span><i class="${cls}"></i>${label}</span>`).join('');
  }

  function bindLifeRows() {
    $('turnoverLifeGrid').querySelectorAll('.turnover-life-row.is-clickable').forEach(row => {
      row.addEventListener('click', () => openTurnoverArticle(row.dataset.articleId));
      row.addEventListener('keydown', event => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        openTurnoverArticle(row.dataset.articleId);
      });
    });
  }

  async function openTurnoverArticle(articleId) {
    const article = state.articleById.get(articleId);
    if (!article) return;

    const drawer = $('drawer');
    const title = $('drawerTitle');
    const body = $('drawerBody');
    const foot = $('drawerFoot');
    if (!drawer || !title || !body || !foot) return;

    title.textContent = article.title || '文章详情';
    drawer.classList.add('open');
    body.innerHTML = '<div style="color:#94a3b8">正在加载正文…</div>';

    let html = '';
    if (article.content_path) {
      try {
        const resp = await fetch(`${CONTENT_API_URL}?path=${encodeURIComponent(article.content_path)}`, { cache: 'no-store' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const payload = await resp.json();
        const md = preprocessArticleMarkdown(payload.markdown || '', article.title);
        html = window.marked
          ? window.marked.parse(md).replace(/<img\s/gi, '<img referrerpolicy="no-referrer" ')
          : `<pre>${esc(md)}</pre>`;
      } catch (err) {
        html = `<div style="color:#991b1b">无法加载正文：${esc(err.message || err)}</div>
                <div style="margin-top:8px;font-size:12px;color:#999">路径：${esc(article.content_path)}</div>`;
      }
    } else if (hasRealUrl(article.url)) {
      html = `<div style="color:#64748b">这篇文章暂时没有本地正文，但可以打开原文。</div>
              <div style="margin-top:8px;font-size:12px;color:#999">原文链接：<a href="${esc(article.url)}" target="_blank" rel="noopener">${esc(article.url)}</a></div>`;
    } else {
      html = `<div style="color:#64748b">当前只有榜单记录，还没有抓到正文或原文链接。</div>
              <div style="margin-top:8px;font-size:12px;color:#999">后续如果抓到正文，这里会自动显示。</div>`;
    }
    body.innerHTML = html;

    const footParts = [];
    if (article.account) footParts.push(esc(article.account));
    if (state.keyword?.keyword) footParts.push(esc(state.keyword.keyword));
    footParts.push(esc(articlePresenceText(article)));
    if (article.latest_rank) footParts.push(`最近第${esc(article.latest_rank)}名`);
    if (article.best_rank) footParts.push(`最好第${esc(article.best_rank)}名`);
    if (article.published_at) footParts.push(esc(article.published_at));
    if (article.read_count != null) footParts.push(`阅读${esc(article.read_count)}`);
    if (article.like_count != null) footParts.push(`赞${esc(article.like_count)}`);
    if (hasRealUrl(article.url)) footParts.push(`<a href="${esc(article.url)}" target="_blank" rel="noopener" style="color:#3b82f6">原文</a>`);
    footParts.push(`<a href="${esc(articleHitDetailHref(article))}" style="color:#3b82f6">命中详情</a>`);
    foot.innerHTML = footParts.map(part => `<span>${part}</span>`).join('');
  }

  function closeDrawer() {
    const drawer = $('drawer');
    if (drawer) drawer.classList.remove('open');
  }

  window.closeDrawer = closeDrawer;
  window.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeDrawer();
  });

  load();
})();
