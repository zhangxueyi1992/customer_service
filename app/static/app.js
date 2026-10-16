/* 智能客服对话界面：会话列表 / 聊天窗口 / 实时消息 / 状态流转 / 转人工 / 评价 */
(function () {
  "use strict";

  // ---------- 状态 ----------
  const state = {
    conversations: [],
    currentConvId: null,
    currentConv: null,
    ws: null,
    reconnectTimer: null,
    pending: new Map(), // client_id -> {msg, el}
    botTyping: false,
    rated: {},
  };

  const SENDERS = {
    user: { name: "我", emoji: "🧑", cls: "avatar-user" },
    bot: { name: "智能客服", emoji: "🤖", cls: "avatar-bot" },
    human: { name: "人工客服", emoji: "🧑‍💼", cls: "avatar-human" },
  };

  // ---------- DOM ----------
  const $ = (sel) => document.querySelector(sel);
  const conversationList = $("#conversationList");
  const searchInput = $("#searchInput");
  const searchClearBtn = $("#searchClearBtn");
  const messageList = $("#messageList");
  const messageInput = $("#messageInput");
  const clearInputBtn = $("#clearInputBtn");
  const sendBtn = $("#sendBtn");
  const transferBtn = $("#transferBtn");
  const peerName = $("#peerName");
  const peerAvatar = $("#peerAvatar");
  const peerStatus = $("#peerStatus");
  const peerStatusText = $("#peerStatusText");
  const convTag = $("#convTag");
  const modalOverlay = $("#modalOverlay");
  const modalBox = $("#modalBox");
  const toastEl = $("#toast");
  const emptyHint = $("#emptyHint");
  const starsEl = $("#stars");
  const ratingState = $("#ratingState");

  // ---------- 工具函数 ----------
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
    return modalBox;
  }

  function closeModal() {
    modalOverlay.classList.add("hidden");
    modalBox.innerHTML = "";
    modalBox.classList.remove("modal-lg");
  }

  // ---------- 消息渲染 ----------
  function renderMessage(msg) {
    const side = msg.sender === "user" ? "user" : "other";
    const el = document.createElement("div");
    el.className = `message ${side}`;
    el.dataset.id = msg.id;
    el.dataset.sender = msg.sender;

    if (msg.sender === "system") {
      el.className = "message system";
      const bubble = document.createElement("div");
      bubble.className = "msg-bubble";
      bubble.textContent = msg.content.text || "";
      el.appendChild(bubble);
      return el;
    }

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
    nameSpan.textContent = msg.sender === "user" ? "" : sender.name;
    const timeSpan = document.createElement("span");
    timeSpan.className = "msg-time";
    timeSpan.textContent = fmtTime(msg.created_at);
    meta.appendChild(nameSpan);
    meta.appendChild(timeSpan);
    main.appendChild(meta);

    const bubble = document.createElement("div");
    bubble.className = "msg-bubble";
    bubble.appendChild(renderBubbleContent(msg));
    main.appendChild(bubble);

    if (msg.type === "text" || msg.type === "answer") {
      const actions = document.createElement("div");
      actions.className = "msg-actions";
      const copyBtn = document.createElement("button");
      copyBtn.className = "msg-copy-btn";
      copyBtn.type = "button";
      copyBtn.title = "复制消息内容";
      copyBtn.setAttribute("aria-label", "复制消息内容");
      copyBtn.innerHTML =
        '<svg viewBox="0 0 1024 1024" width="14" height="14" fill="currentColor" aria-hidden="true">' +
        '<path d="M832 64H296c-4.4 0-8 3.6-8 8v56c0 4.4 3.6 8 8 8h496v688c0 4.4 3.6 8 8 8h56c4.4 0 8-3.6 8-8V96c0-17.7-14.3-32-32-32z"/>' +
        '<path d="M704 192H192c-17.7 0-32 14.3-32 32v704c0 17.7 14.3 32 32 32h512c17.7 0 32-14.3 32-32V224c0-17.7-14.3-32-32-32z"/>' +
        "</svg>";
      copyBtn.addEventListener("click", () => copyMessageText(msg));
      actions.appendChild(copyBtn);
      main.appendChild(actions);
    }

    if (msg.sender === "user") {
      const status = document.createElement("span");
      status.className = `msg-status ${msg.status === "sending" ? "sending" : ""}`;
      status.textContent = msg.status === "sending" ? "发送中" : msg.status === "sent" ? "✓" : "✓✓";
      status.title = msg.status === "sending" ? "发送中" : msg.status === "sent" ? "已发送" : "已读";
      meta.appendChild(status);
    }

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
      body.className = "link-card-body";
      body.innerHTML =
        `<div class="link-card-title">${escapeHtml(c.title || "链接")}</div>` +
        (c.description ? `<div class="link-card-desc">${escapeHtml(c.description)}</div>` : "") +
        `<div class="link-card-domain">${escapeHtml(domainOf(c.url))}</div>`;
      card.appendChild(body);
      wrap.appendChild(card);
      return wrap;
    }

    // text / answer
    const text = c.text || "";
    const div = document.createElement("div");
    div.innerHTML = mdToHtml(text);
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
    if (opts.keepEmptyHint) {
      emptyHint.style.display = "none";
    } else {
      emptyHint.style.display = "none";
    }
    const el = renderMessage(msg);
    messageList.appendChild(el);
    scrollToBottom(opts.smooth);
    return el;
  }

  function updateMessageStatus(id, status) {
    const el = messageList.querySelector(`.message[data-id="${id}"]`);
    if (!el) return;
    const st = el.querySelector(".msg-status");
    if (!st) return;
    st.classList.toggle("sending", status === "sending");
    st.textContent = status === "sending" ? "发送中" : status === "sent" ? "✓" : "✓✓";
    st.title = status === "sending" ? "发送中" : status === "sent" ? "已发送" : "已读";
  }

  function messagePlainText(msg) {
    const c = msg.content || {};
    if (msg.type === "text" || msg.type === "answer") return c.text || "";
    if (msg.type === "card") return [c.title, c.description, c.url].filter(Boolean).join("\n");
    if (msg.type === "image") return c.alt || c.url || "";
    return c.text || "";
  }

  function copyMessageText(msg) {
    const text = messagePlainText(msg);
    if (!text) return;
    const done = () => toast("已复制");
    const fallback = () => {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.top = "-9999px";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
        done();
      } catch {
        toast("复制失败，请手动复制");
      }
      ta.remove();
    };
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(fallback);
    } else {
      fallback();
    }
  }

  // ---------- 会话列表 ----------
  async function loadConversations(selectId) {
    try {
      const res = await fetch("/api/conversations");
      state.conversations = await res.json();
      renderConversationList();
      if (selectId != null) {
        const conv = state.conversations.find((c) => c.id === Number(selectId));
        if (conv) selectConversation(conv, { silent: true });
      } else if (!state.currentConvId && state.conversations.length) {
        selectConversation(state.conversations[0], { silent: true });
      } else if (!state.currentConvId && !state.conversations.length) {
        showChatEmpty();
      }
    } catch {
      toast("加载会话列表失败，请确认服务已启动");
    }
  }

  function renderConversationList(filter = "") {
    conversationList.innerHTML = "";
    const kw = filter.trim().toLowerCase();
    const list = state.conversations.filter((c) =>
      !kw || c.title.toLowerCase().includes(kw) || (c.last_message || "").toLowerCase().includes(kw)
    );
    if (!list.length) {
      const div = document.createElement("div");
      div.className = "empty-hint";
      div.style.marginTop = "40px";
      div.textContent = "暂无会话";
      conversationList.appendChild(div);
      return;
    }
    list.forEach((conv) => {
      const item = document.createElement("div");
      item.className = "conv-item" + (conv.id === state.currentConvId ? " active" : "");
      const human = conv.status === "human";
      const queued = conv.status === "queued";
      const avatarCls = human
        ? "conv-avatar" + " " + "avatar-human"
        : conv.last_message
          ? "conv-avatar" + " " + "avatar-bot"
          : "conv-avatar";
      const avatarEmoji = human ? "🧑‍💼" : queued ? "⏳" : "🤖";
      const miniTag = human
        ? '<span class="conv-tag-mini">人工</span>'
        : queued
          ? '<span class="conv-tag-mini queued">排队中</span>'
          : "";
      item.innerHTML = `
        <div class="${avatarCls}">${avatarEmoji}</div>
        <div class="conv-main">
          <div class="conv-title-row">
            <span class="conv-title">${escapeHtml(conv.title || "新会话")}</span>
            <span class="conv-time">${fmtTime(conv.last_time || conv.created_at)}</span>
          </div>
          <div class="conv-preview">
            ${escapeHtml(conv.last_message || "开始新对话")}
            ${miniTag}
          </div>
        </div>
        <button class="conv-delete" title="删除会话">✕</button>
      `;
      item.querySelector(".conv-delete").onclick = (e) => {
        e.stopPropagation();
        deleteConversation(conv);
      };
      item.onclick = () => selectConversation(conv);
      conversationList.appendChild(item);
    });
  }

  async function deleteConversation(conv) {
    const ok = await confirmDialog(
      "删除会话",
      `确定要删除「${conv.title || "新会话"}」吗？删除后该会话及聊天记录将无法恢复。`
    );
    if (!ok) return;
    try {
      const res = await fetch(`/api/conversations/${conv.id}`, { method: "DELETE" });
      if (!res.ok) {
        toast("删除失败，请重试");
        return;
      }
      state.conversations = state.conversations.filter((c) => c.id !== conv.id);

      if (state.currentConvId === conv.id) {
        state.currentConvId = null;
        state.currentConv = null;
        localStorage.removeItem("cs_last_conv");
        if (state.ws) {
          state.ws.onclose = null;
          state.ws.close();
          state.ws = null;
        }
        if (state.conversations.length) {
          selectConversation(state.conversations[0]);
        } else {
          showChatEmpty();
        }
      }
      renderConversationList($("#searchInput").value);
      toast("会话已删除");
    } catch {
      toast("删除失败，请重试");
    }
  }

  function showChatEmpty() {
    messageList.innerHTML = `
      <div class="chat-empty">
        <div class="big">💬</div>
        <div>点击左侧「＋」新建会话</div>
      </div>
    `;
    peerName.textContent = "智能客服";
    peerAvatar.textContent = "🤖";
    setTyping(false);
    transferBtn.disabled = true;
    convTag.textContent = "机器人";
    convTag.classList.remove("human");
    sendBtn.disabled = true;
    messageInput.disabled = true;
  }

  function setChatHeader(conv) {
    const human = conv && conv.status === "human";
    const queued = conv && conv.status === "queued";
    peerAvatar.textContent = human ? "🧑‍💼" : queued ? "⏳" : "🤖";
    peerAvatar.className = "avatar " + (human ? "avatar-human" : queued ? "avatar-bot" : "avatar-bot");
    peerName.textContent = human
      ? (conv.agent_name ? `人工客服 · ${conv.agent_name}` : "人工客服")
      : queued
        ? "排队等待人工"
        : "智能客服";
    if (queued) {
      convTag.textContent = "排队中";
      convTag.classList.remove("human");
      convTag.classList.add("queued");
    } else {
      convTag.textContent = human ? "人工" : "机器人";
      convTag.classList.toggle("human", !!human);
      convTag.classList.remove("queued");
    }
    transferBtn.disabled = false;
    transferBtn.textContent = queued ? "排队中…" : human ? "转回智能客服" : "转人工客服";
    sendBtn.disabled = false;
    messageInput.disabled = false;
  }

  // ---------- 选中会话 / WebSocket ----------
  async function selectConversation(conv, opts = {}) {
    closeKB();
    state.currentConvId = conv.id;
    state.currentConv = conv;
    localStorage.setItem("cs_last_conv", String(conv.id));
    renderConversationList($("#searchInput").value);
    setChatHeader(conv);

    messageList.innerHTML = "";
    emptyHint.style.display = "none";

    try {
      const res = await fetch(`/api/conversations/${conv.id}/messages`);
      const messages = await res.json();
      messages.forEach((m) => appendMessage(m));
      if (!messages.length) emptyHint.style.display = "block";
    } catch {
      toast("加载历史消息失败");
    }

    connectWS(conv.id);
    updateRatingWidget();
    if (!opts.silent) scrollToBottom(true);
  }

  function connectWS(convId) {
    if (state.ws) {
      state.ws.onclose = null;
      state.ws.close();
    }
    clearTimeout(state.reconnectTimer);
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/chat?conversation_id=${convId}`);
    state.ws = ws;

    ws.onopen = () => {
      // 通知服务端：我已读该会话的机器人消息
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
          setChatHeader(data.conversation);
        }
        break;

      case "conversation_updated":
        if (data.conversation && data.conversation.id === state.currentConvId) {
          state.currentConv = data.conversation;
          setChatHeader(data.conversation);
        }
        refreshConversationList();
        break;

      case "message_ack": {
        const pending = state.pending.get(data.client_id);
        if (pending) {
          pending.msg.id = data.message_id;
          pending.msg.status = "sent";
          pending.el.dataset.id = data.message_id;
          updateMessageStatus(data.message_id, "sent");
          state.pending.delete(data.client_id);
        }
        break;
      }

      case "message":
        // 自己发送的消息不回显（本地已有），其他人/机器人的消息追加
        if (data.message.sender === "user") {
          if (!messageList.querySelector(`.message[data-id="${data.message.id}"]`)) {
            appendMessage(data.message, { smooth: true });
          }
        } else {
          appendMessage(data.message, { smooth: true });
          // 用户正在看会话，主动回执已读
          if (state.ws && state.ws.readyState === 1) {
            state.ws.send(JSON.stringify({ type: "read" }));
          }
        }
        refreshConversationList();
        break;

      case "status":
        (data.message_ids || []).forEach((id) => updateMessageStatus(id, data.status));
        break;

      case "typing":
        if (data.sender === "bot" || data.sender === "human") {
          setTyping(!!data.is_typing);
        }
        break;
    }
  }

  function setTyping(typing) {
    if (typing) {
      peerStatusText.textContent = "对方正在输入…";
      peerStatus.classList.add("visible");
    } else {
      peerStatus.classList.remove("visible");
    }
  }

  // ---------- 发送消息 ----------
  function sendMessage(content) {
    if (!state.currentConvId) {
      toast("请先选择或新建会话");
      return;
    }
    if (!state.ws || state.ws.readyState !== 1) {
      toast("连接尚未就绪，正在重连…");
      connectWS(state.currentConvId);
      return;
    }

    const clientId = "c" + Date.now() + "_" + Math.random().toString(36).slice(2, 7);
    const msg = {
      id: clientId,
      sender: "user",
      type: content.type || "text",
      content: content.data,
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
    refreshConversationList();
  }

  function sendText() {
    const text = messageInput.value.trim();
    if (!text) return;
    sendMessage({ type: "text", data: { text } });
    messageInput.value = "";
    autoResize();
    updateClearBtn();
  }

  function sendImage(url) {
    sendMessage({ type: "image", data: { url: url.trim(), alt: "图片" } });
  }

  function sendCard(card) {
    sendMessage({ type: "card", data: card });
  }

  function autoResize() {
    messageInput.style.height = "auto";
    messageInput.style.height = Math.min(messageInput.scrollHeight, 120) + "px";
  }

  function updateClearBtn() {
    clearInputBtn.hidden = !messageInput.value;
  }

  // ---------- 新建会话 ----------
  async function newConversation() {
    try {
      const res = await fetch("/api/conversations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: "新会话" }),
      });
      const conv = await res.json();
      state.conversations.unshift(conv);
      renderConversationList();
      selectConversation(conv);
      emptyHint.style.display = "block";
    } catch {
      toast("新建会话失败");
    }
  }

  // ---------- 转人工 / 切回智能客服 ----------
  async function toggleTransfer() {
    if (!state.currentConvId) return;
    const status = state.currentConv && state.currentConv.status;
    if (status === "queued") {
      toast("正在排队中，客服接入后会自动提醒您");
      return;
    }
    if (status === "human") {
      const ok = await confirmDialog("转回智能客服", "确定要切换回智能客服吗？之后将由机器人继续为您服务。");
      if (!ok) return;
      try {
        const res = await fetch(`/api/conversations/${state.currentConvId}/switch-ai`, {
          method: "POST",
        });
        const data = await res.json();
        if (data.system_message) appendMessage(data.system_message, { smooth: true });
        state.currentConv.status = "bot";
        setChatHeader(state.currentConv);
        refreshConversationList();
        toast("已切换回智能客服");
      } catch {
        toast("切换失败，请重试");
      }
      return;
    }

    const ok = await confirmDialog("转人工客服", "确定要转接人工客服吗？机器人将停止回复，由人工客服接管本会话。");
    if (!ok) return;
    try {
      const res = await fetch(`/api/conversations/${state.currentConvId}/transfer`, {
        method: "POST",
      });
      const data = await res.json();
      if (data.system_message) appendMessage(data.system_message, { smooth: true });
      state.currentConv.status = "queued";
      setChatHeader(state.currentConv);
      refreshConversationList();
      toast("已进入人工排队");
    } catch {
      toast("转接失败，请重试");
    }
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

  // ---------- 满意度评价 ----------
  function updateRatingWidget() {
    const convId = state.currentConvId;
    const done = state.rated[convId];
    starsEl.innerHTML = "";
    for (let i = 1; i <= 5; i++) {
      const star = document.createElement("span");
      star.className = "star" + (done && done >= i ? " on" : "");
      star.textContent = "★";
      star.onclick = () => openRatingModal(i);
      starsEl.appendChild(star);
    }
    if (done) {
      ratingState.textContent = "已评价 " + "★".repeat(done);
      starsEl.style.pointerEvents = "none";
    } else {
      ratingState.textContent = "";
      starsEl.style.pointerEvents = "";
    }
  }

  function openRatingModal(preset) {
    if (state.rated[state.currentConvId]) {
      toast("您已经评价过本次服务啦");
      return;
    }
    let selected = preset || 0;
    showModal(`
      <h3>满意度评价</h3>
      <div class="rate-big-stars" id="bigStars"></div>
      <div class="rate-hint" id="rateHint">点击星星评分</div>
      <label>评价内容（选填）</label>
      <textarea id="rateComment" placeholder="说说您的建议或感受…"></textarea>
      <div class="modal-actions">
        <button class="modal-btn plain" id="rateCancel">取消</button>
        <button class="modal-btn primary" id="rateSubmit">提交评价</button>
      </div>
    `);
    const bigStars = modalBox.querySelector("#bigStars");
    const hint = modalBox.querySelector("#rateHint");
    const labels = ["", "非常不满意", "不满意", "一般", "满意", "非常满意"];
    function paint() {
      bigStars.innerHTML = "";
      for (let i = 1; i <= 5; i++) {
        const s = document.createElement("span");
        s.className = "star" + (i <= selected ? " on" : "");
        s.textContent = "★";
        s.onclick = () => {
          selected = i;
          paint();
          hint.textContent = labels[i];
        };
        bigStars.appendChild(s);
      }
    }
    paint();

    modalBox.querySelector("#rateCancel").onclick = closeModal;
    modalBox.querySelector("#rateSubmit").onclick = async () => {
      if (!selected) {
        toast("请先选择评分");
        return;
      }
      const comment = modalBox.querySelector("#rateComment").value.trim();
      try {
        await fetch(`/api/conversations/${state.currentConvId}/feedback`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ rating: selected, comment }),
        });
        state.rated[state.currentConvId] = selected;
        localStorage.setItem("cs_rated", JSON.stringify(state.rated));
        closeModal();
        updateRatingWidget();
        toast("感谢您的评价！");
      } catch {
        toast("提交失败，请重试");
      }
    };
  }

  // ---------- 图片 / 卡片输入 ----------
  function openImageModal() {
    showModal(`
      <h3>发送图片</h3>
      <label>图片链接</label>
      <input id="imgUrl" type="text" placeholder="https://example.com/image.png">
      <div class="modal-actions">
        <button class="modal-btn plain" id="imgCancel">取消</button>
        <button class="modal-btn primary" id="imgOk">发送</button>
      </div>
    `);
    modalBox.querySelector("#imgCancel").onclick = closeModal;
    modalBox.querySelector("#imgOk").onclick = () => {
      const url = modalBox.querySelector("#imgUrl").value.trim();
      if (!url) {
        toast("请输入图片链接");
        return;
      }
      sendImage(url);
      closeModal();
    };
  }

  function openCardModal() {
    showModal(`
      <h3>发送链接卡片</h3>
      <label>标题</label>
      <input id="cardTitle" type="text" placeholder="卡片标题">
      <label>链接 URL</label>
      <input id="cardUrl" type="text" placeholder="https://example.com">
      <label>描述（选填）</label>
      <textarea id="cardDesc" placeholder="卡片描述…"></textarea>
      <label>封面图（选填）</label>
      <input id="cardImage" type="text" placeholder="https://example.com/cover.png">
      <div class="modal-actions">
        <button class="modal-btn plain" id="cardCancel">取消</button>
        <button class="modal-btn primary" id="cardOk">发送</button>
      </div>
    `);
    modalBox.querySelector("#cardCancel").onclick = closeModal;
    modalBox.querySelector("#cardOk").onclick = () => {
      const title = modalBox.querySelector("#cardTitle").value.trim();
      const url = modalBox.querySelector("#cardUrl").value.trim();
      const description = modalBox.querySelector("#cardDesc").value.trim();
      const image = modalBox.querySelector("#cardImage").value.trim();
      if (!title || !url) {
        toast("标题和链接不能为空");
        return;
      }
      sendCard({ title, url, description, image });
      closeModal();
    };
  }

  // ---------- 会话列表刷新 ----------
  async function refreshConversationList() {
    try {
      const res = await fetch("/api/conversations");
      state.conversations = await res.json();
      renderConversationList($("#searchInput").value);
      const cur = state.conversations.find((c) => c.id === state.currentConvId);
      if (cur) {
        state.currentConv = cur;
        setChatHeader(cur);
      }
    } catch {
      /* 忽略，下次刷新 */
    }
  }

  // ---------- 事件绑定 ----------
  sendBtn.addEventListener("click", sendText);
  transferBtn.addEventListener("click", toggleTransfer);
  $("#newChatBtn").addEventListener("click", newConversation);
  $("#agentOpenBtn").addEventListener("click", () => {
    location.href = "/agent";
  });
  $("#imageBtn").addEventListener("click", openImageModal);
  $("#cardBtn").addEventListener("click", openCardModal);
  searchInput.addEventListener("input", (e) => {
    renderConversationList(e.target.value);
    searchClearBtn.hidden = !e.target.value;
  });
  searchClearBtn.addEventListener("click", () => {
    searchInput.value = "";
    searchClearBtn.hidden = true;
    renderConversationList("");
    searchInput.focus();
  });
  modalOverlay.addEventListener("click", (e) => {
    if (e.target === modalOverlay) closeModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
  });

  messageInput.addEventListener("input", () => {
    autoResize();
    updateClearBtn();
  });
  clearInputBtn.addEventListener("click", () => {
    messageInput.value = "";
    autoResize();
    updateClearBtn();
    messageInput.focus();
  });
  messageInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendText();
    }
  });

  // ---------- 启动 ----------
  function init() {
    try {
      state.rated = JSON.parse(localStorage.getItem("cs_rated") || "{}");
    } catch {
      state.rated = {};
    }
    const lastConv = localStorage.getItem("cs_last_conv");
    loadConversations(lastConv);
  }

  // ---------- 知识库管理 ----------
  const kbView = $("#kbView");
  const kbFileInput = $("#kbFileInput");
  const kbUploadBtn = $("#kbUploadBtn");
  const kbUploadList = $("#kbUploadList");
  const kbStatsEl = $("#kbStats");
  const kbDocTable = $("#kbDocTable");
  const kbDocEmpty = $("#kbDocEmpty");
  const kbTestInput = $("#kbTestInput");
  const kbTestClearBtn = $("#kbTestClearBtn");
  const kbTopK = $("#kbTopK");
  const kbTestBtn = $("#kbTestBtn");
  const kbTestResults = $("#kbTestResults");

  const KB_STATUS = {
    processing: { text: "处理中", cls: "st-processing" },
    completed: { text: "已完成", cls: "st-completed" },
    failed: { text: "失败", cls: "st-failed" },
  };

  function fmtSize(bytes) {
    if (!bytes && bytes !== 0) return "-";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1024 / 1024).toFixed(2) + " MB";
  }

  function fmtDocType(t) {
    const map = {
      pdf: "PDF",
      docx: "Word",
      txt: "TXT",
      md: "Markdown",
      markdown: "Markdown",
      html: "HTML",
      htm: "HTML",
    };
    return map[t] || t || "-";
  }

  function kbStatusHtml(doc) {
    const st = KB_STATUS[doc.status] || { text: doc.status, cls: "" };
    if (doc.status === "completed" && !doc.enabled) {
      return '<span class="kb-status st-disabled">已停用</span>';
    }
    return `<span class="kb-status ${st.cls}">${escapeHtml(st.text)}</span>`;
  }

  function openKB() {
    if (document.body.classList.contains("kb-open")) {
      closeKB();
      return;
    }
    document.body.classList.add("kb-open");
    loadKB();
  }

  function closeKB() {
    document.body.classList.remove("kb-open");
  }

  async function loadKB() {
    try {
      const res = await fetch("/api/knowledge/documents");
      const data = await res.json();
      kbDocs = data.documents || [];
      renderKBStats(data.stats || {});
      renderKBDocs();
      return true;
    } catch {
      toast("加载知识库失败，请确认服务已启动");
      return false;
    }
  }

  let kbDocs = [];

  function renderKBStats(stats) {
    const items = [
      { num: stats.docs || 0, label: "文档" },
      { num: stats.chunks || 0, label: "切片" },
      { num: stats.enabled_chunks || 0, label: "启用切片" },
      { num: stats.completed_docs || 0, label: "已完成" },
      { num: stats.failed_docs || 0, label: "失败" },
    ];
    kbStatsEl.innerHTML = items
      .map(
        (it) => `
          <div class="kb-stat-item">
            <div class="kb-stat-num">${it.num}</div>
            <div class="kb-stat-label">${it.label}</div>
          </div>`
      )
      .join("");
  }

  function renderKBDocs() {
    const tbody = kbDocTable.querySelector("tbody");
    tbody.innerHTML = "";
    kbDocEmpty.style.display = kbDocs.length ? "none" : "block";
    kbDocs.forEach((doc) => {
      const tr = document.createElement("tr");
      const completed = doc.status === "completed";
      tr.innerHTML = `
        <td>
          <div class="doc-name">${escapeHtml(doc.filename)}</div>
          ${doc.error ? `<div class="doc-error" title="${escapeHtml(doc.error)}">${escapeHtml(String(doc.error).slice(0, 42))}</div>` : ""}
        </td>
        <td><span class="doc-type">${fmtDocType(doc.doc_type)}</span></td>
        <td>${fmtSize(doc.size)}</td>
        <td>${doc.chunk_count || 0}${completed && !doc.enabled ? ' <span class="doc-disabled-tag">已停用</span>' : ""}</td>
        <td>${kbStatusHtml(doc)}</td>
        <td>${fmtTime(doc.created_at)}</td>
        <td>
          <div class="doc-actions">
            <button class="mini-btn" data-act="chunks">切片</button>
            <button class="mini-btn" data-act="toggle">${completed ? (doc.enabled ? "停用" : "启用") : "停用"}</button>
            <button class="mini-btn" data-act="reindex">更新</button>
            <button class="mini-btn danger" data-act="delete">删除</button>
          </div>
        </td>
      `;
      tr.querySelectorAll("[data-act]").forEach((btn) => {
        btn.onclick = () => {
          const act = btn.dataset.act;
          if (act === "chunks") viewChunks(doc.id, doc.filename);
          else if (act === "toggle") toggleDoc(doc);
          else if (act === "reindex") reindexDoc(doc);
          else if (act === "delete") deleteDoc(doc);
        };
      });
      tbody.appendChild(tr);
    });
  }

  kbFileInput.addEventListener("change", () => {
    const files = Array.from(kbFileInput.files || []);
    if (!files.length) return;
    kbUploadList.innerHTML = files
      .map(
        (f) => `
          <div class="kb-upload-item">
            <span class="up-name">${escapeHtml(f.name)}</span>
            <span class="up-state">待上传（${fmtSize(f.size)}）</span>
          </div>`
      )
      .join("");
  });

  async function uploadFiles() {
    const files = Array.from(kbFileInput.files || []);
    if (!files.length) {
      toast("请先选择要上传的文件");
      return;
    }
    kbUploadBtn.disabled = true;
    kbUploadBtn.textContent = "处理中…";
    kbUploadList.innerHTML = "";
    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      const row = document.createElement("div");
      row.className = "kb-upload-item";
      row.innerHTML = `
        <span class="up-name">${escapeHtml(file.name)}</span>
        <span class="up-state">上传解析中…（${i + 1}/${files.length}）</span>`;
      kbUploadList.appendChild(row);
      const stateEl = row.querySelector(".up-state");
      try {
        const form = new FormData();
        form.append("files", file);
        const res = await fetch("/api/knowledge/documents", {
          method: "POST",
          body: form,
        });
        const data = await res.json();
        const item = data.results && data.results[0];
        if (item && item.ok) {
          stateEl.textContent = `已完成（${item.doc.chunk_count || 0} 个切片）`;
          stateEl.classList.add("up-ok");
        } else {
          stateEl.textContent = `失败：${item ? item.error : "未知错误"}`;
          stateEl.classList.add("up-fail");
        }
      } catch {
        stateEl.textContent = "上传失败，请确认服务已启动";
        stateEl.classList.add("up-fail");
      }
    }
    kbUploadBtn.disabled = false;
    kbUploadBtn.textContent = "上传并解析";
    kbFileInput.value = "";
    loadKB();
  }

  async function viewChunks(docId, filename) {
    try {
      const res = await fetch(`/api/knowledge/documents/${docId}/chunks`);
      const chunks = await res.json();
      if (!Array.isArray(chunks)) {
        toast("加载切片失败");
        return;
      }
      showModal(
        `
        <div class="chunk-modal">
          <h3>${escapeHtml(filename)} · 切片（${chunks.length}）</h3>
          <div class="chunk-list">
            ${
              chunks.length
                ? chunks
                    .map(
                      (c) => `
                        <div class="chunk-item">
                          <div class="chunk-meta">
                            <span class="chunk-no">#${c.chunk_index}</span>
                            ${c.title ? `<span class="chunk-title">${escapeHtml(c.title)}</span>` : ""}
                            ${c.page ? `<span>第 ${c.page} 页</span>` : ""}
                            <span class="kb-status ${c.enabled ? "st-completed" : "st-disabled"}">${c.enabled ? "启用" : "停用"}</span>
                          </div>
                          <div class="chunk-content">${escapeHtml(c.content)}</div>
                        </div>`
                    )
                    .join("")
                : '<div class="kb-empty">该文档暂无切片</div>'
            }
          </div>
          <div class="modal-actions">
            <button class="modal-btn primary" id="closeChunks">知道了</button>
          </div>
        </div>`,
        true
      );
      modalBox.querySelector("#closeChunks").onclick = closeModal;
    } catch {
      toast("加载切片失败");
    }
  }

  async function toggleDoc(doc) {
    const target = !doc.enabled;
    try {
      const res = await fetch(`/api/knowledge/documents/${doc.id}/enabled`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: target }),
      });
      if (!res.ok) {
        toast("操作失败，请重试");
        return;
      }
      toast(target ? "已启用" : "已停用");
      loadKB();
    } catch {
      toast("操作失败，请重试");
    }
  }

  async function reindexDoc(doc) {
    const ok = await confirmDialog(
      "更新文档",
      `将重新解析「${doc.filename}」并重建切片与向量，确定继续吗？`
    );
    if (!ok) return;
    try {
      const res = await fetch(`/api/knowledge/documents/${doc.id}/reindex`, {
        method: "POST",
      });
      const data = await res.json();
      if (!res.ok || data.error) {
        toast("更新失败：" + (data.error || ""));
        loadKB();
        return;
      }
      toast(`更新完成，共 ${data.chunk_count || 0} 个切片`);
      loadKB();
    } catch {
      toast("更新失败，请重试");
    }
  }

  async function deleteDoc(doc) {
    const ok = await confirmDialog(
      "删除文档",
      `确定要删除「${doc.filename}」吗？其全部切片与向量将被移除，且不可恢复。`
    );
    if (!ok) return;
    try {
      const res = await fetch(`/api/knowledge/documents/${doc.id}`, {
        method: "DELETE",
      });
      if (!res.ok) {
        toast("删除失败，请重试");
        return;
      }
      toast("已删除");
      loadKB();
    } catch {
      toast("删除失败，请重试");
    }
  }

  async function runKBTest() {
    const question = kbTestInput.value.trim();
    if (!question) {
      toast("请输入测试问题");
      return;
    }
    const topK = parseInt(kbTopK.value, 10) || 5;
    kbTestBtn.disabled = true;
    kbTestBtn.textContent = "检索中…";
    try {
      const res = await fetch("/api/knowledge/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, top_k: topK }),
      });
      const data = await res.json();
      if (!res.ok) {
        kbTestResults.innerHTML = `<div class="kb-empty">${escapeHtml(data.error || "检索失败")}</div>`;
        return;
      }
      const results = data.results || [];
      if (!results.length) {
        kbTestResults.innerHTML =
          '<div class="kb-empty">未命中任何启用中的知识切片，试试换个问法或上传更多文档</div>';
        return;
      }
      kbTestResults.innerHTML = results
        .map((r, i) => {
          const pct = Math.max(0, Math.min(100, Math.round(r.similarity * 100)));
          return `
            <div class="test-item">
              <div class="test-head">
                <span class="test-rank">${i + 1}</span>
                <span class="test-source">
                  ${escapeHtml(r.doc_name || "未知文档")}
                  ${r.title ? ` · ${escapeHtml(r.title)}` : ""}
                  ${r.page ? ` · 第 ${r.page} 页` : ""}
                </span>
                <span class="test-score">相似度 ${(r.similarity * 100).toFixed(1)}%</span>
              </div>
              <div class="test-bar"><i style="width:${pct}%"></i></div>
              <div class="test-snippet">${escapeHtml(r.snippet)}</div>
            </div>`;
        })
        .join("");
    } catch {
      kbTestResults.innerHTML = '<div class="kb-empty">检索失败，请确认服务已启动</div>';
    } finally {
      kbTestBtn.disabled = false;
      kbTestBtn.textContent = "测试检索";
    }
  }

  $("#kbOpenBtn").addEventListener("click", openKB);
  $("#kbBackBtn").addEventListener("click", closeKB);
  $("#kbRefreshBtn").addEventListener("click", async () => {
    const btn = $("#kbRefreshBtn");
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = "刷新中…";
    try {
      const ok = await loadKB();
      if (ok) toast("已刷新");
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  });
  kbUploadBtn.addEventListener("click", uploadFiles);
  kbTestBtn.addEventListener("click", runKBTest);
  kbTestInput.addEventListener("input", () => {
    kbTestClearBtn.hidden = !kbTestInput.value;
  });
  kbTestClearBtn.addEventListener("click", () => {
    kbTestInput.value = "";
    kbTestClearBtn.hidden = true;
    kbTestInput.focus();
  });
  kbTestInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      runKBTest();
    }
  });

  init();
})();
