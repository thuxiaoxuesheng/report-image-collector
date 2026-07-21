const state = {
  page: "dashboard",
  tasks: [],
  allImages: [],
  images: [],
  activeKeywordId: "",
  aiPollTimer: null,
  modalIndex: -1,
  browserTimer: null,
  statusTimer: null,
  statusRefreshBusy: false,
  noticeTimer: null,
  user: null,
  requestedReviewTaskId: "",
};

const pageInfo = {
  dashboard: ["运行概览", "查看任务状态、待审核图片与保留时间"],
  browser: ["小红书登录", "在受限浏览器中自行完成扫码、手机号和验证码登录"],
  tasks: ["采集任务", "选择内置检索流程，或自由配置多个关键词"],
  review: ["图片审核", "筛选、预览并决定最终导出的图片"],
  settings: ["个人设置", "查看管理员统一配置的AI报告单判断模型"],
  admin: ["用户管理", "创建、停用和维护相互隔离的用户"],
};

function reportVerdict(image) {
  if (image.ai_is_report === true) return "报告单";
  if (image.ai_is_report === false) return "非报告";
  return "未判断";
}

const statusNames = {
  draft: "草稿", queued: "排队中", running: "采集中", classifying: "AI筛选中",
  needs_attention: "需要处理", paused: "已暂停", review: "待审核",
  exported: "已导出", cancelled: "已取消", failed: "失败",
};

const searchTemplates = {
  complete: {
    description: "推荐：用10个具体报告类别换取更高多样性，避免宽泛词被体检攻略和就医经验淹没。",
    keywords: ["血常规报告单", "肝功能检验报告", "甲状腺功能报告", "微生物培养药敏报告", "CT检查报告", "MRI检查报告", "超声检查报告", "内镜检查报告", "心电图检查报告", "病理诊断报告"],
  },
  laboratory: {
    description: "常规检验：每个关键词对应一种报告，兼顾血液、体液、生化、凝血和微生物。",
    keywords: ["血常规报告单", "尿常规报告单", "便常规检验报告", "肝功能检验报告", "肾功能检验报告", "血脂检验报告", "血糖检验报告", "凝血功能报告", "肿瘤标志物报告", "培养药敏报告"],
  },
  specialty: {
    description: "专项检验：补充内分泌、免疫、生殖、产前筛查和骨髓等较少见报告。",
    keywords: ["甲状腺功能报告", "性激素六项报告", "糖耐量检验报告", "过敏原检测报告", "自身免疫抗体报告", "传染病检验报告", "无创DNA检测报告", "唐氏筛查报告", "骨髓检查报告", "精液分析报告"],
  },
  imaging: {
    description: "检查报告：覆盖医学影像、内镜和常见功能检查，不下载视频笔记。",
    keywords: ["CT检查报告", "MRI检查报告", "超声检查报告", "X光检查报告", "PET-CT检查报告", "胃镜检查报告", "肠镜检查报告", "心电图检查报告", "肺功能检查报告", "骨密度检查报告"],
  },
  pathology: {
    description: "病理与分子：覆盖组织病理、细胞学、免疫组化及分子检测报告。",
    keywords: ["病理诊断报告", "活检病理报告", "术后病理报告", "免疫组化报告", "细胞学检查报告", "宫颈TCT报告", "骨髓病理报告", "基因检测报告", "染色体核型报告", "分子病理报告"],
  },
  core: {
    description: "补漏流程（噪声较高）：仅在具体类别流程采集不足时使用，并建议开启AI筛选。",
    keywords: ["体检报告单", "化验报告单", "检验结果报告", "检查结果报告"],
  },
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && typeof options.body !== "string") {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const data = await response.json();
      message = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch (_) {}
    throw new Error(message);
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response;
}

function notice(message, kind = "info", timeout = 5000) {
  const el = document.getElementById("notice");
  el.textContent = message;
  el.className = `notice ${kind}`;
  clearTimeout(state.noticeTimer);
  state.noticeTimer = setTimeout(() => el.classList.add("hidden"), timeout);
}

function formatDate(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Shanghai" }).format(new Date(value));
}

function showPage(name) {
  state.page = name;
  document.querySelectorAll(".page").forEach(el => el.classList.toggle("active", el.id === `page-${name}`));
  document.querySelectorAll(".nav-item").forEach(el => el.classList.toggle("active", el.dataset.page === name));
  document.getElementById("page-title").textContent = pageInfo[name][0];
  document.getElementById("page-subtitle").textContent = pageInfo[name][1];
  if (name !== "browser") stopBrowserPolling();
  if (name === "dashboard") loadDashboard();
  if (name === "tasks") loadTasks();
  if (name === "review") loadReviewTasks();
  if (name === "settings") loadSettings();
  if (name === "admin") { loadAdminUsers(); loadAdminQueue(); }
  if (name === "browser") refreshBrowserStatus();
}

function keywordRow(keyword = "", count = 50) {
  const row = document.createElement("div");
  row.className = "keyword-row";
  row.innerHTML = `<input class="keyword-input" maxlength="120" required placeholder="输入搜索关键词" value="${escapeHtml(keyword)}"><input class="keyword-count" type="number" min="1" step="1" value="${count}" title="目标笔记数（不设上限）"><button type="button" title="删除关键词">×</button>`;
  row.querySelector("button").addEventListener("click", () => {
    if (document.querySelectorAll(".keyword-row").length <= 1) return notice("至少保留一个关键词", "error");
    row.remove();
  });
  document.getElementById("keyword-list").appendChild(row);
}

function applyTemplate() {
  const key = document.getElementById("search-template").value;
  const template = searchTemplates[key];
  document.getElementById("keyword-list").innerHTML = "";
  template.keywords.forEach(keyword => keywordRow(keyword, 50));
  document.getElementById("template-description").textContent = template.description;
}

function taskCard(task) {
  const keywords = (task.keywords || []).map(k => `${escapeHtml(k.keyword)} ${k.collected_count}/${k.target_count}`).join(" · ");
  const actions = [];
  if (task.status === "queued" && state.user?.role === "admin") actions.push(`<button class="secondary" data-action="move_up" title="在等待队列中上移">↑ 上移</button><button class="secondary" data-action="move_down" title="在等待队列中下移">↓ 下移</button>`);
  if (["queued","running","classifying","needs_attention"].includes(task.status)) actions.push(`<button class="secondary" data-action="pause">暂停</button>`);
  if (["paused","needs_attention","failed"].includes(task.status)) actions.push(`<button class="secondary" data-action="resume">恢复</button>`);
  if (!["exported","cancelled"].includes(task.status)) actions.push(`<button class="secondary" data-action="cancel">取消</button>`);
  if (["review","exported"].includes(task.status)) actions.push(`<button class="primary" data-review="1">审核</button>`);
  return `<article class="task-card" data-task-id="${task.id}">
    <div class="task-main"><div class="task-title-row"><strong>${escapeHtml(task.name)}</strong><span class="badge ${task.status}">${statusNames[task.status] || task.status}</span></div>
    <div class="task-meta"><span>${task.start_date} 至 ${task.end_date}</span><span>${task.keywords.length} 个关键词</span><span>创建于 ${formatDate(task.created_at)}</span></div>
    <div class="task-progress">${escapeHtml(task.attention_reason || task.progress_message || keywords || "等待执行")}</div></div>
    <div class="task-actions">${actions.join("")}</div></article>`;
}

function bindTaskCards(container) {
  container.querySelectorAll("[data-action]").forEach(button => button.addEventListener("click", async event => {
    const card = event.target.closest(".task-card");
    try {
      await api(`/api/tasks/${card.dataset.taskId}/action`, { method: "POST", body: { action: event.target.dataset.action } });
      notice("任务状态已更新", "success");
      await loadTasks();
      if (state.page === "dashboard") await loadDashboard();
    } catch (error) { notice(error.message, "error"); }
  }));
  container.querySelectorAll("[data-review]").forEach(button => button.addEventListener("click", event => {
    const taskId = event.target.closest(".task-card").dataset.taskId;
    state.requestedReviewTaskId = taskId;
    showPage("review");
  }));
}

async function loadDashboard() {
  try {
    const data = await api("/api/dashboard");
    const metrics = [
      ["排队任务", data.queued, "等待可用执行器"], ["正在运行", data.running, "不同用户可并行"],
      ["需要处理", data.needs_attention, "登录或页面限制"], ["待审核", data.awaiting_review, "请在过期前处理"],
      ["已选图片", data.selected_images, data.expiring_soon ? `${data.expiring_soon} 个任务即将过期` : "当前可导出"],
    ];
    document.getElementById("metrics").innerHTML = metrics.map(m => `<div class="metric-card"><span>${m[0]}</span><strong>${m[1]}</strong><small>${m[2]}</small></div>`).join("");
    const container = document.getElementById("recent-tasks");
    container.innerHTML = data.recent_tasks.length ? data.recent_tasks.map(taskCard).join("") : `<div class="empty-state">还没有任务，先创建一个检索流程</div>`;
    bindTaskCards(container);
  } catch (error) { notice(error.message, "error"); }
}

async function loadTasks() {
  try {
    state.tasks = await api("/api/tasks");
    const container = document.getElementById("all-tasks");
    container.innerHTML = state.tasks.length ? state.tasks.map(taskCard).join("") : `<div class="empty-state">暂无任务</div>`;
    bindTaskCards(container);
  } catch (error) { notice(error.message, "error"); }
}

async function refreshCurrentStatusPage() {
  if (document.hidden || state.statusRefreshBusy) return;
  state.statusRefreshBusy = true;
  try {
    if (state.page === "dashboard") await loadDashboard();
    else if (state.page === "tasks") await loadTasks();
    else if (state.page === "admin") {
      await Promise.all([loadAdminUsers(), loadAdminQueue()]);
    }
  } finally {
    state.statusRefreshBusy = false;
  }
}

function startStatusPolling() {
  clearInterval(state.statusTimer);
  state.statusTimer = setInterval(refreshCurrentStatusPage, 5000);
}

async function createTask(event) {
  event.preventDefault();
  const keywords = [...document.querySelectorAll(".keyword-row")].map(row => ({
    keyword: row.querySelector(".keyword-input").value.trim(),
    target_count: Number(row.querySelector(".keyword-count").value),
  })).filter(item => item.keyword);
  if (!keywords.length) return notice("请至少填写一个关键词", "error");
  const payload = {
    name: document.getElementById("task-name").value.trim(),
    start_date: document.getElementById("start-date").value,
    end_date: document.getElementById("end-date").value,
    keywords,
    privacy_confirmed: document.getElementById("privacy-confirm").checked,
    ai_confirmed: false,
  };
  try {
    await api("/api/tasks", { method: "POST", body: payload });
    notice("任务已进入队列", "success");
    document.getElementById("task-name").value = "";
    document.getElementById("privacy-confirm").checked = false;
    await loadTasks();
  } catch (error) { notice(error.message, "error", 8000); }
}

async function refreshBrowserStatus() {
  try {
    const data = await api("/api/browser/status");
    const box = document.getElementById("login-state");
    box.className = `state-card ${data.login_detected ? "ok" : "warn"}`;
    box.innerHTML = data.login_detected ? `<span>●</span><div><strong>已检测到登录状态</strong><small>登录目录保存在本机</small></div>` : `<span>●</span><div><strong>尚未登录或登录已失效</strong><small>请打开浏览器完成登录</small></div>`;
    if (data.remote_active) startBrowserPolling();
  } catch (error) { notice(error.message, "error"); }
}

function startBrowserPolling() {
  const screen = document.getElementById("browser-screen");
  screen.classList.remove("empty");
  clearInterval(state.browserTimer);
  const refresh = () => { document.getElementById("browser-image").src = `/api/browser/screenshot?t=${Date.now()}`; };
  refresh(); state.browserTimer = setInterval(refresh, 1200);
}
function stopBrowserPolling() { clearInterval(state.browserTimer); state.browserTimer = null; }

async function openBrowser() {
  try { await api("/api/browser/open", { method: "POST" }); startBrowserPolling(); notice("受限浏览器已开启", "success"); }
  catch (error) { notice(error.message, "error"); }
}
async function closeBrowser() {
  try { await api("/api/browser/close", { method: "POST" }); stopBrowserPolling(); document.getElementById("browser-screen").classList.add("empty"); await refreshBrowserStatus(); notice("浏览器控制已关闭，登录状态仍保留", "success"); }
  catch (error) { notice(error.message, "error"); }
}

async function loadReviewTasks() {
  try {
    state.tasks = await api("/api/tasks");
    const select = document.getElementById("review-task");
    const prior = state.requestedReviewTaskId || select.value;
    const candidates = state.tasks.filter(task => ["review","exported","paused","needs_attention","cancelled","failed"].includes(task.status));
    select.innerHTML = `<option value="">选择任务</option>` + candidates.map(t => `<option value="${t.id}">${escapeHtml(t.name)} · ${statusNames[t.status]}</option>`).join("");
    if (candidates.some(t => t.id === prior)) { select.value = prior; state.requestedReviewTaskId = ""; loadImages(); }
    else if (candidates.length === 1) { select.value = candidates[0].id; loadImages(); }
  } catch (error) { notice(error.message, "error"); }
}

async function loadImages() {
  const taskId = document.getElementById("review-task").value;
  const grid = document.getElementById("image-grid");
  if (!taskId) {
    state.allImages = []; state.images = []; state.activeKeywordId = "";
    document.getElementById("review-keywords").innerHTML = "";
    document.getElementById("review-summary").innerHTML = `<strong>请选择任务</strong>`;
    grid.innerHTML = `<div class="empty-state">请选择一个待审核任务</div>`;
    return;
  }
  try {
    const query = new URLSearchParams({ task_id: taskId });
    state.allImages = await api(`/api/images?${query}`);
    const keywordIds = new Set(state.allImages.map(image => image.keyword_id));
    if (state.activeKeywordId && !keywordIds.has(state.activeKeywordId)) state.activeKeywordId = "";
    renderKeywordTabs();
    renderImages();
  } catch (error) { notice(error.message, "error"); }
}

function keywordGroups() {
  const groups = new Map();
  state.allImages.forEach(image => {
    if (!groups.has(image.keyword_id)) groups.set(image.keyword_id, { id: image.keyword_id, keyword: image.keyword, images: [] });
    groups.get(image.keyword_id).images.push(image);
  });
  return [...groups.values()];
}

function renderKeywordTabs() {
  const groups = keywordGroups();
  const selectedTotal = state.allImages.filter(image => image.selected).length;
  const tabs = document.getElementById("review-keywords");
  tabs.innerHTML = `<button class="keyword-tab ${state.activeKeywordId ? "" : "active"}" data-keyword-id="">全部 <strong>${selectedTotal}/${state.allImages.length}</strong></button>` + groups.map(group => {
    const selected = group.images.filter(image => image.selected).length;
    return `<button class="keyword-tab ${state.activeKeywordId === group.id ? "active" : ""}" data-keyword-id="${escapeHtml(group.id)}">${escapeHtml(group.keyword)} <strong>${selected}/${group.images.length}</strong></button>`;
  }).join("");
  tabs.querySelectorAll("[data-keyword-id]").forEach(button => button.addEventListener("click", () => {
    state.activeKeywordId = button.dataset.keywordId;
    renderKeywordTabs(); renderImages();
  }));
}

function filteredImages() {
  const view = document.getElementById("review-view").value;
  const noteFilter = document.getElementById("review-note-filter").value.trim().toLowerCase();
  return state.allImages.filter(image => {
    if (state.activeKeywordId && image.keyword_id !== state.activeKeywordId) return false;
    const noteText = `${image.note_title || ""} ${image.platform_note_id || ""}`.toLowerCase();
    if (noteFilter && !noteText.includes(noteFilter)) return false;
    if (view === "selected" && !image.selected) return false;
    if (view === "excluded" && image.selected) return false;
    if (view === "ai_non_report" && image.ai_is_report !== false) return false;
    return true;
  });
}

function updateReviewSummary() {
  const total = state.allImages.length;
  const selected = state.allImages.filter(image => image.selected).length;
  const excluded = total - selected;
  const reports = state.allImages.filter(image => image.ai_is_report === true).length;
  const nonReports = state.allImages.filter(image => image.ai_is_report === false).length;
  const unreviewed = total - reports - nonReports;
  document.getElementById("review-summary").innerHTML = `<strong>保留 ${selected} / ${total} 张</strong><span>已排除 ${excluded}</span><span>AI报告单 ${reports}</span><span>AI非报告 ${nonReports}</span><span>AI未判断 ${unreviewed}</span>`;
  document.getElementById("review-count").textContent = `当前视图：保留 ${state.images.filter(image => image.selected).length} / ${state.images.length}`;
  document.getElementById("export-selected").textContent = `一键导出 ${selected} 张 ZIP`;
}

function renderImages() {
  const container = document.getElementById("image-grid");
  state.images = filteredImages();
  updateReviewSummary();
  if (!state.images.length) {
    container.innerHTML = `<div class="empty-state">当前筛选条件下没有图片</div>`;
    return;
  }
  const size = document.getElementById("review-size").value;
  const groups = new Map();
  state.images.forEach((image, index) => {
    if (!groups.has(image.keyword_id)) groups.set(image.keyword_id, { keyword: image.keyword, items: [] });
    groups.get(image.keyword_id).items.push({ image, index });
  });
  container.innerHTML = [...groups.entries()].map(([keywordId, group]) => {
    const allInKeyword = state.allImages.filter(image => image.keyword_id === keywordId);
    const selectedInKeyword = allInKeyword.filter(image => image.selected).length;
    const cards = group.items.map(({ image, index }) => `<article class="image-card ${image.selected ? "selected" : "excluded"}" data-index="${index}" data-image-id="${image.id}">
      <button class="image-toggle" title="点击${image.selected ? "排除" : "恢复"}这张图片" aria-label="${image.selected ? "排除" : "恢复"}图片">
        <img src="${image.thumbnail_url}" alt="${escapeHtml(image.note_title || image.platform_note_id)}" loading="lazy">
        <span class="selection-mark">${image.selected ? "✓ 保留" : "× 已排除"}</span>
      </button>
      <button class="zoom-button" title="查看大图" aria-label="查看大图">⌕</button>
      <span class="category-chip">${reportVerdict(image)}</span>
      <div class="image-card-footer"><strong>${escapeHtml(image.note_title || "无标题")}</strong><small>${escapeHtml(image.platform_note_id)} · 第${image.ordinal}张</small></div>
    </article>`).join("");
    return `<section class="keyword-section" data-section-keyword="${escapeHtml(keywordId)}"><div class="keyword-section-head"><div><h3>${escapeHtml(group.keyword)}</h3><span>保留 ${selectedInKeyword} / ${allInKeyword.length} 张</span></div><div class="button-row"><button class="secondary" data-section-action="select" data-keyword-id="${escapeHtml(keywordId)}">本组全选</button><button class="secondary" data-section-action="exclude" data-keyword-id="${escapeHtml(keywordId)}">本组全不选</button><button class="secondary" data-section-action="invert" data-keyword-id="${escapeHtml(keywordId)}">本组反选</button></div></div><div class="image-grid size-${size}">${cards}</div></section>`;
  }).join("");
  container.querySelectorAll(".image-toggle").forEach(button => button.addEventListener("click", async event => {
    const card = event.currentTarget.closest(".image-card");
    const image = state.images[Number(card.dataset.index)];
    try {
      await api(`/api/images/${image.id}`, { method: "PATCH", body: { selected: !image.selected } });
      image.selected = !image.selected;
      const needsRefilter = document.getElementById("review-view").value !== "all";
      renderKeywordTabs();
      if (needsRefilter) renderImages(); else updateCardState(card, image);
    } catch (error) { notice(error.message, "error"); }
  }));
  container.querySelectorAll(".zoom-button").forEach(button => button.addEventListener("click", event => openModal(Number(event.currentTarget.closest(".image-card").dataset.index))));
  container.querySelectorAll("[data-section-action]").forEach(button => button.addEventListener("click", async () => {
    const ids = state.images.filter(image => image.keyword_id === button.dataset.keywordId).map(image => image.id);
    const action = button.dataset.sectionAction;
    await bulkImageIds(ids, action === "select" ? true : action === "exclude" ? false : null, action === "invert");
  }));
}

function updateCardState(card, image) {
  card.classList.toggle("selected", image.selected);
  card.classList.toggle("excluded", !image.selected);
  const mark = card.querySelector(".selection-mark"); mark.textContent = image.selected ? "✓ 保留" : "× 已排除";
  const button = card.querySelector(".image-toggle"); button.title = `点击${image.selected ? "排除" : "恢复"}这张图片`; button.setAttribute("aria-label", `${image.selected ? "排除" : "恢复"}图片`);
  renderKeywordTabs(); updateReviewSummary();
  const section = card.closest(".keyword-section");
  if (section) {
    const keywordId = section.dataset.sectionKeyword;
    const images = state.allImages.filter(item => item.keyword_id === keywordId);
    section.querySelector(".keyword-section-head span").textContent = `保留 ${images.filter(item => item.selected).length} / ${images.length} 张`;
  }
}

async function bulkImageIds(imageIds, selected = null, invert = false) {
  if (!imageIds.length) return;
  try {
    const batchSize = 1000;
    for (let offset = 0; offset < imageIds.length; offset += batchSize) {
      await api("/api/images/bulk", {
        method: "POST",
        body: { image_ids: imageIds.slice(offset, offset + batchSize), selected, invert },
      });
    }
    await loadImages();
  } catch (error) { notice(error.message, "error"); }
}

async function bulkSelect(selected = null, invert = false) { return bulkImageIds(state.images.map(image => image.id), selected, invert); }

function startAiPolling(taskId) {
  clearInterval(state.aiPollTimer);
  const button = document.getElementById("ai-classify"); button.disabled = true; button.textContent = "AI筛选中…";
  state.aiPollTimer = setInterval(async () => {
    try {
      const task = await api(`/api/tasks/${taskId}`);
      if (task.status === "classifying") { notice(task.progress_message || "AI筛选中…", "info", 2500); return; }
      clearInterval(state.aiPollTimer); state.aiPollTimer = null; button.disabled = false; button.textContent = "✦ AI一键筛选（可选）";
      await loadImages();
      notice(task.progress_message || "AI筛选完成，可继续人工修改", task.attention_reason ? "error" : "success", 8000);
    } catch (error) { clearInterval(state.aiPollTimer); state.aiPollTimer = null; button.disabled = false; button.textContent = "✦ AI一键筛选（可选）"; notice(error.message, "error"); }
  }, 1800);
}

async function runAiClassification() {
  const taskId = document.getElementById("review-task").value;
  if (!taskId) return notice("请先选择任务", "error");
  const group = keywordGroups().find(item => item.id === state.activeKeywordId);
  const scope = group ? `当前检索词“${group.keyword}”` : "全部检索词";
  if (!window.confirm(`将对${scope}执行AI筛选：报告单设为保留，非报告设为排除。\n\n这会覆盖这些图片当前的人工选择，之后仍可继续人工修改或撤销本次AI筛选。是否继续？`)) return;
  try {
    const result = await api(`/api/tasks/${taskId}/classify`, { method: "POST", body: { confirmed: true, keyword_id: state.activeKeywordId || null } });
    notice(`已开始AI筛选 ${result.image_count} 张图片`, "success", 5000); startAiPolling(taskId);
  } catch (error) { notice(error.message, "error"); }
}

async function undoAiScreen() {
  const taskId = document.getElementById("review-task").value;
  if (!taskId) return notice("请先选择任务", "error");
  if (!window.confirm("将恢复最近一次AI筛选前的选择状态。之后的人工修改也会被这次撤销覆盖，是否继续？")) return;
  try { const result = await api(`/api/tasks/${taskId}/undo-ai-screen`, { method: "POST" }); await loadImages(); notice(`已恢复 ${result.restored} 张图片`, "success"); }
  catch (error) { notice(error.message, "error"); }
}

function openModal(index) {
  state.modalIndex = index; const image = state.images[index]; if (!image) return;
  document.getElementById("modal-image").src = image.image_url;
  document.getElementById("modal-meta").innerHTML = `<h3>${escapeHtml(image.keyword)}</h3><p>${escapeHtml(image.note_title || "无标题")}</p><p>来源笔记：${escapeHtml(image.platform_note_id)}<br>AI判断：${reportVerdict(image)}${image.ai_confidence != null ? `（${Math.round(image.ai_confidence * 100)}%）` : ""}<br>${escapeHtml(image.ai_reason || "")}</p>`;
  document.getElementById("modal-selected").checked = image.selected;
  document.getElementById("image-modal").classList.remove("hidden");
}
function closeModal() { document.getElementById("image-modal").classList.add("hidden"); }
async function saveModal() {
  const image = state.images[state.modalIndex]; if (!image) return;
  try {
    await api(`/api/images/${image.id}`, { method: "PATCH", body: { selected: document.getElementById("modal-selected").checked } });
    image.selected = document.getElementById("modal-selected").checked;
    const next = state.modalIndex + 1; if (next < state.images.length) openModal(next); else { closeModal(); await loadImages(); }
  } catch (error) { notice(error.message, "error"); }
}

async function exportSelected() {
  const taskId = document.getElementById("review-task").value;
  if (!taskId) return notice("请先选择任务", "error");
  const selectedImages = state.allImages.filter(image => image.selected);
  const count = selectedImages.length;
  if (!count) return notice("当前没有选中的图片", "error");
  const counts = new Map();
  selectedImages.forEach(image => counts.set(image.keyword, (counts.get(image.keyword) || 0) + 1));
  const summary = [...counts.entries()].map(([keyword, imageCount]) => `${keyword}：${imageCount} 张`).join("\n");
  if (!window.confirm(`将一键导出 ${count} 张保留图片，并按检索词分文件夹：\n\n${summary}\n\n请确认你已检查图片中的个人信息，并对后续保存和使用负责。`)) return;
  try {
    const result = await api(`/api/tasks/${taskId}/export`, { method: "POST", body: { confirmed: true } });
    notice(`已生成 ${result.image_count} 张图片的ZIP，下载将在2小时后失效`, "success", 8000);
    window.location.href = result.download_url;
  } catch (error) { notice(error.message, "error"); }
}

function toggleModelFields() {
  const openai = document.getElementById("model-provider").value === "openai_compatible";
  document.getElementById("openai-fields").classList.toggle("hidden", !openai);
}

async function loadSettings() {
  try {
    const data = await api("/api/settings");
    const canManage = Boolean(data.model_can_manage);
    const badge = document.getElementById("key-state");
    badge.textContent = data.model_configured ? "已配置" : "未配置";
    badge.className = `badge ${data.model_configured ? "review" : "paused"}`;
    document.getElementById("model-scope-note").textContent = canManage
      ? "此处保存的模型账号供所有用户进行AI筛选，普通用户无法查看或修改API Key。"
      : "视觉模型由管理员统一管理；你可以使用AI筛选，但无法查看或修改模型账号。";
    document.getElementById("model-admin-controls").classList.toggle("hidden", !canManage);
    document.getElementById("model-user-summary").classList.toggle("hidden", canManage);
    const providerNames = { minimax_token_plan: "MiniMax Token Plan", openai_compatible: "OpenAI兼容视觉模型" };
    document.getElementById("model-shared-provider").textContent = data.model_configured ? (providerNames[data.provider] || "已配置") : "尚未配置";
    document.getElementById("model-shared-status").textContent = data.last_test_message || "";
    if (data.provider) document.getElementById("model-provider").value = data.provider;
    document.getElementById("model-base-url").value = data.base_url || "";
    document.getElementById("model-name").value = data.model_name || "";
    document.getElementById("model-key-hint").textContent = data.key_hint ? `已保存：${data.key_hint}` : "尚未保存密钥";
    document.getElementById("model-test-state").textContent = data.last_test_message || "";
    toggleModelFields();
  } catch (error) { notice(error.message, "error"); }
}

async function saveModel() {
  const provider = document.getElementById("model-provider").value;
  const payload = {
    provider,
    api_key: document.getElementById("model-api-key").value.trim() || null,
    base_url: provider === "openai_compatible" ? document.getElementById("model-base-url").value.trim() : null,
    model_name: provider === "openai_compatible" ? document.getElementById("model-name").value.trim() : null,
  };
  try {
    await api("/api/settings/model", { method: "PUT", body: payload });
    document.getElementById("model-api-key").value = "";
    await loadSettings(); notice("全站视觉模型配置已加密保存", "success");
  } catch (error) { notice(error.message, "error", 8000); }
}

function adminUserCard(user) {
  const own = state.user && state.user.id === user.id;
  const status = user.active ? "启用" : "停用";
  return `<article class="task-card" data-user-id="${user.id}"><div class="task-main"><div class="task-title-row"><strong>${escapeHtml(user.display_name)}</strong><span class="badge ${user.active ? "review" : "paused"}">${status}</span></div><div class="task-meta"><span>@${escapeHtml(user.username || "未设置")}</span><span>${user.role === "admin" ? "管理员" : "普通用户"}</span><span>模型：${user.model_configured ? "已配置" : "未配置"}</span><span>上次登录：${formatDate(user.last_login_at)}</span></div></div><div class="task-actions">${own ? "" : `<button class="secondary" data-user-action="toggle">${user.active ? "停用" : "启用"}</button><button class="secondary" data-user-action="reset">重置密码</button>`}<button class="secondary" data-user-action="clear-browser">清除小红书登录</button></div></article>`;
}

async function loadAdminUsers() {
  if (!state.user || state.user.role !== "admin") return;
  try {
    const users = await api("/api/admin/users");
    const container = document.getElementById("admin-users");
    container.innerHTML = users.length ? users.map(adminUserCard).join("") : `<div class="empty-state">暂无账号</div>`;
    container.querySelectorAll("[data-user-action]").forEach(button => button.addEventListener("click", async () => {
      const card = button.closest("[data-user-id]"); const user = users.find(item => item.id === card.dataset.userId);
      try {
        if (button.dataset.userAction === "toggle") await api(`/api/admin/users/${user.id}`, { method: "PATCH", body: { active: !user.active } });
        if (button.dataset.userAction === "reset") {
          if (!confirm(`确定重置 ${user.display_name} 的密码？该用户当前登录会失效。`)) return;
          const result = await api(`/api/admin/users/${user.id}/reset-password`, { method: "POST" });
          document.getElementById("new-user-result").className = "boundary-note credential-result";
          document.getElementById("new-user-result").innerHTML = `账号：<strong>${escapeHtml(user.username)}</strong><br>新初始密码：<strong>${escapeHtml(result.temporary_password)}</strong><br>请立即复制给用户，本页刷新后不再显示。`;
        }
        if (button.dataset.userAction === "clear-browser") {
          if (!confirm(`确定清除 ${user.display_name} 的小红书登录状态？`)) return;
          await api(`/api/admin/users/${user.id}/browser-profile`, { method: "DELETE" });
        }
        await loadAdminUsers(); notice("账号操作已完成", "success");
      } catch (error) { notice(error.message, "error", 8000); }
    }));
  } catch (error) { notice(error.message, "error"); }
}

function adminQueueCard(task) {
  const actions = [];
  if (task.status === "queued") actions.push(`<button class="secondary" data-queue-action="move_up">↑ 上移</button><button class="secondary" data-queue-action="move_down">↓ 下移</button>`);
  if (["queued","running","classifying","needs_attention"].includes(task.status)) actions.push(`<button class="secondary" data-queue-action="pause">暂停</button>`);
  if (["paused","needs_attention","failed"].includes(task.status)) actions.push(`<button class="secondary" data-queue-action="resume">恢复</button>`);
  if (!['cancelled','exported'].includes(task.status)) actions.push(`<button class="secondary" data-queue-action="cancel">取消</button>`);
  return `<article class="task-card" data-queue-task-id="${task.id}"><div class="task-main"><div class="task-title-row"><strong>${escapeHtml(task.name)}</strong><span class="badge ${task.status}">${statusNames[task.status] || task.status}</span></div><div class="task-meta"><span>${escapeHtml(task.owner_display_name)} · @${escapeHtml(task.owner_username || "未设置")}</span><span>队列位置 ${task.queue_position}</span><span>创建于 ${formatDate(task.created_at)}</span></div><div class="task-progress">${escapeHtml(task.attention_reason || task.progress_message || "等待执行")}</div></div><div class="task-actions">${actions.join("")}</div></article>`;
}

async function loadAdminQueue() {
  if (!state.user || state.user.role !== "admin") return;
  try {
    const tasks = await api("/api/admin/queue");
    const container = document.getElementById("admin-queue");
    container.innerHTML = tasks.length ? tasks.map(adminQueueCard).join("") : `<div class="empty-state">当前没有等待或运行中的任务</div>`;
    container.querySelectorAll("[data-queue-action]").forEach(button => button.addEventListener("click", async () => {
      const taskId = button.closest("[data-queue-task-id]").dataset.queueTaskId;
      try {
        await api(`/api/admin/queue/${taskId}/action`, { method: "POST", body: { action: button.dataset.queueAction } });
        await loadAdminQueue(); notice("全局队列已更新", "success");
      } catch (error) { notice(error.message, "error"); }
    }));
  } catch (error) { notice(error.message, "error"); }
}

async function createAdminUser(event) {
  event.preventDefault();
  try {
    const result = await api("/api/admin/users", { method: "POST", body: { username: document.getElementById("new-username").value.trim(), display_name: document.getElementById("new-display-name").value.trim() } });
    const box = document.getElementById("new-user-result"); box.className = "boundary-note credential-result";
    box.innerHTML = `账号：<strong>${escapeHtml(result.username)}</strong><br>初始密码：<strong>${escapeHtml(result.temporary_password)}</strong><br>请立即复制给用户，本页刷新后不再显示。`;
    event.target.reset(); await loadAdminUsers(); notice("独立账号已创建", "success");
  } catch (error) { notice(error.message, "error", 8000); }
}

function initEvents() {
  document.getElementById("main-nav").addEventListener("click", e => { const item = e.target.closest("[data-page]"); if (item) showPage(item.dataset.page); });
  document.querySelectorAll("[data-goto]").forEach(button => button.addEventListener("click", () => showPage(button.dataset.goto)));
  document.getElementById("apply-template").addEventListener("click", applyTemplate);
  document.getElementById("search-template").addEventListener("change", () => { document.getElementById("template-description").textContent = searchTemplates[document.getElementById("search-template").value].description; });
  document.getElementById("add-keyword").addEventListener("click", () => { if (document.querySelectorAll(".keyword-row").length >= 10) return notice("最多10个关键词", "error"); keywordRow(); });
  document.getElementById("task-form").addEventListener("submit", createTask);
  document.getElementById("refresh-tasks").addEventListener("click", loadTasks);
  document.getElementById("browser-open").addEventListener("click", openBrowser);
  document.getElementById("browser-close").addEventListener("click", closeBrowser);
  document.getElementById("browser-image").addEventListener("click", async event => { const rect = event.target.getBoundingClientRect(); try { await api("/api/browser/click", { method: "POST", body: { x: (event.clientX - rect.left) / rect.width, y: (event.clientY - rect.top) / rect.height } }); } catch (error) { notice(error.message, "error"); } });
  document.getElementById("browser-send").addEventListener("click", async () => { const input = document.getElementById("browser-text"); if (!input.value) return; try { await api("/api/browser/type", { method: "POST", body: { text: input.value } }); input.value = ""; } catch (error) { notice(error.message, "error"); } });
  document.getElementById("browser-text").addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); document.getElementById("browser-send").click(); } });
  document.querySelectorAll(".key-btn[data-key]").forEach(button => button.addEventListener("click", async () => { try { await api("/api/browser/key", { method: "POST", body: { key: button.dataset.key } }); } catch (error) { notice(error.message, "error"); } }));
  document.getElementById("scroll-up").addEventListener("click", () => api("/api/browser/scroll", { method: "POST", body: { delta_y: -650 } }).catch(e => notice(e.message,"error")));
  document.getElementById("scroll-down").addEventListener("click", () => api("/api/browser/scroll", { method: "POST", body: { delta_y: 650 } }).catch(e => notice(e.message,"error")));
  document.getElementById("clear-profile").addEventListener("click", async () => { if (!confirm("确定清除小红书登录状态？清除后需要重新登录。")) return; try { await api("/api/browser/profile", { method: "DELETE" }); stopBrowserPolling(); document.getElementById("browser-screen").classList.add("empty"); await refreshBrowserStatus(); notice("登录状态已清除", "success"); } catch (error) { notice(error.message,"error"); } });
  document.getElementById("review-task").addEventListener("change", () => { state.activeKeywordId = ""; loadImages(); });
  document.getElementById("review-view").addEventListener("change", renderImages); document.getElementById("review-note-filter").addEventListener("input", renderImages); document.getElementById("review-size").addEventListener("change", renderImages);
  document.getElementById("ai-classify").addEventListener("click", runAiClassification);
  document.getElementById("undo-ai-screen").addEventListener("click", undoAiScreen);
  document.getElementById("select-all").addEventListener("click", () => bulkSelect(true)); document.getElementById("select-none").addEventListener("click", () => bulkSelect(false)); document.getElementById("invert-select").addEventListener("click", () => bulkSelect(null,true));
  document.getElementById("export-selected").addEventListener("click", exportSelected);
  document.getElementById("modal-close").addEventListener("click", closeModal); document.querySelector(".modal-backdrop").addEventListener("click", closeModal); document.getElementById("modal-save").addEventListener("click", saveModal);
  document.getElementById("model-provider").addEventListener("change", toggleModelFields);
  document.getElementById("save-model").addEventListener("click", saveModel);
  document.getElementById("test-model").addEventListener("click", async () => { try { notice("正在发送内置测试图片…", "info"); const result = await api("/api/settings/model/test", { method: "POST" }); await loadSettings(); notice(result.message, "success"); } catch (error) { await loadSettings(); notice(error.message, "error", 10000); } });
  document.getElementById("delete-model").addEventListener("click", async () => { if (!confirm("确定删除全站视觉模型配置？所有用户之后都只能人工审核。")) return; try { await api("/api/settings/model", { method: "DELETE" }); await loadSettings(); notice("全站模型配置已删除", "success"); } catch (error) { notice(error.message,"error"); } });
  document.getElementById("change-password").addEventListener("click", async () => { const current = document.getElementById("current-password"); const password = document.getElementById("new-password"); const confirmation = document.getElementById("confirm-password"); try { await api("/api/auth/change-password", { method: "POST", body: { current_password: current.value, new_password: password.value, confirmation: confirmation.value } }); current.value = ""; password.value = ""; confirmation.value = ""; notice("密码已修改，其他设备已退出", "success"); } catch (error) { notice(error.message, "error"); } });
  document.getElementById("user-form").addEventListener("submit", createAdminUser);
  document.getElementById("refresh-users").addEventListener("click", loadAdminUsers);
  document.getElementById("refresh-admin-queue").addEventListener("click", loadAdminQueue);
  document.getElementById("run-cleanup").addEventListener("click", async () => { try { const data = await api("/api/maintenance/cleanup", { method: "POST" }); notice(`清理完成：${data.images} 张图片，${data.exports} 个导出文件`, "success"); } catch (error) { notice(error.message,"error"); } });
}

async function init() {
  initEvents();
  const today = new Date(); const start = new Date(today); start.setDate(today.getDate() - 30);
  const localDate = value => `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2,"0")}-${String(value.getDate()).padStart(2,"0")}`;
  const todayValue = localDate(today);
  document.getElementById("end-date").value = todayValue;
  document.getElementById("end-date").max = todayValue;
  document.getElementById("start-date").value = localDate(start);
  document.getElementById("start-date").max = todayValue;
  applyTemplate();
  try { const user = await api("/api/me"); state.user = user; document.getElementById("user-name").textContent = user.display_name; document.getElementById("user-avatar").textContent = user.display_name.slice(0,1); document.getElementById("admin-nav").classList.toggle("hidden", user.role !== "admin"); }
  catch (error) { notice(error.message,"error",10000); }
  await loadDashboard();
  startStatusPolling();
}

document.addEventListener("DOMContentLoaded", init);
