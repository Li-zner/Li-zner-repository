
(function() {
  // ---------- DOM 元素 ----------
  const loginContainer = document.getElementById('login-container');
  const appContainer = document.getElementById('appContainer');
  const loginBtn = document.getElementById('loginBtn');
  const loginError = document.getElementById('loginError');
  const loginUsername = document.getElementById('loginUsername');
  const loginPassword = document.getElementById('loginPassword');
  const logoutBtn = document.getElementById('logoutBtn');

  const messagesEl = document.getElementById('messages');
  const inputEl = document.getElementById('input');
  const sendBtn = document.getElementById('sendBtn');
  const stopBtn = document.getElementById('stopBtn');
  const historyList = document.getElementById('historyList');
  const newChatBtn = document.getElementById('newChatBtn');
  const mobileMenuBtn = document.getElementById('mobileMenuBtn');
  const mobileOverlay = document.getElementById('mobileOverlay');
  const mobileCloseBtn = document.getElementById('mobileCloseBtn');
  const mobileNewBtn = document.getElementById('mobileNewBtn');
  const mobileHistoryList = document.getElementById('mobileHistoryList');

  const API_URL = '/v2/chat/stream';
  const TASK_API_URL = '/v2/chat/tasks';
  const LOGIN_URL = '/api/login';
  const STORAGE_KEY = 'travel_chat_sessions';
  const TOKEN_KEY = 'travel_token';

  let sessions = [];
  let currentSessionId = null;
  let isStreaming = false;
  let currentAbortController = null;
  let updateThrottleTimer = null;
  let pendingUpdateText = null;
  let abortController = null;

  // ---------- 工具函数 ----------
  function cleanReasoning(text) {
    // 替换内部代码名为中文描述
    return text
      .replace(/query_weather/g, '查询天气')
      .replace(/query_hotel/g, '查找酒店')
      .replace(/query_route/g, '规划路线')
      .replace(/query_food/g, '推荐美食')
      .replace(/fetch_weather_async/g, '获取天气')
      .replace(/call_sub_agent/g, '调用助手')
      .replace(/tool_call_id/g, '工具调用')
      .replace(/"departure"\s*:\s*"[^"]*"/g, '')
      .replace(/"destination"\s*:\s*"[^"]*"/g, '')
      .replace(/"preference"\s*:\s*"[^"]*"/g, '')
      .replace(/"cuisine"\s*:\s*"[^"]*"/g, '')
      .replace(/"location"\s*:\s*"[^"]*"/g, '')
      .replace(/{[^}]*"success"[^}]*}/g, '')
      // 压缩多余空行
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }

  function updateStreamingText(msg) {
    // 流式更新专用：使用 textContent 避免 marked.parse 全量闪烁
    const lastDiv = messagesEl.querySelector('.message.assistant:last-child');
    if (!lastDiv) { renderMessages(); return; }
    let textDiv = lastDiv.querySelector('.answer-box');
    if (!textDiv) { renderMessages(); return; }
    
    // 流式期间直接用纯文本（避免 marked.parse 每次全量重渲染闪烁）
    textDiv.textContent = msg.text || '';
    
    // 思考面板更新
    if (msg.thinking) {
        let thinkBlock = lastDiv.querySelector('.think-block-top');
        if (!thinkBlock) {
            const details = document.createElement('details');
            details.className = 'think-block-top'; details.open = true;
            const summary = document.createElement('summary');
            summary.textContent = '思考过程';
            details.appendChild(summary);
            const contentDiv = document.createElement('div');
            details.appendChild(contentDiv);
            lastDiv.insertBefore(details, lastDiv.firstChild);
            thinkBlock = details;
        }
        const contentDiv = thinkBlock.querySelector('div');
        if (contentDiv) contentDiv.textContent = msg.thinking;
    }
    
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function updateMessageText(msg) {
    const lastAssistantDiv = messagesEl.querySelector('.message.assistant:last-child');
    if (!lastAssistantDiv) return;
    let textDiv = lastAssistantDiv.querySelector('.answer-box');
    if (!textDiv) { renderMessages(); return; }
    // 完成时用 marked.parse 渲染完整格式
    textDiv.innerHTML = marked.parse(msg.text || '');

    // 思考面板
    if (msg.thinking) {
        let thinkBlock = lastAssistantDiv.querySelector('.think-block-top');
        if (!thinkBlock) {
            const details = document.createElement('details');
            details.className = 'think-block-top'; details.open = true;
            const summary = document.createElement('summary');
            summary.textContent = '思考过程';
            details.appendChild(summary);
            const contentDiv = document.createElement('div');
            details.appendChild(contentDiv);
            lastAssistantDiv.insertBefore(details, lastAssistantDiv.firstChild);
            thinkBlock = details;
        }
        const contentDiv = thinkBlock.querySelector('div');
        if (contentDiv) contentDiv.textContent = msg.thinking;
}

  // ---------- 界面切换 ----------
  function showLogin() {
    loginContainer.style.display = 'flex';
    appContainer.classList.remove('show');
    loginError.style.display = 'none';
  }

  function showApp() {
    loginContainer.style.display = 'none';
    appContainer.classList.add('show');
  }

  // ---------- 存储 ----------
  function loadSessions() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      sessions = raw ? JSON.parse(raw) : [];
    } catch { sessions = []; }
    if (!sessions.length) createNewSession();
    const lastId = localStorage.getItem('travel_last_session');
    if (lastId && sessions.find(s => s.id === lastId)) currentSessionId = lastId;
    else currentSessionId = sessions[0].id;
    renderHistory();
    renderMobileHistory();
    renderMessages();
    // 自动恢复：检查是否有未完成的任务
    autoResumePendingTask();
  }

  async function autoResumePendingTask() {
    const session = getCurrentSession();
    const msgs = session.messages;
    for (let i = msgs.length - 1; i >= 0; i--) {
      const msg = msgs[i];
      if (msg.role === 'assistant' && msg.task_id && !msg.text.includes('⏹') && !msg.text.includes('❌')) {
        // 找到 pending 或 generating 状态的消息，恢复轮询
        const token = localStorage.getItem(TOKEN_KEY);
        try {
          const resp = await fetch(`${TASK_API_URL}/${msg.task_id}/result?wait=0`, {
            headers: { 'Authorization': 'Bearer ' + token }
          });
          if (!resp.ok) continue;
          const data = await resp.json();
          if (data.status === 'pending' || data.status === 'generating') {
            // 恢复轮询
            if (data.content) {
              msg.text = data.content;
              renderMessages();
            }
            startPolling(msg);
            return;
          } else if (data.status === 'completed' && data.content) {
            msg.text = data.content;
            saveSessions();
            renderMessages();
            return;
          } else if (data.status === 'cancelled' && data.content) {
            msg.text = data.content + '\n\n⏹（已中断）';
            saveSessions();
            renderMessages();
            return;
          }
        } catch (e) { /* ignore */ }
      }
    }
  }

  function saveSessions() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions));
    if (currentSessionId) localStorage.setItem('travel_last_session', currentSessionId);
  }

  function createNewSession() {
    const s = {
      id: 'sess_' + Date.now(),
      title: '新对话',
      messages: [],
      conversation_id: crypto.randomUUID ? crypto.randomUUID() : 'conv_' + Date.now() + '_' + Math.random().toString(36).substr(2, 6)
    };
    sessions.unshift(s);
    currentSessionId = s.id;
    saveSessions();
    renderHistory();
    renderMobileHistory();
    renderMessages();
    mobileOverlay.classList.remove('show');
    return s;
  }

  function getCurrentSession() {
    return sessions.find(s => s.id === currentSessionId) || sessions[0];
  }

  function switchSession(id) {
    if (id === currentSessionId) return;
    currentSessionId = id;
    saveSessions();
    renderHistory();
    renderMobileHistory();
    renderMessages();
    mobileOverlay.classList.remove('show');
  }

  async function deleteSession(id) {
    const session = sessions.find(s => s.id === id);
    if (sessions.length <= 1) { alert('至少保留一个会话'); return; }
    if (!confirm('删除这个对话？\nAgent 将遗忘该次对话并清除相关用户画像。')) return;

    // 调用后端 API 删除对话消息和用户画像
    if (session && session.conversation_id) {
      const token = localStorage.getItem(TOKEN_KEY);
      try {
        const resp = await fetch(`/api/conversations/${session.conversation_id}`, {
          method: 'DELETE',
          headers: { 'Authorization': 'Bearer ' + token }
        });
        if (resp.ok) {
          const data = await resp.json();
          console.log('🗑️ 对话已删除:', data);
        } else if (resp.status === 401) {
          logout();
          alert('登录已过期，请重新登录');
          return;
        } else {
          const err = await resp.json().catch(() => ({}));
          console.warn('删除对话API返回错误:', err.detail || resp.status);
        }
      } catch (e) {
        // 后端不可用时仅本地删除，不阻塞用户
        console.warn('删除对话API调用失败（仅执行本地删除）:', e);
      }
    }

    // 本地删除
    sessions = sessions.filter(s => s.id !== id);
    if (currentSessionId === id) currentSessionId = sessions[0].id;
    saveSessions();
    renderHistory();
    renderMobileHistory();
    renderMessages();
  }

  // ---------- 删除单条消息 ----------
  function deleteLastMessage() {
    const session = getCurrentSession();
    if (!session || session.messages.length < 2) return;
    // 删除最后一条 assistant 消息和它前面的 user 消息
    const lastMsg = session.messages[session.messages.length - 1];
    if (lastMsg.role === 'assistant') {
      session.messages.pop(); // 删 assistant
      if (session.messages.length > 0 && session.messages[session.messages.length - 1].role === 'user') {
        session.messages.pop(); // 删前面的 user
      }
    } else {
      session.messages.pop(); // 只删最后一条
    }
    saveSessions();
    renderMessages();
  }

  // ---------- 渲染 ----------
  function renameSession(id) {
    const session = sessions.find(s => s.id === id);
    if (!session) return;
    const newName = prompt('修改对话名称：', session.title || '');
    if (newName && newName.trim()) {
      session.title = newName.trim();
      saveSessions();
      renderHistory();
      renderMobileHistory();
    }
  }

  function renderHistory() {
    historyList.innerHTML = '';
    sessions.forEach(s => {
      const item = document.createElement('div');
      item.className = 'history-item' + (s.id === currentSessionId ? ' active' : '');
      const title = s.title || s.messages?.[0]?.text?.slice(0, 30) || '新对话';
      item.innerHTML = `<span class="title-text" title="${title.replace(/"/g, '&quot;')}">${title}</span>`
        + `<span class="rename-btn" title="重命名">✎</span>`
        + `<span class="delete-btn" title="删除此对话">✕</span>`;
      item.addEventListener('click', (e) => {
        if (e.target.classList.contains('delete-btn')) return;
        if (e.target.classList.contains('rename-btn')) return;
        switchSession(s.id);
      });
      const del = item.querySelector('.delete-btn');
      del.addEventListener('click', (e) => {
        e.stopPropagation();
        e.preventDefault();
        deleteSession(s.id);
      });
      const rename = item.querySelector('.rename-btn');
      rename.addEventListener('click', (e) => {
        e.stopPropagation();
        e.preventDefault();
        renameSession(s.id);
      });
      historyList.appendChild(item);
    });
  }

  function renderMobileHistory() {
    mobileHistoryList.innerHTML = '';
    if (!sessions.length) {
      mobileHistoryList.innerHTML = '<div class="panel-empty">暂无历史对话</div>';
      return;
    }
    sessions.forEach(s => {
      const item = document.createElement('div');
      item.className = 'panel-history-item' + (s.id === currentSessionId ? ' active' : '');
      const title = s.title || s.messages?.[0]?.text?.slice(0, 30) || '新对话';
      item.innerHTML = `<span title="${title.replace(/"/g, '&quot;')}">${title}</span>`
        + `<span class="rename-btn" title="重命名">✎</span>`
        + `<span class="del-btn" title="删除此对话">✕</span>`;
      item.addEventListener('click', (e) => {
        if (e.target.classList.contains('del-btn')) return;
        if (e.target.classList.contains('rename-btn')) return;
        switchSession(s.id);
      });
      const del = item.querySelector('.del-btn');
      del.addEventListener('click', (e) => {
        e.stopPropagation();
        e.preventDefault();
        deleteSession(s.id);
      });
      const rename = item.querySelector('.rename-btn');
      rename.addEventListener('click', (e) => {
        e.stopPropagation();
        e.preventDefault();
        renameSession(s.id);
      });
      mobileHistoryList.appendChild(item);
    });
  }

  // ----- 创建单条消息 DOM 元素（用于增量更新） -----
  function createMessageElement(msg) {
    const div = document.createElement('div');
    div.className = `message ${msg.role}`;

    // 思考面板（实时思考 + 已保存思考）
    const thinkText = msg.thinkClean || msg.think || msg.thinking || '';
    const hasThink = thinkText.trim().length > 0;
    if (hasThink) {
        const details = document.createElement('details');
        details.className = 'think-block-top';
        details.open = true;
        const summary = document.createElement('summary');
        summary.textContent = '思考过程';
        details.appendChild(summary);
        const contentDiv = document.createElement('div');
        const lines = thinkText.split('\n').filter(line => line.trim());
        lines.forEach(line => {
            const p = document.createElement('div');
            p.textContent = line;
            contentDiv.appendChild(p);
        });
        details.appendChild(contentDiv);
        div.insertBefore(details, div.firstChild);
    }

    // 消息文本（Markdown 渲染）— 放入生成框
    const textDiv = document.createElement('div');
    textDiv.className = 'answer-box';
    textDiv.innerHTML = msg.text ? marked.parse(msg.text) : '';
    div.appendChild(textDiv);

    // 底部栏：时间戳 + 操作按钮（flex 行，避免重叠）
    const bottomBar = document.createElement('div');
    bottomBar.style.cssText = 'display: flex; align-items: center; justify-content: space-between; margin-top: 6px; gap: 8px;';

    // 时间戳（左侧）
    const timeDiv = document.createElement('span');
    timeDiv.style.cssText = 'font-size: 11px; color: #999;';
    if (msg.timestamp) {
        const d = new Date(msg.timestamp);
        const month = d.getMonth() + 1;
        const day = d.getDate();
        const hours = String(d.getHours()).padStart(2, '0');
        const minutes = String(d.getMinutes()).padStart(2, '0');
        timeDiv.textContent = `${month}.${day} ${hours}:${minutes}`;
    } else {
        timeDiv.textContent = '';
    }
    bottomBar.appendChild(timeDiv);

    // 按钮组（右侧）
    const btnGroup = document.createElement('div');
    btnGroup.style.cssText = 'display: flex; align-items: center; gap: 4px;';

    // 复制按钮
    const copyBtn = document.createElement('button');
    copyBtn.className = 'copy-btn';
    copyBtn.textContent = '📋 复制';
    copyBtn.title = '复制内容';
    copyBtn.style.cssText = 'background: none; border: none; cursor: pointer; padding: 2px 6px; border-radius: 4px; font-size: 12px; color: var(--text-muted); transition: var(--transition);';
    copyBtn.addEventListener('mouseenter', () => { copyBtn.style.background = 'rgba(0,0,0,0.04)'; copyBtn.style.color = 'var(--primary)'; });
    copyBtn.addEventListener('mouseleave', () => { copyBtn.style.background = 'none'; copyBtn.style.color = 'var(--text-muted)'; });
    copyBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        const displayThink = msg.thinkClean || msg.think || '';
        const fullText = msg.text + (displayThink ? '\n\n--- 思考过程 ---\n' + displayThink : '');
        navigator.clipboard?.writeText(fullText).then(() => {
            copyBtn.textContent = '✅ 已复制';
            setTimeout(() => copyBtn.textContent = '📋 复制', 1500);
        }).catch(() => {
            const ta = document.createElement('textarea');
            ta.value = fullText;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            ta.remove();
            copyBtn.textContent = '✅ 已复制';
            setTimeout(() => copyBtn.textContent = '📋 复制', 1500);
        });
    });
    btnGroup.appendChild(copyBtn);

    // 中断恢复按钮：调用 resume 接口重新生成
    if (msg.role === 'assistant' && msg.task_id && msg.text && (msg.text.startsWith('⏹') || msg.text.includes('已中断'))) {
        const retryBtn = document.createElement('button');
        retryBtn.textContent = '🔄 继续生成';
        retryBtn.title = '重新生成回答';
        retryBtn.style.cssText = 'background: var(--primary); border: none; cursor: pointer; padding: 2px 10px; border-radius: 12px; font-size: 11px; color: #fff; transition: var(--transition);';
        retryBtn.addEventListener('mouseenter', () => { retryBtn.style.background = 'var(--primary-dark)'; });
        retryBtn.addEventListener('mouseleave', () => { retryBtn.style.background = 'var(--primary)'; });
        retryBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const token = localStorage.getItem(TOKEN_KEY);
            try {
                const resp = await fetch(`${TASK_API_URL}/${msg.task_id}/resume`, {
                    method: 'POST',
                    headers: { 'Authorization': 'Bearer ' + token }
                });
                if (!resp.ok) throw new Error('恢复失败');
                const data = await resp.json();
                // 更新当前消息的 task_id 并重置状态
                msg.task_id = data.task_id;
                msg.text = '🤔 继续生成中...';
                renderMessages();
                saveSessions();
                // 启动轮询
                startPolling(msg);
            } catch (e) {
                console.error('恢复失败:', e);
                alert('恢复失败，请稍后重试');
            }
        });
        btnGroup.appendChild(retryBtn);
    }

    // 删除按钮（仅 assistant）
    if (msg.role === 'assistant') {
        const delBtn = document.createElement('button');
        delBtn.textContent = '🗑️';
        delBtn.title = '删除上一条消息';
        delBtn.style.cssText = 'background: none; border: none; cursor: pointer; padding: 2px 6px; border-radius: 4px; font-size: 14px; color: var(--text-muted); transition: var(--transition);';
        delBtn.addEventListener('mouseenter', () => { delBtn.style.background = 'rgba(0,0,0,0.06)'; delBtn.style.color = '#ef4444'; });
        delBtn.addEventListener('mouseleave', () => { delBtn.style.background = 'none'; delBtn.style.color = 'var(--text-muted)'; });
        delBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            deleteLastMessage();
        });
        btnGroup.appendChild(delBtn);
    }

    bottomBar.appendChild(btnGroup);
    div.appendChild(bottomBar);

    return div;
  }

  function renderMessages() {
    const session = getCurrentSession();
    const messages = session.messages;

    // 空消息
    if (!messages.length) {
      messagesEl.innerHTML = `<div class="empty-state">👋 你好！我是你的旅行规划助手，有什么可以帮你的？</div>`;
      return;
    }

    // 增量更新：如果 DOM 中消息数量与列表一致，且最后一条是 assistant，只更新最后一条
    const existingChildren = messagesEl.children;
    if (existingChildren.length === messages.length && messages.length > 0) {
      const lastMsg = messages[messages.length - 1];
      if (lastMsg.role === 'assistant') {
        // 移除旧的最后一条 assistant 消息
        existingChildren[existingChildren.length - 1].remove();
        // 创建新的消息元素并追加
        const newDiv = createMessageElement(lastMsg);
        messagesEl.appendChild(newDiv);
        messagesEl.scrollTop = messagesEl.scrollHeight;
        return;
      }
    }

    // 全量重建（非 assistant 消息或数量不匹配时）
    messagesEl.innerHTML = '';
    messages.forEach(msg => {
      const div = createMessageElement(msg);
      messagesEl.appendChild(div);
    });
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // ---------- 登录 ----------
  async function handleLogin() {
    const username = loginUsername.value.trim();
    const password = loginPassword.value.trim();
    if (!username || !password) {
      loginError.textContent = '请输入用户名和密码';
      loginError.style.display = 'block';
      return;
    }
    try {
      const resp = await fetch(LOGIN_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password })
      });
      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail || '登录失败');
      }
      const data = await resp.json();
      const token = data.access_token;
      localStorage.setItem(TOKEN_KEY, token);
      loginError.style.display = 'none';
      showApp();
      loadSessions();
      inputEl.focus();
    } catch (e) {
      loginError.textContent = e.message || '用户名或密码错误';
      loginError.style.display = 'block';
    }
  }

  // ---------- 退出登录 ----------
  function logout() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(STORAGE_KEY);
    localStorage.removeItem('travel_last_session');
    sessions = [];
    currentSessionId = null;
    showLogin();
    messagesEl.innerHTML = '<div class="empty-state">👋 请登录后使用</div>';
    renderHistory();
    renderMobileHistory();
  }

function updateThoughtPanel(msg) {
    const lastAssistantDiv = messagesEl.querySelector('.message.assistant:last-child');
    if (!lastAssistantDiv) return;

    let thinkBlock = lastAssistantDiv.querySelector('.think-block-top');
    if (!thinkBlock) {
        // 如果不存在，创建新的 details
        const details = document.createElement('details');
        details.className = 'think-block-top';
        details.open = true;
        const summary = document.createElement('summary');
        summary.textContent = '思考过程';
        details.appendChild(summary);
        const contentDiv = document.createElement('div');
        details.appendChild(contentDiv);
        // 插入到消息顶部
        const textDiv = lastAssistantDiv.querySelector('.answer-box');
        if (textDiv) {
            lastAssistantDiv.insertBefore(details, textDiv);
        } else {
            lastAssistantDiv.prepend(details);
        }
        thinkBlock = details;
    }

    const contentDiv = thinkBlock.querySelector('div:not(summary)') || thinkBlock.querySelector('div:last-child');
    if (contentDiv) {
        contentDiv.innerHTML = '';
        const thinkText = msg.thinkClean || msg.think || '';
        const lines = thinkText.split('\n').filter(line => line.trim());
        lines.forEach(line => {
            const p = document.createElement('div');
            p.textContent = line;
            contentDiv.appendChild(p);
        });
    }
}

async function sendMessage() {
    if (isStreaming) return;
    const text = inputEl.value.trim();
    if (!text) return;

    const session = getCurrentSession();
    if (!session.conversation_id) {
        session.conversation_id = crypto.randomUUID ? crypto.randomUUID() : 'conv_' + Date.now() + '_' + Math.random().toString(36).substr(2, 6);
        saveSessions();
    }

    if (!session.messages.length) {
        session.title = text.slice(0, 30) + (text.length > 30 ? '…' : '');
        saveSessions();
        renderHistory();
        renderMobileHistory();
    }

    session.messages.push({ role: 'user', text, timestamp: new Date() });
    renderMessages();
    inputEl.value = '';
    sendBtn.disabled = true;
    isStreaming = true;
    stopBtn.style.display = 'inline-block';
    sendBtn.style.display = 'none';

    const placeholder = { role: 'assistant', text: '🤔 思考中，请稍候...', think: '', task_id: null, timestamp: new Date() };
    session.messages.push(placeholder);
    // 用 msg 引用该消息对象，方便后续操作
    const msg = placeholder;
    saveSessions();
    renderMessages();

    const token = localStorage.getItem(TOKEN_KEY);
    let currentTaskId = null;   // 当前 poll 的 task_id
    let pollingTimer = null;    // setInterval 句柄
    let isAborted = false;

    try {
        // 收集待发送的文件ID并清空
        let fileIds = [];
        if (session.pendingFileIds && session.pendingFileIds.length) {
            fileIds = [...session.pendingFileIds];
            session.pendingFileIds = [];
            saveSessions();
        }
        const userCity = localStorage.getItem('user_city');

        // ===== 直接使用流式接口（SSE）作为主要方式 =====
        // 使用 AbortController 实现暂停/继续
        const abortController = new AbortController();
        currentAbortController = abortController;
        const payload = {
            query: text,
            conversation_id: session.conversation_id || null,
            user_location: userCity || '',
            file_ids: fileIds.length ? fileIds : undefined,
        };

        try {
            const resp = await fetch(API_URL, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
                body: JSON.stringify(payload),
                signal: abortController.signal
            });
            if (resp.status === 401) { logout(); alert('登录已过期'); return; }
            if (!resp.ok) {
                msg.text = '❌ 请求失败（' + resp.status + '），请稍后重试。';
                renderMessages(); saveSessions(); finishStreaming(); return;
            }

            const reader = resp.body.getReader();
            const decoder = new TextDecoder('utf-8');
            let buffer = '';
            let fullText = '';
            let fullReasoning = '';
            let hasReasoning = false;

            // 停止按钮
            stopBtn.onclick = () => {
                isAborted = true;
                abortController.abort();
                const m = session.messages[session.messages.length - 1];
                if (m) {
                    m.text = m.text === '🤔 思考中，请稍候...' ? '⏹ 已中断' : (m.text || '') + '\n\n⏹（已中断）';
                    renderMessages(); saveSessions();
                }
                finishStreaming();
                // 保存继续所需上下文
                if (fullText) {
                    localStorage.setItem('resume_context_' + session.conversation_id, JSON.stringify({
                        text: fullText, reasoning: fullReasoning
                    }));
                }
            };

            while (true) {
                let result;
                try { result = await reader.read(); } catch (e) {
                    if (e.name === 'AbortError') break;
                    throw e;
                }
                const { done, value } = result;
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';
                for (const line of lines) {
                    const trimmed = line.trim();
                    if (!trimmed.startsWith('data: ')) continue;
                    const data = trimmed.slice(6);
                    if (data === '[DONE]') {
                        if (fullText) { msg.text = fullText; updateMessageText(msg); saveSessions(); }
                        break;
                    }
                    try {
                        const json = JSON.parse(data);

                        // 思考过程：实时展示
                        if (json.type === 'reasoning_chunk') {
                            hasReasoning = true;
                            fullReasoning += json.content || '';
                            msg.thinking = fullReasoning;
                            if (!msg.text || msg.text === '🤔 思考中，请稍候...') {
                                msg.text = '🤔 思考中...';
                                updateStreamingText(msg);
                            }
                        }
                        // 后端无思考内容时，前端模拟思考提示
                        if (json.type === 'thought') {
                            msg.text = '🤔 ' + (json.content || '思考中...');
                            updateStreamingText(msg);
                        }

                        // 思考结束
                        if (json.type === 'reasoning_done') {
                            // 思考结束
                        }

                        // 工具调用
                        if (json.type === 'tool_call') {
                            msg.text = '🔧 正在查询: ' + (json.name || '') + '...';
                            updateStreamingText(msg);
                        }

                        // 回答内容
                        if (json.type === 'answer_chunk') {
                            fullText = json.content || fullText;
                            msg.text = fullText;
                            if (!updateThrottleTimer) {
                                updateThrottleTimer = setTimeout(function() {
                                    updateThrottleTimer = null;
                                    updateStreamingText(msg);
                                    saveSessions();
                                }, 50);
                            }
                        }
                        if (json.type === 'answer_complete') {
                            fullText = json.content || fullText;
                            msg.text = fullText;
                            if (updateThrottleTimer) {
                                clearTimeout(updateThrottleTimer);
                                updateThrottleTimer = null;
                            }
                            // 流式结束：清除 streaming 标记，下次更新用完整 markdown
                            const lastDiv = messagesEl.querySelector('.message.assistant:last-child');
                            if (lastDiv) {
                                const textDiv = lastDiv.querySelector('.answer-box');
                                if (textDiv) delete textDiv.dataset.streaming;
                            }
                            updateMessageText(msg);
                            saveSessions();
                        }
                    } catch (e) { /* ignore parse errors */ }
                }
            }
        } catch (e) {
            if (e.name === 'AbortError') {
                // 用户主动暂停，不报错
            } else {
                console.error('流式请求失败:', e);
                msg.text = '❌ 请求失败，请稍后重试。';
                renderMessages(); saveSessions();
            }
        }
    } catch (error) {
        console.error('发送消息失败:', error);
        const msg = session.messages[session.messages.length - 1];
        if (msg) {
            msg.text = '❌ 请求失败，请稍后重试。';
            renderMessages();
        }
    } finally {
        finishStreaming();
    }

    function finishStreaming() {
        if (pollingTimer) { clearTimeout(pollingTimer); pollingTimer = null; }
        resetStreamingUI();
    }
}

function resetStreamingUI() {
    if (updateThrottleTimer) { clearTimeout(updateThrottleTimer); updateThrottleTimer = null; }
    sendBtn.style.display = 'inline-block';
    stopBtn.style.display = 'none';
    stopBtn.onclick = null;
    sendBtn.disabled = false;
    isStreaming = false;
    currentAbortController = null;
    if (inputEl) inputEl.focus();
    saveSessions();
    renderHistory();
    renderMobileHistory();
}

// 降级到旧流式接口（任务接口不可用时）
async function fallbackToStreaming(text, fileIds, userCity) {
    const session = getCurrentSession();
    const token = localStorage.getItem(TOKEN_KEY);
    const msg = session.messages[session.messages.length - 1];
    if (!msg) return;

    const conversationId = session.conversation_id || null;
    const payload = { query: text, user: 'web-user' };
    if (conversationId) payload.conversation_id = conversationId;
    if (fileIds && fileIds.length) payload.file_ids = fileIds;
    if (userCity) payload.user_location = userCity;

    try {
        const resp = await fetch(API_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
            body: JSON.stringify(payload)
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

        const reader = resp.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';
        let fullText = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';
            for (const line of lines) {
                const trimmed = line.trim();
                if (!trimmed.startsWith('data: ')) continue;
                const data = trimmed.slice(6);
                if (data === '[DONE]') {
                    if (fullText) { msg.text = fullText; updateMessageText(msg); saveSessions(); }
                    break;
                }
                try {
                    const json = JSON.parse(data);
                    if (json.type === 'answer_chunk' || json.type === 'answer_complete') {
                        fullText = json.content || fullText;
                        msg.text = fullText;
                        updateMessageText(msg);
                        saveSessions();
                    }
                } catch (e) { /* ignore */ }
            }
        }
    } catch (e) {
        console.error('流式降级失败:', e);
        msg.text = '❌ 请求失败，请稍后重试。';
        renderMessages();
    }
    resetStreamingUI();
}

// 轮询辅助函数（供继续生成按钮使用）
function startPolling(msg) {
    if (!msg.task_id) return;
    const token = localStorage.getItem(TOKEN_KEY);
    let pollCount = 0;
    const MAX_POLLS = 120;

    function doPoll() {
        pollCount++;
        fetch(`${TASK_API_URL}/${msg.task_id}/result?wait=0`, {
            headers: { 'Authorization': 'Bearer ' + token }
        }).then(resp => {
            if (!resp.ok) return;
            return resp.json();
        }).then(data => {
            if (!data) return;
            if (data.content && data.content !== msg.text) {
                msg.text = data.content;
                const session = getCurrentSession();
                const msgs = session.messages;
                const idx = msgs.indexOf(msg);
                if (idx >= 0) {
                    session.messages[idx] = msg;
                    saveSessions();
                    renderMessages();
                }
            }
            if (data.status === 'completed' || data.status === 'cancelled' || data.status === 'error') {
                if (data.status === 'completed' && data.content) {
                    msg.text = data.content;
                } else if (data.status === 'cancelled') {
                    msg.text = data.content || '⏹ 已中断（内容不完整）';
                } else if (data.status === 'error') {
                    msg.text = '❌ ' + (data.content || '生成失败');
                }
                saveSessions();
                renderMessages();
                return;
            }
            if (pollCount < MAX_POLLS) {
                setTimeout(doPoll, 500);
            }
        }).catch(() => {});
    }

    doPoll();
}

  // ---------- 手机号登录 ----------
  let phoneCodeTimer = null;
  let phoneCodeCountdown = 0;

  // 登录方式切换
  document.querySelectorAll('.login-tab').forEach(function(tab) {
    tab.addEventListener('click', function() {
      document.querySelectorAll('.login-tab').forEach(function(t) {
        t.style.background = '#f0f5f0';
        t.style.color = 'var(--text-secondary)';
        t.classList.remove('active');
      });
      this.style.background = 'var(--primary)';
      this.style.color = '#fff';
      this.classList.add('active');
      var tabName = this.getAttribute('data-tab');
      document.getElementById('loginTabPassword').style.display = tabName === 'password' ? 'block' : 'none';
      document.getElementById('loginTabPhone').style.display = tabName === 'phone' ? 'block' : 'none';
      document.getElementById('loginError').style.display = 'none';
      document.getElementById('phoneError').style.display = 'none';
    });
  });

  async function handlePhoneSendCode() {
    var phone = document.getElementById('phoneInput').value.trim();
    if (!phone || !/^\d{7,15}$/.test(phone)) {
      document.getElementById('phoneError').textContent = '请输入有效手机号';
      document.getElementById('phoneError').style.display = 'block';
      return;
    }
    if (phoneCodeCountdown > 0) return;
    try {
      var resp = await fetch('/api/phone/send-code', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ phone: phone })
      });
      var data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || '发送失败');
      document.getElementById('phoneError').textContent = '✅ 验证码已发送，请查收短信';
      document.getElementById('phoneError').style.display = 'block';
      document.getElementById('phoneError').style.color = 'var(--primary)';
      // 60秒倒计时
      phoneCodeCountdown = 60;
      var btn = document.getElementById('phoneSendCodeBtn');
      btn.disabled = true;
      clearInterval(phoneCodeTimer);
      phoneCodeTimer = setInterval(function() {
        phoneCodeCountdown--;
        btn.textContent = phoneCodeCountdown + 's';
        if (phoneCodeCountdown <= 0) {
          clearInterval(phoneCodeTimer);
          btn.textContent = '重新获取';
          btn.disabled = false;
        }
      }, 1000);
    } catch (e) {
      document.getElementById('phoneError').textContent = e.message || '发送失败';
      document.getElementById('phoneError').style.display = 'block';
      document.getElementById('phoneError').style.color = '#ef4444';
    }
  }

  async function handlePhoneLogin() {
    var phone = document.getElementById('phoneInput').value.trim();
    var code = document.getElementById('phoneCodeInput').value.trim();
    var password = document.getElementById('phonePasswordInput').value.trim();
    var agree = document.getElementById('agreeCheckbox').checked;
    var errorEl = document.getElementById('phoneError');
    if (!phone || !code) {
      errorEl.textContent = '请填写手机号和验证码';
      errorEl.style.display = 'block';
      errorEl.style.color = '#ef4444';
      return;
    }
    if (password && !agree) {
      errorEl.textContent = '请阅读并同意用户协议';
      errorEl.style.display = 'block';
      errorEl.style.color = '#ef4444';
      return;
    }
    try {
      // 如果有密码则注册，否则直接验证码登录
      var endpoint = password ? '/api/phone/register' : '/api/phone/login';
      var body = { phone: phone, code: code };
      if (password) { body.password = password; body.agree = agree; }
      var resp = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      var data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || '操作失败');
      localStorage.setItem(TOKEN_KEY, data.access_token);
      errorEl.style.display = 'none';
      showApp();
      loadSessions();
      inputEl.focus();
      // 清除倒计时
      clearInterval(phoneCodeTimer);
      document.getElementById('phoneSendCodeBtn').disabled = false;
      document.getElementById('phoneSendCodeBtn').textContent = '获取验证码';
    } catch (e) {
      errorEl.textContent = e.message || '操作失败';
      errorEl.style.display = 'block';
      errorEl.style.color = '#ef4444';
    }
  }

  // ---------- 手机弹窗 ----------
  function openMobileMenu() {
    renderMobileHistory();
    mobileOverlay.classList.add('show');
  }
  function closeMobileMenu() {
    mobileOverlay.classList.remove('show');
  }

  // ---------- 浏览器定位（与地图页共享定位数据）----------
  function initGeolocation() {
    // 如果已经定位过了就不再重复请求
    if (localStorage.getItem('user_city') && localStorage.getItem('user_city_ts')) {
      const ts = parseInt(localStorage.getItem('user_city_ts'));
      // 缓存1小时有效
      if (Date.now() - ts < 3600000) return;
    }
    // 如果地图页已经定位了，直接复用
    var mapLoc = localStorage.getItem('map_user_location');
    if (mapLoc) {
      try {
        var parsed = JSON.parse(mapLoc);
        if (parsed && parsed.city) {
          localStorage.setItem('user_city', parsed.city);
          localStorage.setItem('user_city_ts', Date.now().toString());
          return;
        }
      } catch(e) {}
    }
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition(async (pos) => {
      try {
        const lat = pos.coords.latitude;
        const lng = pos.coords.longitude;
        const token = localStorage.getItem(TOKEN_KEY);
        const resp = await fetch(`/api/map/regeo?lat=${lat}&lng=${lng}`, {
          headers: token ? { 'Authorization': 'Bearer ' + token } : {}
        });
        if (resp.ok) {
          const data = await resp.json();
          if (data.city) {
            localStorage.setItem('user_city', data.city);
            localStorage.setItem('user_city_ts', Date.now().toString());
          }
        }
      } catch (e) {
        console.warn('定位转城市失败:', e);
      }
    }, () => {
      // 用户拒绝定位，静默处理
    }, { timeout: 5000, enableHighAccuracy: false });
  }

  // ---------- 事件绑定 ----------
  loginBtn.addEventListener('click', handleLogin);
  loginPassword.addEventListener('keydown', (e) => { if (e.key === 'Enter') handleLogin(); });
  loginUsername.addEventListener('keydown', (e) => { if (e.key === 'Enter') handleLogin(); });
  document.getElementById('phoneSendCodeBtn').addEventListener('click', handlePhoneSendCode);
  document.getElementById('phoneLoginBtn').addEventListener('click', handlePhoneLogin);
  document.getElementById('phoneCodeInput').addEventListener('keydown', function(e) { if (e.key === 'Enter') handlePhoneLogin(); });
  document.getElementById('phonePasswordInput').addEventListener('keydown', function(e) { if (e.key === 'Enter') handlePhoneLogin(); });
  document.getElementById('phoneInput').addEventListener('keydown', function(e) { if (e.key === 'Enter') handlePhoneSendCode(); });
  logoutBtn.addEventListener('click', logout);
  sendBtn.addEventListener('click', sendMessage);
  inputEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') sendMessage(); });
  newChatBtn.addEventListener('click', createNewSession);
  mobileMenuBtn.addEventListener('click', openMobileMenu);
  mobileCloseBtn.addEventListener('click', closeMobileMenu);
  mobileNewBtn.addEventListener('click', createNewSession);
  mobileOverlay.addEventListener('click', (e) => {
    if (e.target === mobileOverlay) closeMobileMenu();
  });

  // ---------- 清空所有历史 ----------
  const clearAllBtn = document.getElementById('clearAllHistoryBtn');
  if (clearAllBtn) {
    clearAllBtn.addEventListener('click', async function() {
      if (sessions.length <= 1) { alert('至少保留一个会话'); return; }
      if (!confirm('确定要清空所有历史对话吗？此操作不可恢复。')) return;
      const token = localStorage.getItem(TOKEN_KEY);
      // 逐个删除后端对话（除了当前会话）
      const toDelete = sessions.filter(s => s.id !== currentSessionId);
      for (const s of toDelete) {
        if (s.conversation_id) {
          try {
            await fetch(`/api/conversations/${s.conversation_id}`, {
              method: 'DELETE',
              headers: { 'Authorization': 'Bearer ' + token }
            });
          } catch (e) {
            // 静默失败
          }
        }
      }
      // 保留当前会话
      const currentId = currentSessionId;
      sessions = sessions.filter(s => s.id === currentId);
      saveSessions();
      renderHistory();
      renderMobileHistory();
      renderMessages();
    });
  }

  // ---------- 页面关闭时保存 ----------
  window.addEventListener('beforeunload', function() {
    saveSessions();
  });

  // ---------- 页面初始化 ----------
  (function init() {
    const urlParams = new URLSearchParams(window.location.search);
    const tokenFromUrl = urlParams.get('token');
    const error = urlParams.get('error');

    if (error) {
      alert('登录失败: ' + error);
    }

    if (tokenFromUrl) {
      localStorage.setItem(TOKEN_KEY, tokenFromUrl);
      window.history.replaceState({}, document.title, window.location.pathname);
      showApp();
      loadSessions();
      inputEl.focus();
      return;
    }

    const savedToken = localStorage.getItem(TOKEN_KEY);
    if (savedToken) {
      showApp();
      loadSessions();
      inputEl.focus();

      // 检测从地图页带回的自动 prompt
      const autoPrompt = localStorage.getItem('travel_auto_prompt');
      if (autoPrompt) {
        localStorage.removeItem('travel_auto_prompt');
        setTimeout(() => {
          inputEl.value = autoPrompt;
          sendMessage();
        }, 500);
      }

      // 浏览器定位（获取用户城市）
      initGeolocation();
    } else {
      showLogin();
    }
  })();

})();
