/* ContentOS bridge: original WritingMoney UI remains intact, but static demo data
   is replaced by real Hub jobs before the page is released to the user. */
(function () {
  'use strict';
  var mask = document.getElementById('realDataMask');

  function showState(title, detail) {
    if (!mask) return;
    mask.innerHTML = '<strong>' + escapeHtml(title) + '</strong><span>' + escapeHtml(detail || '') + '</span>';
    mask.classList.add('is-final');
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function payloadOf(item) {
    return item && item.payload && typeof item.payload === 'object' ? item.payload : {};
  }

  function providerLabel(item) {
    var kind = String(item && item.provider_kind || '');
    var status = String(item && item.provider_status || '');
    if (kind === 'fake' || kind === 'fake_provider' || kind === 'demo') {
      return '当前使用 Fake Provider（仅本机演示，不代表真实 AI 生成）';
    }
    if (kind || status) return 'Provider 状态：' + (kind || '未配置') + ' / ' + (status || 'unknown');
    return 'Provider 状态由 Hub 任务回执提供';
  }

  function mapStatus(status) {
    if (status === 'succeeded') return 'done';
    if (status === 'blocked') return 'failed';
    if (status === 'queued') return 'waiting';
    return status || 'waiting';
  }

  function mapJob(item) {
    var p = payloadOf(item);
    var topic = String(p.topic || item.job_id || '未命名任务');
    var isMother = item.job_type === 'mother_forge';
    var providerNote = providerLabel(item);
    var purpose = String(p.purpose || p.requirements && p.requirements.purpose || '');
    var storedStage = String(p.stage || '');
    var stage = storedStage || (item.status === 'succeeded' ? 'done' : 'decision');
    var storedPlan = p.plan && typeof p.plan === 'object' ? p.plan : {};
    var urlMaterials = Array.isArray(p.url_materials) ? p.url_materials : (
      Array.isArray(p.urls) ? p.urls.map(function (url, index) {
        return { id: 'url-' + index, type: 'url', title: String(url), path: String(url), url: String(url), usage: 'reference', reason: '来自 Hub 任务输入', points: [], parseStatus: 'received' };
      }) : []
    );
    return {
      id: item.job_id,
      title: topic,
      topic: topic,
      folder: String(p.output_dir || ('Hub/' + item.job_id + '/')),
      purpose: purpose || '未填写写作目的。',
      status: stage === 'done' || item.status === 'succeeded' ? 'done' : mapStatus(item.status),
      stage: stage,
      updated: item.updated_at || item.created_at || '',
      category: isMother ? '母文章铸造' : '批量成稿',
      archived: false,
      materials: Array.isArray(p.materials) ? p.materials : [],
      urlMaterials: urlMaterials,
      templates: Array.isArray(p.templates) ? p.templates : [],
      plan: {
        titleDirection: '等待真实写作决策回执',
        core: providerNote + '。正文、产物路径和审计仍以 Hub 回执为准。',
        outline: ['读取已持久化任务输入', '等待真实 Provider 或人工确认', '通过审计后进入下一阶段'],
        close: '真实生成完成后再进入发布链路。',
        ...storedPlan
      },
      json: p
    };
  }

  function mapBatch(item) {
    var p = payloadOf(item);
    var state = p.batch_state && typeof p.batch_state === 'object' ? p.batch_state : {};
    var keywords = Array.isArray(state.keywords) ? state.keywords : (
      Array.isArray(p.keywords) ? p.keywords.map(function (keyword, index) {
        return { id: item.job_id + '-kw-' + index, keyword: String(keyword), count: 1, readiness: 'needs-mother' };
      }) : []
    );
    var name = String(p.topic || item.job_id || '未命名批次');
    var providerNote = providerLabel(item);
    return {
      id: item.job_id,
      name: name,
      source: String(state.source || p.source || 'Hub'),
      brief: String(state.brief || p.requirements && p.requirements.brief || p.topic || '') + ' · ' + providerNote,
      status: state.status === 'done' || state.stage === 'batch-done' ? 'done' : 'pending',
      createdAt: item.created_at || '',
      updatedAt: item.updated_at || '',
      outputDir: String(state.output_dir || p.output_dir || ('Hub/' + item.job_id + '/')),
      publishHandoff: Boolean(state.publish_handoff),
      stage: String(state.stage || 'batch-config'),
      keywords: keywords.map(function (keyword, index) {
        return {
          id: String(keyword.id || item.job_id + '-kw-' + index),
          keyword: String(keyword.keyword || keyword),
          purpose: String(keyword.purpose || ''),
          signal: String(keyword.signal || 'medium'),
          signalReason: String(keyword.signalReason || 'Hub 任务输入'),
          count: Number(keyword.count || 0),
          recommendedCount: Number(keyword.recommendedCount || keyword.count || 0),
          hookId: String(keyword.hookId || 'hook-plan'),
          motherMatches: Array.isArray(keyword.motherMatches) ? keyword.motherMatches : [],
          readiness: keyword.readiness === 'ready' ? 'ready' : 'needs-mother'
        };
      }),
      hubQueue: Array.isArray(state.queue) ? state.queue : []
    };
  }

  function emptyJob(mode) {
    return {
      id: 'empty-' + mode,
      title: mode === 'batch' ? '暂无真实批量成稿任务' : '暂无真实母文章铸造任务',
      topic: '',
      folder: 'Hub/',
      purpose: '当前页面只展示 Hub 已持久化的真实任务。请点击“新建”创建任务；Provider 结果会明确标记为真实或演示。',
      status: 'failed',
      stage: 'decision',
      updated: '',
      category: mode === 'batch' ? '批量成稿' : '母文章铸造',
      archived: false,
      materials: [], urlMaterials: [], templates: [],
      plan: {
        titleDirection: '等待真实任务',
        core: '暂无可展示的真实任务。',
        outline: ['创建任务', '等待真实 Provider 或人工确认', '进入审计链路'],
        close: '没有 Hub 回执时不生成正文。'
      },
      json: {}
    };
  }

  function emptyBatch() {
    return {
      id: 'empty-batch',
      name: '暂无真实批量成稿任务',
      source: 'Hub',
      brief: '请点击“新建批次”创建真实任务。',
      status: 'pending',
      createdAt: '', updatedAt: '', outputDir: 'Hub/',
      publishHandoff: false, keywords: []
    };
  }

  function reloadFromHub() {
    window.location.reload();
  }

  function postMutation(jobId, action, value) {
    return fetch('/api/v1/writing/jobs/' + encodeURIComponent(jobId) + '/mutate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
      body: JSON.stringify({action: action, value: value || {}, operator: 'user'})
    }).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok || !body || body.ok === false) {
          throw new Error((body && (body.detail || body.error)) || 'Hub 返回 HTTP ' + response.status);
        }
        return body;
      });
    });
  }

  function runJob(jobId) {
    return fetch('/api/v1/writing/jobs/' + encodeURIComponent(jobId) + '/run', {method: 'POST'})
      .then(function (response) {
        return response.json().then(function (body) {
          if (!response.ok) throw new Error((body && body.detail) || 'Hub 返回 HTTP ' + response.status);
          return body;
        });
      });
  }

  function activeBatchSnapshot() {
    var batch = typeof getActiveBatch === 'function' ? getActiveBatch() : null;
    if (!batch) return null;
    return JSON.parse(JSON.stringify({
      name: batch.name,
      source: batch.source,
      brief: batch.brief,
      output_dir: batch.outputDir,
      publish_handoff: Boolean(batch.publishHandoff),
      stage: activeStage === 'batch-done' ? 'batch-done' : 'batch-config',
      status: batch.status,
      keywords: Array.isArray(batch.keywords) ? batch.keywords : [],
      queue: Array.isArray(batch.hubQueue) ? batch.hubQueue : []
    }));
  }

  function persistBatchThen(action, value, after) {
    var jobId = activeBatchId;
    var snapshot = activeBatchSnapshot();
    if (!jobId || String(jobId).indexOf('empty-') === 0 || !snapshot) {
      showState('无法保存', '当前没有可持久化的真实批次。');
      return;
    }
    postMutation(jobId, 'batch_state', {state: snapshot})
      .then(function () { return action ? postMutation(jobId, action, value || {}) : null; })
      .then(function () { if (after) return after(); })
      .then(reloadFromHub)
      .catch(function (error) { showState('保存失败', error.message || '未获得真实回执'); });
  }

  function interceptWrites() {
    document.addEventListener('click', function (event) {
      var target = event.target && event.target.closest ? event.target.closest('button, [data-action], .template-card, .seg, .stepper-btn') : null;
      if (!target) return;
      if (target.id === 'confirmNewJob') {
        event.preventDefault(); event.stopImmediatePropagation();
        var topic = String(document.getElementById('topicInput').value || '').trim();
        var purpose = String(document.getElementById('purposeInput').value || '').trim();
        if (!topic || !purpose) { showState('创建失败', '选题和写作目的不能为空。'); return; }
        fetch('/api/v1/writing/jobs', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mode: 'mother_forge', topic: topic, purpose: purpose}) })
          .then(function (r) { if (!r.ok) throw new Error('Hub 返回 HTTP ' + r.status); return r.json(); })
          .then(reloadFromHub).catch(function (error) { showState('创建失败', error.message || '未获得真实回执'); });
        return;
      }
      if (target.id === 'confirmNewBatch') {
        event.preventDefault(); event.stopImmediatePropagation();
        var name = String(document.getElementById('batchNameInput').value || '').trim();
        var brief = String(document.getElementById('batchBriefInput').value || '').trim();
        var raw = String(document.getElementById('batchKeywordsInput').value || '').trim();
        if (!name || !raw) { showState('创建失败', '批次名称和关键词不能为空。'); return; }
        var outputDir = String(document.getElementById('batchOutputDirInput').value || '').trim();
        fetch('/api/v1/writing/jobs', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mode: 'batch_production', topic: name, source: 'manual', requirements: {brief: brief, output_dir: outputDir}, keywords: raw.split(/\n+/).filter(Boolean), target_article_count: 1}) })
          .then(function (r) { if (!r.ok) throw new Error('Hub 返回 HTTP ' + r.status); return r.json(); })
          .then(reloadFromHub).catch(function (error) { showState('创建失败', error.message || '未获得真实回执'); });
        return;
      }
      if (target.id === 'purposeConfirmPrimary') {
        event.preventDefault(); event.stopImmediatePropagation();
        var purposeJobId = String(document.getElementById('purposeModal').dataset.jobId || activeJobId);
        var purpose = String(document.getElementById('purposeEditor').value || '').trim();
        if (!purpose) { showState('保存失败', '写作目的不能为空。'); return; }
        postMutation(purposeJobId, 'purpose', {purpose: purpose})
          .then(reloadFromHub).catch(function (error) { showState('保存失败', error.message || '未获得真实回执'); });
        return;
      }
      if (target.id === 'confirmUrlModal') {
        event.preventDefault(); event.stopImmediatePropagation();
        var urlJobId = String(document.getElementById('urlModal').dataset.jobId || activeJobId);
        var rawUrl = String(document.getElementById('urlInput').value || '').trim();
        var note = String(document.getElementById('urlNoteInput').value || '').trim();
        if (!/^https?:\/\/[^\\s]+$/i.test(rawUrl)) { showState('保存失败', 'URL 格式不合法。'); return; }
        postMutation(urlJobId, 'add_url', {url: rawUrl, note: note})
          .then(reloadFromHub).catch(function (error) { showState('保存失败', error.message || '未获得真实回执'); });
        return;
      }
      if (target.id === 'saveBatchKeyword') {
        event.preventDefault(); event.stopImmediatePropagation();
        var batchId = String(activeBatchId);
        var keywordId = String(window.batchKeywordEditorId || '');
        var currentBatch = typeof getActiveBatch === 'function' ? getActiveBatch() : null;
        var currentKeyword = currentBatch && currentBatch.keywords ? currentBatch.keywords.find(function (item) { return item.id === keywordId; }) : null;
        if (!currentKeyword) {
          keywordId = String(document.getElementById('batchKeywordModal').dataset.keywordId || keywordId);
        }
        var nextKeyword = String(document.getElementById('batchKeywordInput').value || '').trim();
        var nextPurpose = String(document.getElementById('batchKeywordPurposeInput').value || '').trim();
        if (!nextKeyword) { showState('保存失败', '关键词不能为空。'); return; }
        postMutation(batchId, 'batch_state', {state: activeBatchSnapshot()})
          .then(function () { return postMutation(batchId, 'batch_keyword_edit', {keyword_id: keywordId, keyword: nextKeyword, purpose: nextPurpose}); })
          .then(reloadFromHub).catch(function (error) { showState('保存失败', error.message || '未获得真实回执'); });
        return;
      }
      if (target.id === 'saveBatchMother') {
        event.preventDefault(); event.stopImmediatePropagation();
        var motherBatchId = String(activeBatchId);
        var motherKeywordId = String(window.batchMotherEditorKwId || '');
        var checked = Array.prototype.slice.call(document.querySelectorAll('#batchMotherList input[type="checkbox"]:checked'))
          .map(function (checkbox) { return checkbox.dataset.motherId; });
        postMutation(motherBatchId, 'batch_state', {state: activeBatchSnapshot()})
          .then(function () { return postMutation(motherBatchId, 'batch_mother_edit', {keyword_id: motherKeywordId, mother_ids: checked}); })
          .then(reloadFromHub).catch(function (error) { showState('保存失败', error.message || '未获得真实回执'); });
        return;
      }
      var seg = target.closest ? target.closest('.seg') : null;
      if (seg) {
        event.preventDefault(); event.stopImmediatePropagation();
        var materialId = String(seg.closest('.segmented').dataset.materialId || '');
        var usage = String(seg.dataset.usage || '');
        postMutation(String(activeJobId), 'material_usage', {material_id: materialId, usage: usage})
          .then(reloadFromHub).catch(function (error) { showState('保存失败', error.message || '未获得真实回执'); });
        return;
      }
      var templateCard = target.closest ? target.closest('.template-card') : null;
      if (templateCard) {
        event.preventDefault(); event.stopImmediatePropagation();
        postMutation(String(activeJobId), 'template_select', {template_id: String(templateCard.dataset.templateId || '')})
          .then(reloadFromHub).catch(function (error) { showState('保存失败', error.message || '未获得真实回执'); });
        return;
      }
      var action = target.dataset && target.dataset.action;
      if (action === 'confirm-decision' || action === 'confirm-plan') {
        event.preventDefault(); event.stopImmediatePropagation();
        postMutation(String(activeJobId), action, {stage: 'package'})
          .then(reloadFromHub).catch(function (error) { showState('保存失败', error.message || '未获得真实回执'); });
        return;
      }
      if (action === 'simulate-finish') {
        event.preventDefault(); event.stopImmediatePropagation();
        runJob(String(activeJobId)).then(reloadFromHub).catch(function (error) { showState('运行失败', error.message || '未获得真实回执'); });
        return;
      }
      if (action === 'confirm-batch-queue') {
        event.preventDefault(); event.stopImmediatePropagation();
        persistBatchThen('batch_confirm_queue', {}, null);
        return;
      }
      if (action === 'simulate-running') {
        event.preventDefault(); event.stopImmediatePropagation();
        var runningId = String(activeJobId);
        if (!runningId || runningId.indexOf('empty-') === 0) { showState('无法运行', '当前没有可运行的真实任务。'); return; }
        runJob(runningId).then(reloadFromHub).catch(function (error) { showState('运行失败', error.message || '未获得真实回执'); });
        return;
      }
      if (action === 'simulate-queue-run') {
        event.preventDefault(); event.stopImmediatePropagation();
        var jobId = activeBatchId;
        if (!jobId || String(jobId).indexOf('empty-') === 0) { showState('无法运行', '当前没有可运行的真实任务。'); return; }
        persistBatchThen('batch_confirm_queue', {}, function () { return runJob(String(jobId)); });
        return;
      }
      if (target.classList.contains('stepper-btn')) {
        event.preventDefault(); event.stopImmediatePropagation();
        var stepper = target.closest('.stepper');
        var countKeywordId = String(stepper && stepper.dataset.keywordId || '');
        var delta = target.dataset.step === 'inc' ? 1 : -1;
        var snapshot = activeBatchSnapshot();
        var kw = snapshot && snapshot.keywords ? snapshot.keywords.find(function (item) { return String(item.id) === countKeywordId; }) : null;
        if (kw) kw.count = Math.max(0, Math.min(99, Number(kw.count || 0) + delta));
        postMutation(String(activeBatchId), 'batch_state', {state: snapshot})
          .then(reloadFromHub).catch(function (error) { showState('保存失败', error.message || '未获得真实回执'); });
      }
    }, true);
  }

  function release() {
    if (mask) mask.classList.add('hidden');
  }

  var params = new URLSearchParams(window.location.search);
  var wantedMode = params.get('mode');
  fetch('/api/v1/writing/jobs?limit=100', { headers: { 'Accept': 'application/json' } })
    .then(function (response) {
      if (!response.ok) throw new Error('Hub 返回 HTTP ' + response.status);
      return response.json();
    })
    .then(function (envelope) {
      var data = envelope && envelope.data ? envelope.data : {};
      var items = Array.isArray(data.items) ? data.items : [];
      var motherItems = items.filter(function (item) { return item.job_type === 'mother_forge'; });
      var batchItems = items.filter(function (item) { return item.job_type === 'batch_production'; });
      jobs.splice(0, jobs.length);
      motherItems.forEach(function (item) { jobs.push(mapJob(item)); });
      batchList = batchItems.map(mapBatch);
      batchList.forEach(function (batch) {
        if (typeof batchQueues === 'object' && batchQueues) {
          batchQueues[batch.id] = Array.isArray(batch.hubQueue) ? batch.hubQueue : [];
        }
      });
      if (!jobs.length) jobs.push(emptyJob('mother'));
      if (!batchList.length) batchList = [emptyBatch()];
      activeJobId = jobs[0] ? jobs[0].id : '';
      activeBatchId = batchList[0] ? batchList[0].id : '';
      if (wantedMode === 'batch' && typeof setMode === 'function') {
        setMode('batch');
        activeStage = batchList[0] && batchList[0].stage === 'batch-done' ? 'batch-done' : 'batch-config';
      } else if (typeof setMode === 'function') setMode('mother');
      interceptWrites();
      renderAll();
      release();
    })
    .catch(function (error) {
      showState('Hub 暂时无法连接', error && error.message ? error.message : '未获得真实任务回执');
    });
})();
