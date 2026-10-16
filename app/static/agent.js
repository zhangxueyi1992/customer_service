/* 人工客服工作台：排队会话接入 / 多会话实时聊天 / 快捷回复 / 标签备注 */
(function () {
  "use strict";

  // ---------- 状态 ----------
  const state = {
    agentName: localStorage.getItem("cs_agent_name") || "客服1",
    conversations: [],
    currentConvId: null,
    currentConv: null,
    ws: null,
    reconnectTimer: null,
    pollTimer: null,
    pending: new Map(), // client_id -> {msg, el}
    quickReplies: [],
    userTyping: false,
    metaDirty: { tag: false, note: false },
  };

  // ---------- DOM ----------
  const $ = (sel) => document.querySelector(sel);
  const agentNameText = $("#agentNameText");
  const convList = $("#convList");
  const convCount = $("#convCount");
  const chatTitle = $("#chatTitle");
  const chatAvatar = $("#chatAvatar");
  const chatStatus = $("#chatStatus");
  const chatTag = $("#chatTag");
  const returnQueueBtn = $("#returnQueueBtn");
  const messageList = $("#messageList");
  const msgInput = $("#msgInput");
  const clearInputBtn = $("#clearInputBtn");
  const sendBtn = $("#sendBtn");
  const userTypingHint = $("#userTypingHint");
  const metaTag = $("#metaTag");
  const metaNote = $("#metaNote");
  const qrList = $("#qrList");
  const modalOverlay = $("#modalOverlay");
  const modalBox = $("#modalBox");
  const toastEl = $("#toast");

  const SENDERS = {
    user: { name: "用户", emoji: "🧑", cls: "avatar-user" },
    bot: { name: "智能客服", emoji: "🤖", cls: "avatar-bot" },
    human: { name: "人工客服", emoji: "🧑‍💼", cls: "avatar-human" },
  };

  // ---------- 工具 ----------
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function mdToHtml(text) {
    let html = escapeHtml(text);
    html = html
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
    const lines = html.split("\n");
    html = lines
      .map((line) => {
        const trimmed = line.trim();
        if (trimmed.startsWith("## ")) return `<p class="md-h2">${line.slice(3)}</p>`;
        if (trimmed.startsWith("# ")) return `<p class="md-h1">${line.slice(2)}</p>`;
        if (trimmed.startsWith("- ")) return `<li>${line.slice(2)}</li>`;
        return `<p>${line || "<br>"}</p>`;
      })
      .join("");
    return html;
  }

  function fmtTime(ts) {
    if (!ts) return "";
    const d = new Date(String(ts).replace(" ", "T"));
    const now = new Date();
    const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    if (d.toDateString() === now.toDateString()) return hm;
    return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${hm}`;
  }

  function scrollToBottom(smooth) {
    messageList.scrollTo({ top: messageList.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  }

  function toast(text) {
    toastEl.textContent = text;
    toastEl.classList.remove("hidden");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => toastEl.classList.add("hidden"), 2200);
  }

  function showModal(html, large) {
    modalBox.innerHTML = html;
    modalBox.classList.toggle("modal-lg", !!large);
    modalOverlay.classList.remove("hidden");
    const firstInput = modalBox.querySelector("input, textarea");
    if (firstInput) firstInput.focus();
  }

  function closeModal() {
    modalOverlay.classList.add("hidden");
    modalBox.innerHTML = "";
    modalBox.classList.remove("modal-lg");
  }

  function confirmDialog(title, text) {
    return new Promise((resolve) => {
      showModal(`
        <h3>${escapeHtml(title)}</h3>
        <p style="font-size:14px;color:#555;line-height:1.7;">${escapeHtml(text)}</p>
        <div class="modal-actions">
          <button class="modal-btn plain" id="dlgCancel">取消</button>
          <button class="modal-btn primary" id="dlgOk">确定</button>
        </div>
      `);
      modalBox.querySelector("#dlgCancel").onclick = () => { closeModal(); resolve(false); };
      modalBox.querySelector("#dlgOk").onclick = () => { closeModal(); resolve(true); };
    });
  }

  function setAgentName(name) {
    state.agentName = name;
    localStorage.setItem("cs_agent_name", name);
    agentNameText.textContent = name;
  }

  // ---------- 消息渲染 ----------
  function renderMessage(msg) {
    const el = document.createElement("div");
    el.className = "message";
    el.dataset.id = msg.id;

    if (msg.sender === "system") {
      el.classList.add("system");
      const bubble = document.createElement("div");
      bubble.className = "msg-bubble";
      bubble.textContent = msg.content.text || "";
      el.appendChild(bubble);
      return el;
    }

    const isUser = msg.sender === "user";
    el.classList.add(isUser ? "user" : "other");

    const sender = SENDERS[msg.sender] || SENDERS.bot;
    const avatar = document.createElement("div");
    avatar.className = `avatar ${sender.cls}`;
    avatar.textContent = sender.emoji;
    el.appendChild(avatar);

    const main = document.createElement("div");
    main.className = "msg-main";

    const meta = document.createElement("div");
    meta.className = "msg-meta";
    const nameSpan = document.createElement("span");
    nameSpan.className = "msg-name";
    const displayName =
      msg.sender === "human"
        ? (msg.content.agent_name || "人工客服")
        : msg.sender === "bot"
          ? "智能客服"
          : "";
    nameSpan.textContent = displayName;
    const timeSpan = document.createElement("span");
    timeSpan.className = "msg-time";
    timeSpan.textContent = fmtTime(msg.created_at);
    meta.appendChild(nameSpan);
    meta.appendChild(timeSpan);
    if (isUser && msg.status === "read") {
      const st = document.createElement("span");
      st.className = "msg-status";
      st.textContent = "✓✓";
      st.title = "已读";
      meta.appendChild(st);
    }
    main.appendChild(meta);

    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    bubble.appendChild(renderBubbleContent(msg));
    main.appendChild(bubble);
    el.appendChild(main);
    return el;
  }

  function renderBubbleContent(msg) {
    const c = msg.content || {};
    const wrap = document.createElement("div");

    if (msg.type === "image") {
      const img = document.createElement("img");
      img.className = "msg-img";
      img.src = c.url;
      img.alt = c.alt || "图片";
      img.onerror = () => { img.style.opacity = ".4"; };
      img.onclick = () => openLightbox(c.url);
      wrap.appendChild(img);
      return wrap;
    }

    if (msg.type === "card") {
      const card = document.createElement("a");
      card.className = "link-card";
      card.href = c.url || "#";
      card.target = "_blank";
      card.rel = "noopener";
      if (c.image) {
        const img = document.createElement("img");
        img.className = "link-card-img";
        img.src = c.image;
        img.alt = "";
        card.appendChild(img);
      }
      const body = document.createElement("div");
      body.innerHTML =
        `<div class="link-card-title">${escapeHtml(c.title || "链接")}</div>` +
        (c.description ? `<div class="link-card-desc">${escapeHtml(c.description)}</div>` : "") +
        `<div class="link-card-domain">${escapeHtml(domainOf(c.url))}</div>`;
      card.appendChild(body);
      wrap.appendChild(card);
      return wrap;
    }

    const div = document.createElement("div");
    div.innerHTML = mdToHtml(c.text || "");
    wrap.appendChild(div);
    return wrap;
  }

  function domainOf(url) {
    try {
      return new URL(url).hostname.replace(/^www\./, "");
    } catch {
      return url || "";
    }
  }

  function openLightbox(src) {
    const box = document.createElement("div");
    box.className = "lightbox";
    const img = document.createElement("img");
    img.src = src;
    box.appendChild(img);
    box.onclick = () => box.remove();
    document.body.appendChild(box);
  }

  function appendMessage(msg, opts = {}) {
    const el = renderMessage(msg);
    messageList.appendChild(el);
    scrollToBottom(opts.smooth);
    return el;
  }

  // ---------- 会话列表 ----------
  async function loadConversations(selectFirst) {
    try {
      const res = await fetch(`/api/agent/conversations?agent_name=${encodeURIComponent(state.agentName)}`);
      state.conversations = await res.json();
      renderConvList();
      if (selectFirst && !state.currentConvId && state.conversations.length) {
        selectConversation(state.conversations[0]);
      }
      if (state.currentConvId) {
        const cur = state.conversations.find((c) => c.id === state.currentConvId);
        if (!cur) {
          // 会话已被接管/结束/切回机器人，清空当前选择
          clearCurrent();
        } else {
          state.currentConv = cur;
          renderChatHeader();
          fillMeta();
        }
      }
    } catch {
      /* 忽略，下次轮询重试 */
    }
  }

  function renderConvList() {
    convList.innerHTML = "";
    convCount.textContent = state.conversations.length;
    if (!state.conversations.length) {
      const div = document.createElement("div");
      div.className = "agent-empty";
      div.style.marginTop = "30px";
      div.textContent = "暂无排队或接管的会话";
      convList.appendChild(div);
      return;
    }
    state.conversations.forEach((conv) => {
      const item = document.createElement("div");
      item.className = "agent-conv-item" + (conv.id === state.currentConvId ? " active" : "");
      const queued = conv.status === "queued";
      const statusText = queued ? "排队中" : `已接入 · ${conv.agent_name || "人工"}`;
      item.innerHTML = `
        <div class="agent-conv-main">
          <div class="agent-conv-title-row">
            <span class="agent-conv-title">${escapeHtml(conv.title || "新会话")}</span>
            <span class="agent-conv-time">${fmtTime(conv.last_time || conv.created_at)}</span>
          </div>
          <div class="agent-conv-preview">${escapeHtml(conv.last_message || "暂无消息")}</div>
          <div class="agent-conv-extra">
            <span class="agent-conv-badge ${queued ? "queued" : "human"}">${statusText}</span>
            ${conv.tag ? `<span class="agent-conv-badge tag">${escapeHtml(conv.tag)}</span>` : ""}
          </div>
        </div>
        <div class="agent-conv-side">
          ${queued ? '<button class="agent-take-btn" data-take="1" type="button">接入</button>' : ""}
          <button class="agent-pin-btn${conv.pinned ? " pinned" : ""}" data-pin="1" type="button"
                  title="${conv.pinned ? "取消置顶" : "置顶"}">📌</button>
        </div>
      `;
      const takeBtn = item.querySelector("[data-take]");
      if (takeBtn) {
        takeBtn.onclick = (e) => {
          e.stopPropagation();
          takeConversation(conv);
        };
      }
      const pinBtn = item.querySelector("[data-pin]");
      if (pinBtn) {
        pinBtn.onclick = (e) => {
          e.stopPropagation();
          togglePinned(conv);
        };
      }
      item.onclick = () => selectConversation(conv);
      convList.appendChild(item);
    });
  }

  async function togglePinned(conv) {
    const target = !conv.pinned;
    try {
      const res = await fetch(`/api/agent/conversations/${conv.id}/pinned`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pinned: target }),
      });
      const data = await res.json();
      if (!res.ok) {
        toast(data.error || "操作失败");
        return;
      }
      conv.pinned = target;
      if (state.currentConvId === conv.id) {
        state.currentConv = data.conversation;
      }
      toast(target ? "已置顶" : "已取消置顶");
      await loadConversations();
    } catch {
      toast("操作失败，请重试");
    }
  }

  async function takeConversation(conv) {
    const btn = convList.querySelector(`.agent-conv-item .agent-take-btn`);
    if (btn) btn.disabled = true;
    try {
      const res = await fetch(`/api/agent/conversations/${conv.id}/assign`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_name: state.agentName }),
      });
      const data = await res.json();
      if (!res.ok) {
        toast(data.error || "接入失败");
        await loadConversations();
        return;
      }
      toast("已接入会话");
      await loadConversations();
      selectConversation(data.conversation);
    } catch {
      toast("接入失败，请重试");
      if (btn) btn.disabled = false;
    }
  }

  function clearCurrent() {
    if (state.ws) {
      state.ws.onclose = null;
      state.ws.close();
      state.ws = null;
    }
    state.currentConvId = null;
    state.currentConv = null;
    messageList.innerHTML = '<div class="agent-empty">从左侧选择会话开始服务</div>';
    chatTitle.textContent = "请选择左侧会话";
    chatAvatar.textContent = "🧑";
    chatStatus.textContent = "—";
    chatTag.textContent = "—";
    chatTag.className = "conv-tag";
    returnQueueBtn.hidden = true;
    sendBtn.disabled = true;
    msgInput.disabled = true;
    metaTag.value = "";
    metaNote.value = "";
    state.metaDirty = { tag: false, note: false };
  }

  // ---------- 选中会话 / WebSocket ----------
  async function selectConversation(conv) {
    state.currentConvId = conv.id;
    state.currentConv = conv;
    // 切换会话：重置编辑标记，让标签/备注输入框重新显示当前会话的数据
    state.metaDirty = { tag: false, note: false };
    renderConvList();
    renderChatHeader();
    fillMeta();

    messageList.innerHTML = "";
    try {
      const res = await fetch(`/api/conversations/${conv.id}/messages`);
      const messages = await res.json();
      messages.forEach((m) => appendMessage(m));
      if (!messages.length) {
        messageList.innerHTML = '<div class="agent-empty">该会话暂无消息</div>';
      }
    } catch {
      toast("加载历史消息失败");
    }

    connectWS(conv.id);
    scrollToBottom(true);
  }

  function connectWS(convId) {
    if (state.ws) {
      state.ws.onclose = null;
      state.ws.close();
    }
    clearTimeout(state.reconnectTimer);
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}/ws/chat?conversation_id=${convId}&role=agent&agent_name=${encodeURIComponent(state.agentName)}`;
    const ws = new WebSocket(url);
    state.ws = ws;

    ws.onopen = () => {
      // 客服打开会话：把用户消息标记为已读
      ws.send(JSON.stringify({ type: "read" }));
    };

    ws.onmessage = (ev) => {
      let data;
      try {
        data = JSON.parse(ev.data);
      } catch {
        return;
      }
      handleWS(data);
    };

    ws.onclose = () => {
      if (state.currentConvId === convId && !document.hidden) {
        state.reconnectTimer = setTimeout(() => connectWS(convId), 1500);
      }
    };
  }

  function handleWS(data) {
    switch (data.type) {
      case "hello":
        if (data.conversation) {
          state.currentConv = data.conversation;
          renderChatHeader();
          fillMeta();
        }
        break;

      case "conversation_updated":
        if (data.conversation && data.conversation.id === state.currentConvId) {
          state.currentConv = data.conversation;
          renderChatHeader();
          fillMeta();
        }
        loadConversations();
        break;

      case "message_ack": {
        const pending = state.pending.get(data.client_id);
        if (pending) {
          pending.msg.id = data.message_id;
          pending.msg.status = "sent";
          pending.el.dataset.id = data.message_id;
          state.pending.delete(data.client_id);
        }
        break;
      }

      case "message":
        // 自己发送的消息不回显（本地已有）；用户/其他客服消息追加
        if (data.sender === "agent") {
          const same = data.client_id && state.pending.get(data.client_id);
          if (same) {
            same.msg.id = data.message_id;
            same.msg.status = "sent";
            same.el.dataset.id = data.message_id;
            state.pending.delete(data.client_id);
            break;
          }
        }
        if (!messageList.querySelector(`.message[data-id="${data.message.id}"]`)) {
          appendMessage(data.message, { smooth: true });
          if (data.message.sender === "user" && state.ws && state.ws.readyState === 1) {
            state.ws.send(JSON.stringify({ type: "read" }));
          }
        }
        loadConversations();
        break;

      case "status":
        (data.message_ids || []).forEach((id) => {
          const el = messageList.querySelector(`.message[data-id="${id}"]`);
          if (!el) return;
          const st = el.querySelector(".msg-status");
          if (st) st.textContent = "✓✓";
        });
        break;

      case "typing":
        if (data.sender === "user") {
          userTypingHint.hidden = !data.is_typing;
          userTypingHint.textContent = data.is_typing ? "对方正在输入…" : "";
        }
        break;
    }
  }

  function renderChatHeader() {
    const conv = state.currentConv;
    if (!conv) return;
    chatTitle.textContent = conv.title || "新会话";
    chatAvatar.textContent = "🧑";
    const queued = conv.status === "queued";
    const human = conv.status === "human";
    if (queued) {
      chatStatus.textContent = "用户在排队等待接入";
      chatTag.textContent = "排队中";
      chatTag.className = "conv-tag queued";
    } else if (human) {
      chatStatus.textContent = conv.agent_name ? `由 ${conv.agent_name} 服务` : "人工服务中";
      chatTag.textContent = "人工";
      chatTag.className = "conv-tag human";
    } else {
      chatStatus.textContent = "会话已切回智能客服";
      chatTag.textContent = "机器人";
      chatTag.className = "conv-tag";
    }
    returnQueueBtn.hidden = !human;
    sendBtn.disabled = queued || (!human && !queued);
    msgInput.disabled = queued || (!human && !queued);
  }

  function fillMeta() {
    const conv = state.currentConv;
    if (!conv) return;
    // 用户正在编辑或已修改过的字段不被轮询/刷新覆盖
    if (!state.metaDirty.tag && document.activeElement !== metaTag) {
      metaTag.value = conv.tag || "";
    }
    if (!state.metaDirty.note && document.activeElement !== metaNote) {
      metaNote.value = conv.note || "";
    }
  }

  // ---------- 发送消息 ----------
  function sendMessage(content) {
    if (!state.currentConv || state.currentConv.status !== "human") {
      toast("请先接入该会话后再发送消息");
      return;
    }
    if (!state.ws || state.ws.readyState !== 1) {
      toast("连接尚未就绪，正在重连…");
      connectWS(state.currentConvId);
      return;
    }

    const clientId = "a" + Date.now() + "_" + Math.random().toString(36).slice(2, 7);
    const msg = {
      id: clientId,
      sender: "human",
      type: content.type || "text",
      content: Object.assign({ agent_name: state.agentName }, content.data),
      status: "sending",
      created_at: new Date().toISOString().slice(0, 19).replace("T", " "),
    };
    const el = appendMessage(msg, { smooth: true });
    state.pending.set(clientId, { msg, el });

    state.ws.send(
      JSON.stringify({
        type: "send",
        client_id: clientId,
        msg_type: content.type || "text",
        content: content.data,
      })
    );
    loadConversations();
  }

  function sendText() {
    const text = msgInput.value.trim();
    if (!text) return;
    sendMessage({ type: "text", data: { text } });
    msgInput.value = "";
    autoResize();
    updateClearBtn();
  }

  function autoResize() {
    msgInput.style.height = "auto";
    msgInput.style.height = Math.min(msgInput.scrollHeight, 110) + "px";
  }

  function updateClearBtn() {
    clearInputBtn.hidden = !msgInput.value;
  }

  // ---------- 会话信息（标签 / 备注） ----------
  async function saveMeta() {
    if (!state.currentConvId) {
      toast("请先选择会话");
      return;
    }
    try {
      const res = await fetch(`/api/agent/conversations/${state.currentConvId}/meta`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag: metaTag.value, note: metaNote.value }),
      });
      const data = await res.json();
      if (!res.ok) {
        toast(data.error || "保存失败");
        return;
      }
      if (state.currentConv) {
        state.currentConv.tag = data.conversation.tag;
        state.currentConv.note = data.conversation.note;
      }
      state.metaDirty = { tag: false, note: false };
      toast("已保存");
      renderConvList();
    } catch {
      toast("保存失败，请重试");
    }
  }

  // ---------- 快捷回复 ----------
  async function loadQuickReplies() {
    try {
      const res = await fetch("/api/agent/quick-replies");
      state.quickReplies = await res.json();
      renderQuickReplies();
      return true;
    } catch {
      return false;
    }
  }

  function renderQuickReplies() {
    qrList.innerHTML = "";
    if (!state.quickReplies.length) {
      const div = document.createElement("div");
      div.className = "agent-qr-empty";
      div.textContent = "暂无快捷回复，点击「新增」添加常用话术";
      qrList.appendChild(div);
      return;
    }
    state.quickReplies.forEach((qr) => {
      const item = document.createElement("div");
      item.className = "agent-qr-item";
      item.innerHTML = `
        <div class="agent-qr-title">${escapeHtml(qr.title)}</div>
        <div class="agent-qr-content">${escapeHtml(qr.content)}</div>
        <div class="agent-qr-actions">
          <button class="mini-btn" data-act="edit" type="button">编辑</button>
          <button class="mini-btn danger" data-act="del" type="button">删除</button>
        </div>
      `;
      item.querySelector("[data-act='edit']").onclick = (e) => {
        e.stopPropagation();
        editQuickReply(qr);
      };
      item.querySelector("[data-act='del']").onclick = (e) => {
        e.stopPropagation();
        deleteQuickReply(qr);
      };
      item.onclick = () => {
        // 点击即一键发送（仅会话为人工状态时可用）
        if (!state.currentConv || state.currentConv.status !== "human") {
          toast("请先接入会话再发送快捷回复");
          return;
        }
        sendMessage({ type: "text", data: { text: qr.content } });
      };
      qrList.appendChild(item);
    });
  }

  function openQuickReplyModal(qr, onSaved, onCancel) {
    showModal(`
      <h3>${qr ? "编辑快捷回复" : "新增快捷回复"}</h3>
      <label>标题</label>
      <input id="qrTitle" type="text" placeholder="如：发货时间" value="${escapeHtml(qr ? qr.title : "")}">
      <label>内容</label>
      <textarea id="qrContent" placeholder="输入常用话术…">${escapeHtml(qr ? qr.content : "")}</textarea>
      <div class="modal-actions">
        <button class="modal-btn plain" id="qrCancel">${onCancel ? "返回" : "取消"}</button>
        <button class="modal-btn primary" id="qrOk">保存</button>
      </div>
    `);
    modalBox.querySelector("#qrCancel").onclick = () => {
      closeModal();
      if (onCancel) onCancel();
    };
    modalBox.querySelector("#qrOk").onclick = async () => {
      const title = modalBox.querySelector("#qrTitle").value.trim();
      const content = modalBox.querySelector("#qrContent").value.trim();
      if (!title || !content) {
        toast("标题和内容不能为空");
        return;
      }
      try {
        const url = qr ? `/api/agent/quick-replies/${qr.id}` : "/api/agent/quick-replies";
        const method = qr ? "PUT" : "POST";
        const res = await fetch(url, {
          method,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title, content }),
        });
        const data = await res.json();
        if (!res.ok) {
          toast(data.error || "保存失败");
          return;
        }
        toast(qr ? "已更新" : "已新增");
        closeModal();
        await loadQuickReplies();
        if (onSaved) onSaved();
      } catch {
        toast("保存失败，请重试");
      }
    };
  }

  function editQuickReply(qr) {
    openQuickReplyModal(qr);
  }

  async function deleteQuickReply(qr) {
    const ok = await confirmDialog("删除快捷回复", `确定删除「${qr.title}」吗？`);
    if (!ok) return;
    try {
      const res = await fetch(`/api/agent/quick-replies/${qr.id}`, { method: "DELETE" });
      if (!res.ok) {
        toast("删除失败");
        return false;
      }
      toast("已删除");
      loadQuickReplies();
      return true;
    } catch {
      toast("删除失败，请重试");
      return false;
    }
  }

  // ---------- 放回排队 ----------
  async function returnToQueue() {
    if (!state.currentConv) return;
    const ok = await confirmDialog(
      "放回排队",
      "确定把这个会话放回排队吗？其他客服可以再次接入，用户会看到排队提示。"
    );
    if (!ok) return;
    try {
      const res = await fetch(`/api/agent/conversations/${state.currentConvId}/return`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_name: state.agentName }),
      });
      const data = await res.json();
      if (!res.ok) {
        toast(data.error || "操作失败");
        return;
      }
      toast("已放回排队");
      clearCurrent();
      await loadConversations();
      if (state.conversations.length) selectConversation(state.conversations[0]);
    } catch {
      toast("操作失败，请重试");
    }
  }

  // ---------- 修改客服姓名 ----------
  function editAgentName() {
    showModal(`
      <h3>客服姓名</h3>
      <label>姓名会展示给用户，并用于会话归属</label>
      <input id="agentNameInput" type="text" value="${escapeHtml(state.agentName)}" maxlength="12">
      <div class="modal-actions">
        <button class="modal-btn plain" id="nameCancel">取消</button>
        <button class="modal-btn primary" id="nameOk">保存</button>
      </div>
    `);
    modalBox.querySelector("#nameCancel").onclick = closeModal;
    modalBox.querySelector("#nameOk").onclick = () => {
      const name = modalBox.querySelector("#agentNameInput").value.trim();
      if (!name) {
        toast("姓名不能为空");
        return;
      }
      setAgentName(name);
      closeModal();
      toast("客服姓名已更新");
      clearCurrent();
      loadConversations();
      loadQuickReplies();
    };
  }

  // ---------- 事件绑定 ----------
  sendBtn.addEventListener("click", sendText);
  msgInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendText();
    }
  });
  msgInput.addEventListener("input", () => {
    autoResize();
    updateClearBtn();
    if (state.ws && state.ws.readyState === 1 && state.currentConv) {
      state.ws.send(JSON.stringify({ type: "typing", sender: "human" }));
    }
  });
  clearInputBtn.addEventListener("click", () => {
    msgInput.value = "";
    autoResize();
    updateClearBtn();
    msgInput.focus();
  });
  $("#refreshConvsBtn").addEventListener("click", () => {
    loadConversations();
    toast("已刷新");
  });
  $("#agentNameBtn").addEventListener("click", editAgentName);
  $("#metaSaveBtn").addEventListener("click", saveMeta);
  metaTag.addEventListener("input", () => { state.metaDirty.tag = true; });
  metaNote.addEventListener("input", () => { state.metaDirty.note = true; });
  $("#addQrBtn").addEventListener("click", () => openQuickReplyModal(null));
  $("#quickReplyBtn").addEventListener("click", openQuickReplyPicker);
  returnQueueBtn.addEventListener("click", returnToQueue);
  modalOverlay.addEventListener("click", (e) => {
    if (e.target === modalOverlay) closeModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
  });

  // ---------- 快捷回复选择器 ----------
  function openQuickReplyPicker() {
    loadQuickReplies().then(() => {
      if (!state.currentConv || state.currentConv.status !== "human") {
        toast("请先接入会话，再使用快捷回复");
        return;
      }
      renderQuickReplyPicker();
    });
  }

  function renderQuickReplyPicker() {
      showModal(
        `
        <h3>⚡ 快捷回复</h3>
        <p style="font-size:12px;color:#8a8a8a;margin-bottom:10px;">点击即可发送给当前用户，也可新增常用话术。</p>
        <div class="qr-picker-list">
          ${
            state.quickReplies.length
              ? state.quickReplies
                  .map(
                    (qr) => `
                      <div class="qr-picker-item" data-qr-id="${qr.id}">
                        <div class="qr-picker-head">
                          <span class="qr-picker-title">${escapeHtml(qr.title)}</span>
                          <div class="qr-picker-ops">
                            <button class="qr-picker-btn" data-edit-id="${qr.id}" type="button" title="编辑">✎</button>
                            <button class="qr-picker-btn danger" data-del-id="${qr.id}" type="button" title="删除">✕</button>
                          </div>
                        </div>
                        <div class="qr-picker-content">${escapeHtml(qr.content)}</div>
                      </div>`
                  )
                  .join("")
              : '<div class="agent-qr-empty">还没有快捷回复，先新增一条吧</div>'
          }
        </div>
        <div class="modal-actions">
          <button class="modal-btn plain" id="qrManageBtn">＋ 新增</button>
          <button class="modal-btn primary" id="qrPickerClose">关闭</button>
        </div>
        `,
        true
      );
    modalBox.querySelectorAll(".qr-picker-item").forEach((item) => {
      item.onclick = (e) => {
        if (e.target.closest(".qr-picker-btn")) return;
        const qr = state.quickReplies.find((q) => q.id === Number(item.dataset.qrId));
        if (!qr) return;
        sendMessage({ type: "text", data: { text: qr.content } });
        closeModal();
        toast("已发送快捷回复");
      };
    });
    modalBox.querySelectorAll(".qr-picker-btn[data-edit-id]").forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        const qr = state.quickReplies.find((q) => q.id === Number(btn.dataset.editId));
        if (!qr) return;
        openQuickReplyModal(qr, openQuickReplyPicker, openQuickReplyPicker);
      };
    });
    modalBox.querySelectorAll(".qr-picker-btn[data-del-id]").forEach((btn) => {
      btn.onclick = async (e) => {
        e.stopPropagation();
        const qr = state.quickReplies.find((q) => q.id === Number(btn.dataset.delId));
        if (!qr) return;
        closeModal();
        const ok = await confirmDialog("删除快捷回复", `确定删除「${qr.title}」吗？`);
        if (!ok) {
          renderQuickReplyPicker();
          return;
        }
        try {
          const res = await fetch(`/api/agent/quick-replies/${qr.id}`, { method: "DELETE" });
          if (!res.ok) {
            toast("删除失败");
          } else {
            toast("已删除");
          }
          await loadQuickReplies();
          renderQuickReplyPicker();
        } catch {
          toast("删除失败，请重试");
          renderQuickReplyPicker();
        }
      };
    });
    modalBox.querySelector("#qrManageBtn").onclick = () => {
      openQuickReplyModal(null, openQuickReplyPicker, openQuickReplyPicker);
    };
    modalBox.querySelector("#qrPickerClose").onclick = closeModal;
  }

  // ---------- 启动 ----------
  function init() {
    agentNameText.textContent = state.agentName;
    clearCurrent();
    loadConversations();
    loadQuickReplies();
    state.pollTimer = setInterval(() => loadConversations(), 4000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) loadConversations();
    });
  }

  init();
})();
