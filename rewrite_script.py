import sys

html_path = 'static/index.html'
html = open(html_path, 'r', encoding='utf-8').read()
script_start = html.find('<script>')
script_end = html.find('</script>') + 9

new_script = """<script>
    const state = {
      currentFile: 'story_brief',
      currentPreview: 'story_brief',
      files: {},
    };

    const fileLabels = {
      story_brief: 'story_brief.json',
      outline_draft: 'outline_draft.json',
      outline: 'outline.json',
      characters: 'characters.json',
      storyline: 'storyline.json',
      conversation_memory: 'conversation_memory.json',
    };

    const editor = document.getElementById('editor');
    const storyBriefInput = document.getElementById('storyBriefInput');
    const currentFileEl = document.getElementById('currentFile');
    const outlineStateEl = document.getElementById('outlineState');
    const statusEl = document.getElementById('status');
    const dynamicPreviewMeta = document.getElementById('dynamicPreviewMeta');
    const dynamicPreviewBody = document.getElementById('dynamicPreviewBody');
    const streamLog = document.getElementById('streamLog');
    const chapterResultEl = document.querySelector('#chapterResult pre');
    const updateResultEl = document.querySelector('#updateResult');
    const memoryResultEl = document.querySelector('#memoryResult');
    const generateBtn = document.getElementById('generateBtn');

    function isOutlineConfirmed() {
      return (state.files.outline || {}).status === 'confirmed';
    }

    function pretty(data) {
      return JSON.stringify(data, null, 2);
    }

    function escapeHtml(text) {
      if (text === null || text === undefined) return '';
      return String(text)
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;');
    }

    function setStatus(text, color = '') {
      statusEl.textContent = text;
      statusEl.style.color = color || '';
    }

    async function api(path, options = {}) {
      const res = await fetch(path, {
        headers: {
          'Content-Type': 'application/json',
          ...(options.headers || {}),
        },
        ...options,
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      return data;
    }

    function parseSseFrame(frame) {
      const lines = frame.split(/\\r?\\n/);
      let event = 'message';
      const dataLines = [];
      for (const line of lines) {
        if (line.startsWith('event:')) {
          event = line.slice(6).trim();
        } else if (line.startsWith('data:')) {
          dataLines.push(line.slice(5).trimStart());
        }
      }
      return { event, data: dataLines.join('\\n') };
    }

    async function streamApi(path, payload, handlers = {}) {
      const res = await fetch(`${path}?stream=1`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'text/event-stream',
        },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';
      let finalPayload = null;
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buffer.indexOf('\\n\\n')) >= 0) {
          const frame = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          if (!frame.trim()) continue;
          const event = parseSseFrame(frame);
          if (event.event === 'chunk') {
            handlers.onChunk?.(event.data);
          } else if (event.event === 'meta') {
            handlers.onMeta?.(event.data);
          } else if (event.event === 'final') {
            finalPayload = JSON.parse(event.data);
            handlers.onFinal?.(finalPayload);
          } else if (event.event === 'error') {
            const err = JSON.parse(event.data);
            throw new Error(err.error || 'stream error');
          }
        }
      }
      return finalPayload;
    }

    function addBubble(role, title, text = '') {
      const node = document.createElement('div');
      node.className = `bubble ${role}`;
      node.innerHTML = `
        <div class="label">${escapeHtml(title)}</div>
        <pre>${escapeHtml(text)}</pre>
      `;
      streamLog.appendChild(node);
      streamLog.scrollTop = streamLog.scrollHeight;
      return node.querySelector('pre');
    }

    function parseSseChunk(text) {
      try {
        const payload = JSON.parse(text);
        return payload.text || '';
      } catch (e) {
        return text;
      }
    }

    // Dynamic Preview Rendering
    function renderDynamicPreview() {
      const type = state.currentPreview;
      let meta = '';
      let body = '';

      if (type === 'story_brief') {
        const data = state.files.story_brief || {};
        meta = '故事简介文本';
        body = `<div style="line-height:1.6; white-space:pre-wrap;">${escapeHtml(data.description || '暂无简介')}</div>`;
      } 
      else if (type === 'outline') {
        const outline = state.files.outline || state.files.outline_draft || {};
        meta = outline.status === 'confirmed'
          ? `已确认 · ${outline.title || '未命名'}`
          : outline.status === 'draft'
            ? `草案 · ${outline.title || '未命名'}`
            : '等待生成大纲';
        const plans = Array.isArray(outline.chapter_plan) ? outline.chapter_plan : [];
        body = `
          <div class="small">题材：${escapeHtml(outline.genre || '未设置')}</div>
          <div class="small">主题：${escapeHtml(outline.theme || '未设置')}</div>
          <div style="margin-top:8px; line-height:1.6;">${escapeHtml(outline.logline || '暂无简介')}</div>
          <div class="small" style="margin-top:10px;">章节计划 ${plans.length} 章</div>
          <ul style="margin-top:8px;">
            ${plans.slice(0, 10).map(item => `<li style="margin-bottom:6px;"><strong>第${item.chapter_no || '?'}章：</strong>${escapeHtml(item.goal || '')}</li>`).join('')}
            ${plans.length > 10 ? '<li>...</li>' : ''}
          </ul>
        `;
      } 
      else if (type === 'characters') {
        const chars = state.files.characters?.characters || [];
        meta = `角色列表 · 共 ${chars.length} 人`;
        body = '<div style="display:flex; flex-direction:column; gap:12px;">' + chars.map(c => `
          <div style="background:rgba(255,255,255,0.03); border:1px solid rgba(255,255,255,0.05); padding:10px; border-radius:8px;">
            <div style="font-weight:bold; color:var(--accent); margin-bottom:4px;">${escapeHtml(c.name)} <span class="muted small" style="font-weight:normal;">(${escapeHtml(c.role)})</span></div>
            <div class="small" style="margin-bottom:4px;"><span style="color:var(--text);">外貌：</span>${escapeHtml(c.appearance || '无')}</div>
            <div class="small" style="margin-bottom:4px;"><span style="color:var(--text);">动机：</span>${escapeHtml(c.motivation || '无')}</div>
            <div class="small"><span style="color:var(--text);">当前状态：</span>${escapeHtml(c.current_state?.location || '未知')} · ${escapeHtml(c.current_state?.mood || '平静')}</div>
          </div>
        `).join('') + '</div>';
        if (chars.length === 0) body = '<div class="muted">暂无角色</div>';
      }
      else if (type === 'storyline') {
        const storyline = state.files.storyline || {};
        const chapters = Array.isArray(storyline.chapter_summaries) ? storyline.chapter_summaries : [];
        meta = `已记录 ${chapters.length} 章`;
        body = `
          <div style="line-height:1.6; margin-bottom:12px;">${escapeHtml(storyline.overall_summary || '暂无故事线摘要')}</div>
          <div class="small" style="color:var(--danger); margin-bottom:4px;">未解决线索</div>
          <ul style="margin-top:0;">
            ${(storyline.open_threads || []).map(item => `<li>${escapeHtml(item)}</li>`).join('') || '<li class="muted">无</li>'}
          </ul>
        `;
      }
      else if (type === 'memory') {
        const memory = state.files.conversation_memory || {};
        const summary = memory.dialogue_summary || {};
        meta = '记忆与对话约束';
        body = `
          <div class="small" style="color:var(--accent-2); margin-bottom:4px;">确认过的约束</div>
          <ul style="margin-top:0;">
            ${(summary.confirmed_constraints || []).map(item => `<li>${escapeHtml(item)}</li>`).join('') || '<li class="muted">无</li>'}
          </ul>
          <div class="small" style="color:var(--accent); margin-bottom:4px;">用户偏好</div>
          <ul style="margin-top:0;">
            ${(summary.user_preferences || []).map(item => `<li>${escapeHtml(item)}</li>`).join('') || '<li class="muted">无</li>'}
          </ul>
          <div style="margin-top:8px; line-height:1.5;">${escapeHtml(summary.freeform_summary || '')}</div>
        `;
      }

      dynamicPreviewMeta.textContent = meta;
      dynamicPreviewBody.innerHTML = body;
      
      document.querySelectorAll('.preview-tab').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.preview === type);
      });
    }

    function renderUpdateResult(updates) {
      if (!updates || Object.keys(updates).length === 0) {
        updateResultEl.innerHTML = '<div class="muted">无更新内容</div>';
        return;
      }
      let html = '';
      if (updates.chapter_summary && updates.chapter_summary.length > 0) {
        html += '<div class="small" style="margin-bottom:4px;color:var(--accent);">章节摘要</div><ul>' + updates.chapter_summary.map(s => `<li>${escapeHtml(s)}</li>`).join('') + '</ul>';
      }
      if (updates.new_events && updates.new_events.length > 0) {
        html += '<div class="small" style="margin-top:10px;margin-bottom:4px;color:var(--accent-2);">新事件</div><ul>' + updates.new_events.map(s => `<li>${escapeHtml(s)}</li>`).join('') + '</ul>';
      }
      if (updates.open_threads && updates.open_threads.length > 0) {
        html += '<div class="small" style="margin-top:10px;margin-bottom:4px;color:var(--danger);">新增悬念 / 待解决</div><ul>' + updates.open_threads.map(s => `<li>${escapeHtml(s)}</li>`).join('') + '</ul>';
      }
      if (updates.resolved_threads && updates.resolved_threads.length > 0) {
        html += '<div class="small" style="margin-top:10px;margin-bottom:4px;color:var(--muted);">已解决线索</div><ul>' + updates.resolved_threads.map(s => `<li><del>${escapeHtml(s)}</del></li>`).join('') + '</ul>';
      }
      if (updates.character_updates && updates.character_updates.length > 0) {
        html += '<div class="small" style="margin-top:10px;margin-bottom:4px;color:var(--text);">角色状态更新</div><ul>' + updates.character_updates.map(c => {
          let changes = c.current_state_changes ? Object.entries(c.current_state_changes).map(([k, v]) => `${k}: ${v}`).join(', ') : '';
          return `<li><strong>${escapeHtml(c.id)}</strong> - ${escapeHtml(changes || '无')} <span class="muted">(${escapeHtml(c.notes || '')})</span></li>`;
        }).join('') + '</ul>';
      }
      updateResultEl.innerHTML = html || '<div class="muted">无具体更新详情</div>';
    }

    function renderMemory() {
      const memory = state.files.conversation_memory || {};
      const turns = memory.recent_turns || [];
      let html = '';
      if (turns.length > 0) {
        html += '<div style="display:flex;flex-direction:column;gap:8px;">';
        [...turns].reverse().slice(0, 5).forEach(turn => {
          html += `
            <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.05);padding:8px;border-radius:8px;">
              <div class="small" style="margin-bottom:4px;"><span style="color:var(--accent-2);">User:</span> ${escapeHtml(turn.user)}</div>
              <div class="small" style="color:var(--text);"><span style="color:var(--accent);">Agent:</span> ${escapeHtml(turn.assistant).substring(0, 150)}${turn.assistant.length > 150 ? '...' : ''}</div>
            </div>
          `;
        });
        html += '</div>';
      } else {
        html += '<div class="muted">暂无最近对话记录</div>';
      }
      memoryResultEl.innerHTML = html;
    }

    function refreshResultPanels(payload) {
      if (payload.chapter_text !== undefined) chapterResultEl.innerHTML = `<pre>${escapeHtml(payload.chapter_text || '')}</pre>`;
      if (payload.updates !== undefined) renderUpdateResult(payload.updates);
      if (payload.memory !== undefined) {
        state.files.conversation_memory = payload.memory;
        renderMemory();
      }
    }

    async function loadState() {
      setStatus('正在加载项目文件...');
      const data = await api('/api/state', { method: 'GET' });
      state.files = data;
      if (!storyBriefInput.value.trim()) {
        storyBriefInput.value = (data.story_brief && data.story_brief.description) || '';
      }
      renderCurrentFile();
      renderMemory();
      renderDynamicPreview();
      syncFlowState();
      setStatus('项目文件已加载。', '#94f7d6');
    }

    function renderCurrentFile() {
      const data = state.files[state.currentFile] || {};
      currentFileEl.textContent = fileLabels[state.currentFile];
      editor.value = pretty(data);
      document.querySelectorAll('.tab:not(.preview-tab)').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.file === state.currentFile);
      });
      
      let pTarget = state.currentFile;
      if (pTarget === 'outline_draft') pTarget = 'outline';
      if (pTarget === 'conversation_memory') pTarget = 'memory';
      if (document.querySelector(`.preview-tab[data-preview="${pTarget}"]`)) {
        state.currentPreview = pTarget;
        renderDynamicPreview();
      }
    }

    function syncFlowState() {
      const confirmed = isOutlineConfirmed();
      outlineStateEl.textContent = confirmed
        ? `大纲状态：已确认（${(state.files.outline || {}).title || '未命名'}）`
        : '大纲状态：未确认，先生成大纲草案并确认后才能生成章节';
      generateBtn.disabled = !confirmed;
      document.getElementById('confirmOutlineBtn').disabled = !state.files.outline_draft;
    }

    function applyOutlineDraft(draft) {
      state.files.outline_draft = draft;
      state.currentFile = 'outline_draft';
      renderCurrentFile();
      syncFlowState();
    }

    function applyConfirmedOutline(outline) {
      state.files.outline = outline;
      renderDynamicPreview();
      syncFlowState();
    }

    document.querySelectorAll('.tab:not(.preview-tab)').forEach(btn => {
      btn.addEventListener('click', () => {
        state.currentFile = btn.dataset.file;
        renderCurrentFile();
      });
    });

    document.querySelectorAll('.preview-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        state.currentPreview = btn.dataset.preview;
        renderDynamicPreview();
      });
    });

    document.getElementById('saveBtn').addEventListener('click', async () => {
      try {
        setStatus('正在保存...');
        const payload = JSON.parse(editor.value);
        const data = await api('/api/save', {
          method: 'POST',
          body: JSON.stringify({ name: state.currentFile, json_text: editor.value }),
        });
        state.files[state.currentFile] = payload;
        if (state.currentFile === 'conversation_memory') renderMemory();
        renderDynamicPreview();
        setStatus(`已保存 ${fileLabels[state.currentFile]}。`, '#94f7d6');
      } catch (err) {
        setStatus(`保存失败：${err.message}`, '#fca5a5');
      }
    });

    document.getElementById('generateOutlineBtn').addEventListener('click', async () => {
      try {
        const storyBrief = storyBriefInput.value.trim();
        if (!storyBrief) return setStatus('请输入故事描述。', '#fca5a5');
        setStatus('正在生成大纲...');
        const userBubble = addBubble('user', '用户', storyBrief);
        const assistantBubble = addBubble('assistant', 'DeepSeek', '');
        let markdownBuffer = '';
        
        const payload = await streamApi('/api/generate-outline', { story_brief: storyBrief }, {
          onChunk: (text) => {
            markdownBuffer += parseSseChunk(text);
            if (markdownBuffer.indexOf('```json') !== -1) {
                assistantBubble.innerHTML = `<div class="label">DeepSeek</div><div style="line-height:1.5;">${escapeHtml(markdownBuffer.split('```json')[0])}</div><div class="muted small" style="margin-top:8px;">[正在生成结构化 JSON...]</div>`;
            } else {
                assistantBubble.innerHTML = `<div class="label">DeepSeek</div><div style="line-height:1.5; white-space: pre-wrap;">${escapeHtml(markdownBuffer)}</div>`;
            }
          },
          onFinal: (finalPayload) => {
            state.files.story_brief = { description: storyBrief, created_at: finalPayload.record?.generated_at || '', updated_at: finalPayload.record?.generated_at || '' };
            applyOutlineDraft(finalPayload.outline_draft || {});
            state.files.conversation_memory = finalPayload.memory || state.files.conversation_memory;
            renderMemory();
            assistantBubble.innerHTML = `<div class="label">DeepSeek</div><div style="line-height:1.5;">大纲草案已生成。请在上方检查并确认。</div>`;
          },
        });
        if (payload?.outline_draft) state.files.outline_draft = payload.outline_draft;
        setStatus('大纲草案已生成，请检查并确认。', '#94f7d6');
      } catch (err) {
        setStatus(`生成大纲失败：${err.message}`, '#fca5a5');
      }
    });

    document.getElementById('confirmOutlineBtn').addEventListener('click', async () => {
      try {
        setStatus('正在确认大纲...');
        let outlineJson = editor.value;
        if (state.currentFile !== 'outline_draft') outlineJson = JSON.stringify(state.files.outline_draft || {}, null, 2);
        const payload = await api('/api/confirm-outline', {
          method: 'POST',
          body: JSON.stringify({ outline_json: outlineJson, story_brief: storyBriefInput.value.trim() }),
        });
        applyConfirmedOutline(payload.outline);
        state.currentFile = 'outline';
        await loadState();
        renderCurrentFile();
        syncFlowState();
        setStatus('大纲已确认，可以开始生成章节。', '#94f7d6');
      } catch (err) {
        setStatus(`确认大纲失败：${err.message}`, '#fca5a5');
      }
    });

    document.getElementById('reloadBtn').addEventListener('click', async () => {
      try { await loadState(); } catch (err) { setStatus(`加载失败：${err.message}`, '#fca5a5'); }
    });

    document.getElementById('resetBtn').addEventListener('click', async () => {
      if (!confirm('确认重置为默认 JSON 文件？')) return;
      try {
        setStatus('正在重置...');
        await api('/api/reset', { method: 'POST', body: '{}' });
        await loadState();
        setStatus('已重置为默认文件。', '#94f7d6');
      } catch (err) {
        setStatus(`重置失败：${err.message}`, '#fca5a5');
      }
    });

    document.getElementById('generateBtn').addEventListener('click', async () => {
      try {
        if (!isOutlineConfirmed()) return setStatus('请先确认大纲后再生成章节。', '#fca5a5');
        setStatus('正在生成章节...');
        const chapterNo = Number(document.getElementById('chapterNo').value || 1);
        const assistantBubble = addBubble('assistant', `第 ${chapterNo} 章`, '');
        let contentBuffer = '';
        const payload = await streamApi('/api/generate-chapter', {
          chapter_no: chapterNo,
          chapter_title: document.getElementById('chapterTitle').value,
          tone: document.getElementById('chapterTone').value,
          length_target: Number(document.getElementById('lengthTarget').value || 1800),
          instruction: document.getElementById('chapterInstruction').value,
        }, {
          onChunk: (text) => {
            contentBuffer += parseSseChunk(text);
            assistantBubble.innerHTML = `<div class="label">第 ${chapterNo} 章</div><div style="line-height:1.6; white-space: pre-wrap; word-break: break-all;">${escapeHtml(contentBuffer)}</div>`;
          },
          onFinal: (finalPayload) => {
            refreshResultPanels(finalPayload);
            renderDynamicPreview();
          },
        });
        refreshResultPanels(payload || {});
        await loadState();
        setStatus(`第 ${payload.chapter_no} 章已生成并写回 JSON。`, '#94f7d6');
      } catch (err) {
        setStatus(`生成失败：${err.message}`, '#fca5a5');
      }
    });

    document.getElementById('chatBtn').addEventListener('click', async () => {
      try {
        setStatus('正在发送对话...');
        const message = document.getElementById('chatInput').value.trim();
        if (!message) return setStatus('请输入对话内容。', '#fca5a5');
        const assistantBubble = addBubble('assistant', 'DeepSeek', '');
        let contentBuffer = '';
        const payload = await streamApi('/api/chat', { message }, {
          onChunk: (text) => {
            contentBuffer += parseSseChunk(text);
            assistantBubble.innerHTML = `<div class="label">DeepSeek</div><div style="line-height:1.6; white-space: pre-wrap;">${escapeHtml(contentBuffer)}</div>`;
          },
          onFinal: (finalPayload) => {
            chapterResultEl.innerHTML = `<pre>${escapeHtml(finalPayload.reply || '')}</pre>`;
            state.files.conversation_memory = finalPayload.memory || state.files.conversation_memory;
            renderMemory();
            renderDynamicPreview();
          },
        });
        if (payload) chapterResultEl.innerHTML = `<pre>${escapeHtml(payload.reply || '')}</pre>`;
        await loadState();
        setStatus('对话已完成并写入最近记忆。', '#94f7d6');
      } catch (err) {
        setStatus(`对话失败：${err.message}`, '#fca5a5');
      }
    });

    loadState().catch(err => setStatus(`初始化失败：${err.message}`, '#fca5a5'));
  </script>"""

if __name__ == '__main__':
    html = html[:script_start] + new_script + html[script_end:]
    open(html_path, 'w', encoding='utf-8').write(html)
    print('Script replaced successfully.')
