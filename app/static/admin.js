/* 管理后台：指标总览 / 对话记录查询导出 / 账号管理 */
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const toastEl = $("#toast");
  const modalOverlay = $("#modalOverlay");
  const modalBox = $("#modalBox");

  const ROLE_LABEL = { admin: "管理员", agent: "客服", user: "普通用户" };
  const STATUS_LABEL = {
    bot: "机器人",
    queued: "排队中",
    human: "人工服务",
    active: "正常",
    disabled: "停用",
  };

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtTime(ts) {
    if (!ts) return "";
    const d = new Date(String(ts).replace(" ", "T"));
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
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
    const first = modalBox.querySelector("input, select, textarea");
    if (first) first.focus();
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

  function exportParams() {
    return new URLSearchParams({
      keyword: $("#recKeyword").value.trim(),
      status: $("#recStatus").value,
      date_from: $("#recFrom").value,
      date_to: $("#recTo").value,
    }).toString();
  }

  // ---------- 登录 ----------
  async function doLogin() {
    const name = $("#loginName").value.trim();
    const password = $("#loginPass").value;
    $("#loginError").textContent = "";
    if (!name || !password) {
      $("#loginError").textContent = "请输入账号和密码";
      return;
    }
    try {
      const res = await fetch("/api/admin/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, password }),
      });
      const data = await res.json();
      if (!res.ok) {
        $("#loginError").textContent = data.error || "登录失败";
        return;
      }
      localStorage.setItem("cs_admin", JSON.stringify(data.user));
      enterMain(data.user);
    } catch {
      $("#loginError").textContent = "登录失败，请确认服务已启动";
    }
  }

  function enterMain(user) {
    $("#adminUserLabel").textContent = `${user.name}（${ROLE_LABEL[user.role] || user.role}）`;
    $("#loginView").classList.add("hidden");
    $("#mainView").classList.remove("hidden");
    loadStats();
    loadRecords();
    loadUsers();
  }

  function logout() {
    localStorage.removeItem("cs_admin");
    $("#mainView").classList.add("hidden");
    $("#loginView").classList.remove("hidden");
    $("#loginPass").value = "";
  }

  // ---------- 指标总览 ----------
  function renderStats(stats) {
    const cards = [
      { num: stats.total_conversations, label: "总会话数", sub: "" },
      { num: `${stats.transfer_rate}%`, label: "转人工率", sub: `转人工 ${stats.transfer_count} 次` },
      { num: `${stats.kb_hit_rate}%`, label: "知识库命中率", sub: `回答 ${stats.kb_answered} 次` },
      { num: `${stats.avg_response_secs}s`, label: "平均响应时间", sub: "用户发消息到机器人回复" },
      { num: stats.avg_rating, label: "平均满意度", sub: `${stats.feedback_count} 条评价` },
    ];
    $("#statCards").innerHTML = cards
      .map(
        (c) => `
          <div class="stat-card">
            <div class="stat-num">${escapeHtml(c.num)}</div>
            <div class="stat-label">${escapeHtml(c.label)}</div>
            ${c.sub ? `<div class="stat-sub">${escapeHtml(c.sub)}</div>` : ""}
          </div>`
      )
      .join("");

    const dist = stats.rating_dist || {};
    const distHtml = [5, 4, 3, 2, 1]
      .map((r) => `${r} 星：${dist[r] || 0} 条`)
      .join(" · ");
    $("#statCards2").innerHTML = `
      <div class="stat-card">
        <div class="stat-num" style="font-size:15px;font-weight:600;">评分分布</div>
        <div class="stat-sub" style="margin-top:10px;">${escapeHtml(distHtml)}</div>
      </div>
      <div class="stat-card">
        <div class="stat-num" style="font-size:15px;font-weight:600;">人工服务会话</div>
        <div class="stat-sub" style="margin-top:10px;">当前有 ${stats.human_conversations} 个会话处于人工服务/排队</div>
      </div>
      <div class="stat-card">
        <div class="stat-num" style="font-size:15px;font-weight:600;">说明</div>
        <div class="stat-sub" style="margin-top:10px;">命中率=知识库回答/全部知识库问答；响应时间取同会话内机器人回复间隔</div>
      </div>`;
  }

  async function loadStats() {
    try {
      const from = $("#statsFrom").value;
      const to = $("#statsTo").value;
      const params = new URLSearchParams({ date_from: from, date_to: to }).toString();
      const res = await fetch(`/api/admin/stats?${params}`);
      const data = await res.json();
      if (!res.ok) {
        toast(data.error || "加载指标失败");
        return;
      }
      renderStats(data);
    } catch {
      toast("加载指标失败，请确认服务已启动");
    }
  }

  // ---------- 对话记录 ----------
  function renderRecords(records) {
    const tbody = $("#recTbody");
    tbody.innerHTML = "";
    $("#recEmpty").style.display = records.length ? "none" : "block";
    records.forEach((c) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${c.id}</td>
        <td>
          <div class="cell-main">
            <div class="cell-title">${escapeHtml(c.title || "新会话")}</div>
            ${c.tag ? `<div class="cell-preview">标签：${escapeHtml(c.tag)}</div>` : ""}
          </div>
        </td>
        <td><span class="status-badge ${c.status}">${STATUS_LABEL[c.status] || c.status}</span></td>
        <td>${escapeHtml(c.agent_name || "-")}</td>
        <td>${c.msg_count || 0}</td>
        <td><div class="cell-main"><div class="cell-preview">${escapeHtml(c.last_message || "-")}</div></div></td>
        <td>${fmtTime(c.created_at)}</td>
        <td><button class="mini-btn" data-view="1" type="button">详情</button></td>
      `;
      tr.querySelector("[data-view]").onclick = () => viewDetail(c.id, c.title);
      tbody.appendChild(tr);
    });
  }

  async function loadRecords() {
    try {
      const params = new URLSearchParams({
        keyword: $("#recKeyword").value.trim(),
        status: $("#recStatus").value,
        date_from: $("#recFrom").value,
        date_to: $("#recTo").value,
        limit: "500",
      }).toString();
      const res = await fetch(`/api/admin/conversations?${params}`);
      const data = await res.json();
      if (!res.ok) {
        toast(data.error || "查询失败");
        return;
      }
      renderRecords(data);
    } catch {
      toast("查询失败，请确认服务已启动");
    }
  }

  async function viewDetail(convId, title) {
    try {
      const res = await fetch(`/api/admin/conversations/${convId}`);
      const data = await res.json();
      if (!res.ok) {
        toast(data.error || "加载失败");
        return;
      }
      const senderName = { user: "用户", bot: "智能客服", human: "人工客服", system: "系统" };
      showModal(
        `
        <h3>对话详情 · ${escapeHtml(data.title || title || "新会话")}</h3>
        <p style="font-size:12px;color:#8a8a8a;margin-bottom:10px;">
          状态：${STATUS_LABEL[data.status] || data.status}
          ${data.agent_name ? ` · 客服：${escapeHtml(data.agent_name)}` : ""}
          ${data.tag ? ` · 标签：${escapeHtml(data.tag)}` : ""}
        </p>
        <div class="detail-msgs">
          ${
            data.messages.length
              ? data.messages
                  .map((m) => {
                    const content = m.content || {};
                    const text = content.text || content.title || content.url || "";
                    return `
                      <div class="detail-msg ${m.sender}">
                        <div class="dm-meta">
                          <span class="dm-sender">${senderName[m.sender] || m.sender}</span>
                          <span>${fmtTime(m.created_at)}</span>
                        </div>
                        <div>${escapeHtml(text) || `[${m.type}]`}</div>
                      </div>`;
                  })
                  .join("")
              : '<div class="admin-empty">该会话暂无消息</div>'
          }
        </div>
        <div class="modal-actions">
          <button class="modal-btn primary" id="detailClose">关闭</button>
        </div>
        `,
        true
      );
      modalBox.querySelector("#detailClose").onclick = closeModal;
    } catch {
      toast("加载详情失败");
    }
  }

  function doExport(fmt) {
    const params = exportParams();
    window.location.href = `/api/admin/export?fmt=${fmt}&${params}`;
  }

  // ---------- 账号管理 ----------
  function renderUsers(users) {
    const tbody = $("#userTbody");
    tbody.innerHTML = "";
    $("#userEmpty").style.display = users.length ? "none" : "block";
    users.forEach((u) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${u.id}</td>
        <td>${escapeHtml(u.name)}</td>
        <td><span class="role-badge ${u.role}">${ROLE_LABEL[u.role] || u.role}</span></td>
        <td><span class="status-badge ${u.status}">${STATUS_LABEL[u.status] || u.status}</span></td>
        <td>${fmtTime(u.created_at)}</td>
        <td>
          <button class="mini-btn" data-edit="1" type="button">编辑</button>
          <button class="mini-btn danger" data-del="1" type="button">删除</button>
        </td>
      `;
      tr.querySelector("[data-edit]").onclick = () => openUserModal(u);
      tr.querySelector("[data-del]").onclick = () => deleteUser(u);
      tbody.appendChild(tr);
    });
  }

  async function loadUsers() {
    try {
      const res = await fetch("/api/admin/users");
      const data = await res.json();
      if (!res.ok) {
        toast(data.error || "加载账号失败");
        return;
      }
      renderUsers(data);
    } catch {
      toast("加载账号失败");
    }
  }

  function openUserModal(user) {
    showModal(`
      <h3>${user ? "编辑账号" : "新增账号"}</h3>
      <label>账号名</label>
      <input id="uName" type="text" value="${escapeHtml(user ? user.name : "")}" ${user ? "disabled" : ""}>
      <label>角色</label>
      <select id="uRole">
        <option value="admin" ${user && user.role === "admin" ? "selected" : ""}>管理员</option>
        <option value="agent" ${!user || user.role === "agent" ? "selected" : ""}>客服</option>
        <option value="user" ${user && user.role === "user" ? "selected" : ""}>普通用户</option>
      </select>
      <label>密码${user ? "（留空则不修改）" : ""}</label>
      <input id="uPass" type="password" placeholder="${user ? "留空保持不变" : "请输入密码"}">
      <div class="modal-actions">
        <button class="modal-btn plain" id="uCancel">取消</button>
        <button class="modal-btn primary" id="uOk">保存</button>
      </div>
    `);
    modalBox.querySelector("#uCancel").onclick = closeModal;
    modalBox.querySelector("#uOk").onclick = async () => {
      const name = (modalBox.querySelector("#uName").value || "").trim();
      const role = modalBox.querySelector("#uRole").value;
      const password = modalBox.querySelector("#uPass").value;
      if (!name) {
        toast("账号名不能为空");
        return;
      }
      try {
        if (user) {
          const body = { role };
          if (password) body.password = password;
          const res = await fetch(`/api/admin/users/${user.id}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
          const data = await res.json();
          if (!res.ok) {
            toast(data.error || "保存失败");
            return;
          }
          toast("已保存");
        } else {
          const res = await fetch("/api/admin/users", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, role, password }),
          });
          const data = await res.json();
          if (!res.ok) {
            toast(data.error || "创建失败");
            return;
          }
          toast("已创建");
        }
        closeModal();
        loadUsers();
      } catch {
        toast("保存失败，请重试");
      }
    };
  }

  async function deleteUser(user) {
    const ok = await confirmDialog("删除账号", `确定删除账号「${user.name}」吗？`);
    if (!ok) return;
    try {
      const res = await fetch(`/api/admin/users/${user.id}`, { method: "DELETE" });
      const data = await res.json();
      if (!res.ok) {
        toast(data.error || "删除失败");
        return;
      }
      toast("已删除");
      loadUsers();
    } catch {
      toast("删除失败，请重试");
    }
  }

  // ---------- 标签页 ----------
  document.querySelectorAll(".admin-tab").forEach((tab) => {
    tab.onclick = () => {
      document.querySelectorAll(".admin-tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      document.querySelectorAll(".admin-section").forEach((s) => s.classList.add("hidden"));
      $("#tab-" + tab.dataset.tab).classList.remove("hidden");
      if (tab.dataset.tab === "stats") loadStats();
      if (tab.dataset.tab === "records") loadRecords();
      if (tab.dataset.tab === "users") loadUsers();
    };
  });

  // ---------- 事件绑定 ----------
  $("#loginBtn").addEventListener("click", doLogin);
  $("#loginPass").addEventListener("keydown", (e) => {
    if (e.key === "Enter") doLogin();
  });
  $("#logoutBtn").addEventListener("click", logout);
  $("#statsRefreshBtn").addEventListener("click", loadStats);
  $("#recSearchBtn").addEventListener("click", loadRecords);
  $("#recExportCsv").addEventListener("click", () => doExport("csv"));
  $("#recExportJson").addEventListener("click", () => doExport("json"));
  $("#addUserBtn").addEventListener("click", () => openUserModal(null));
  modalOverlay.addEventListener("click", (e) => {
    if (e.target === modalOverlay) closeModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
  });

  // ---------- 启动 ----------
  function init() {
    const saved = localStorage.getItem("cs_admin");
    if (saved) {
      try {
        const user = JSON.parse(saved);
        enterMain(user);
        return;
      } catch {
        /* 忽略损坏的会话 */
      }
    }
    $("#loginView").classList.remove("hidden");
  }

  init();
})();
