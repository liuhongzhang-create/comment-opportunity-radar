const Core = window.RadarCore;

const state = {
  headers: [],
  rows: [],
  results: [],
  config: null,
  identityColumn: -1,
  running: false,
};

const $ = (id) => document.getElementById(id);
const apiKey = $("api-key");
const fileInput = $("file-input");
const textColumn = $("text-column");
const analyzeButton = $("analyze-button");
const exportButton = $("export-button");
const retryButton = $("retry-button");
const dropzone = $("dropzone");

const intentLabels = {
  purchase: "购买咨询",
  objection: "成交顾虑",
  content_request: "内容需求",
  complaint: "投诉",
  casual: "普通互动",
  unknown: "未知",
};

const SETTINGS_KEY = "radar.settings.v2";
const IDENTITY_PATTERN = /昵称|用户名|用户|作者|账号|达人|nickname|username|user|author|name/i;
// 用于表格展示：优先显示昵称，纯 ID 对人不友好。导出不受影响，导出始终带全部列。
const DISPLAY_NAME_PATTERN = /昵称|用户名|昵称|nickname/i;

const THRESHOLD_FIELDS = [
  ["review_confidence", "thr-review"],
  ["high_purchase_score", "thr-high-score"],
  ["high_urgent", "thr-high-urgent"],
  ["medium_purchase_score", "thr-med-score"],
  ["medium_urgent", "thr-med-urgent"],
];

let renderTimer = null;
let tickTimer = null;

function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => {
    toast.hidden = true;
  }, 4200);
}

function escapeHtml(text) {
  return String(text).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

const percent = Core.percent;

function clamp(value, low, high) {
  const number = Number(value);
  if (!isFinite(number)) return low;
  return Math.min(high, Math.max(low, number));
}

// --------------------------------------------------------------------------
// Settings
// --------------------------------------------------------------------------

function loadStoredSettings() {
  try {
    return JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") || {};
  } catch (error) {
    return {};
  }
}

function readSettingsFromForm() {
  const thresholds = {};
  THRESHOLD_FIELDS.forEach(([key, id]) => {
    thresholds[key] = Number($(id).value);
  });
  return {
    model: $("model-input").value.trim(),
    concurrency: Number($("concurrency-input").value),
    thresholds,
  };
}

function writeSettingsToForm(settings) {
  const bounds = (state.config && state.config.thresholdBounds) || {};
  THRESHOLD_FIELDS.forEach(([key, id]) => {
    const fallback = state.config.thresholds[key];
    const value = settings.thresholds && settings.thresholds[key] !== undefined ? settings.thresholds[key] : fallback;
    $(id).value = String(value);
    const range = bounds[key];
    if (range) {
      $(id).min = String(range[0]);
      $(id).max = String(range[1]);
    }
  });
  $("model-input").value = settings.model || state.config.model;
  $("concurrency-input").value = String(settings.concurrency || state.config.concurrency);
  $("concurrency-input").max = String(state.config.maxConcurrency);
}

function saveSettings() {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(readSettingsFromForm()));
  } catch (error) {
    /* private mode: settings simply do not persist */
  }
}

function resetSettings() {
  try {
    localStorage.removeItem(SETTINGS_KEY);
  } catch (error) {
    /* ignore */
  }
  writeSettingsToForm({});
  showToast("已恢复默认阈值");
}

async function loadConfig() {
  const response = await fetch("/api/config");
  state.config = await response.json();
  writeSettingsToForm(loadStoredSettings());
  $("endpoint-note").textContent = `上游 ${state.config.apiBase}${state.config.proxy ? ` · 代理 ${state.config.proxy}` : ""}`;
  $("max-rows-hint").textContent = `单次最多分析 ${state.config.maxRows} 条`;
}

// --------------------------------------------------------------------------
// Dataset
// --------------------------------------------------------------------------

function detectIdentityColumn(headers) {
  const displayName = headers.findIndex((header) => DISPLAY_NAME_PATTERN.test(header));
  if (displayName >= 0) return displayName;
  return headers.findIndex((header) => IDENTITY_PATTERN.test(header));
}

function updateAnalyzeState() {
  const selected = getSelectedRows();
  analyzeButton.disabled = state.running || douyin.busy || !apiKey.value.trim() || selected.length === 0;
  $("row-count").textContent = selected.length;
}

function getSelectedRows() {
  if (!state.rows.length) return [];
  return Core.getSelectedRows(state.rows, Number(textColumn.value || 0), state.config.maxRows);
}

function setDataset(matrix, filename) {
  if (matrix.length < 2) throw new Error("CSV 至少需要一行列名和一行内容");
  state.headers = matrix[0].map((value, index) => value.trim() || `列${index + 1}`);
  state.rows = matrix.slice(1);
  state.results = [];
  textColumn.innerHTML = state.headers
    .map((header, index) => `<option value="${index}">${escapeHtml(header)}</option>`)
    .join("");
  const likely = state.headers.findIndex((header) => /评论|内容|文本|comment|content|text/i.test(header));
  textColumn.value = String(likely >= 0 ? likely : 0);
  state.identityColumn = detectIdentityColumn(state.headers);

  $("file-controls").hidden = false;
  $("drop-title").textContent = filename;
  $("drop-meta").textContent = `${state.rows.length} 行数据 · 已识别 ${state.headers.length} 列`;
  $("identity-note").textContent =
    state.identityColumn >= 0
      ? `导出时会一并带上「${state.headers[state.identityColumn]}」等全部原始列`
      : "未找到用户身份列：导出时仍会带上全部原始列";

  $("summary-grid").hidden = true;
  $("table-shell").hidden = true;
  $("empty-state").hidden = false;
  exportButton.hidden = true;
  retryButton.hidden = true;
  $("progress-wrap").hidden = true;
  $("status-text").textContent = "等待开始分析";
  updateAnalyzeState();
}

async function loadFile(file) {
  if (!file || !file.name.toLowerCase().endsWith(".csv")) throw new Error("请选择 CSV 文件");
  const buffer = await file.arrayBuffer();
  setDataset(Core.parseCsv(Core.decodeCsvBuffer(buffer)), file.name);
}

// --------------------------------------------------------------------------
// Analysis
// --------------------------------------------------------------------------

function upsertResult(item) {
  const at = state.results.findIndex((existing) => existing.source_index === item.source_index);
  if (at >= 0) state.results[at] = item;
  else state.results.push(item);
}

function scheduleRender() {
  if (renderTimer) return;
  renderTimer = setTimeout(() => {
    renderTimer = null;
    renderResults();
  }, 200);
}

function setProgress(done, total, label) {
  const wrap = $("progress-wrap");
  wrap.hidden = false;
  $("progress-bar").style.width = total ? `${Math.round((done / total) * 100)}%` : "0%";
  $("progress-label").textContent = label;
}

function handleStreamEvent(event) {
  if (event.type === "start") {
    state.pendingTotal = event.total;
    state.pendingDone = 0;
    if (event.model) $("model-in-use").textContent = `实际使用 ${event.model}`;
    setProgress(0, event.total, `0 / ${event.total}`);
    return;
  }
  if (event.type === "result") {
    upsertResult(event.result);
    state.pendingDone += 1;
    setProgress(state.pendingDone, state.pendingTotal, `${state.pendingDone} / ${state.pendingTotal}`);
    scheduleRender();
    return;
  }
  if (event.type === "done") {
    state.streamDone = true;
    return;
  }
  if (event.type === "error") {
    throw new Error(event.error || "分析中断");
  }
}

async function consumeStream(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    let newline = buffer.indexOf("\n");
    while (newline >= 0) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (line) handleStreamEvent(JSON.parse(line));
      newline = buffer.indexOf("\n");
    }
  }
  if (buffer.trim()) handleStreamEvent(JSON.parse(buffer.trim()));
}

async function runAnalysis(rows) {
  const options = readSettingsFromForm();
  saveSettings();
  const response = await fetch("/api/analyze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ apiKey: apiKey.value.trim(), rows, options, stream: true }),
  });

  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error || `请求失败（HTTP ${response.status}）`);
  }

  if (!response.body || !response.body.getReader) {
    const payload = await response.json();
    payload.results.forEach(upsertResult);
    return payload.results.length;
  }

  await consumeStream(response);
  return rows.length;
}

async function analyze(rows) {
  if (state.running) return;
  const selected = rows || getSelectedRows();
  if (!apiKey.value.trim() || !selected.length) return;

  state.running = true;
  state.streamDone = false;
  state.pendingTotal = selected.length;
  state.pendingDone = 0;
  if (!rows) state.results = [];
  analyzeButton.disabled = true;
  analyzeButton.querySelector("span:first-child").textContent = "分析中…";
  retryButton.hidden = true;
  $("status-text").textContent = `正在处理 ${selected.length} 条评论`;
  $("empty-state").hidden = true;
  $("table-shell").hidden = false;
  setProgress(0, selected.length, `0 / ${selected.length}`);

  const startedAt = Date.now();
  clearInterval(tickTimer);
  tickTimer = setInterval(() => {
    if (!state.running) return;
    const seconds = Math.round((Date.now() - startedAt) / 1000);
    $("elapsed").textContent = `已用时 ${seconds}s`;
  }, 1000);

  try {
    await runAnalysis(selected);
    renderResults();
    const failed = state.results.filter((item) => item.error).length;
    $("status-text").textContent = failed ? `完成，${failed} 条失败可重试` : `已分析 ${state.results.length} 条`;
  } catch (error) {
    renderResults();
    showToast(error.message);
    $("status-text").textContent = "分析中断，请检查 Key 或网络";
  } finally {
    state.running = false;
    clearInterval(tickTimer);
    $("elapsed").textContent = "";
    analyzeButton.querySelector("span:first-child").textContent = "开始分析";
    exportButton.hidden = state.results.length === 0;
    retryButton.hidden = !state.results.some((item) => item.error);
    updateAnalyzeState();
  }
}

function retryFailed() {
  const failed = state.results.filter((item) => item.error);
  if (!failed.length) return;
  const rows = failed.map((item) => ({ text: item.text, source_index: item.source_index }));
  $("progress-wrap").hidden = false;
  analyze(rows);
}

// --------------------------------------------------------------------------
// Douyin collection
// --------------------------------------------------------------------------

const douyin = {
  status: null,
  works: [],
  selected: new Set(),
  busy: false,
  pollTimer: null,
};

function setDouyinStatus(text, kind) {
  const node = $("douyin-status");
  node.textContent = text;
  node.className = `douyin-status${kind ? ` ${kind}` : ""}`;
}

function setBusy(busy) {
  douyin.busy = busy;
  $("douyin-login").disabled = busy;
  $("douyin-refresh").disabled = busy || !(douyin.status && douyin.status.loggedIn);
  $("douyin-collect").disabled = busy || douyin.selected.size === 0;
  updateAnalyzeState();
}

function renderDouyinStatus() {
  const status = douyin.status;
  if (!status) return;
  if (!status.available) {
    setDouyinStatus("抓取不可用", "bad");
    $("douyin-account").textContent = "";
    const hint = $("douyin-setup-hint");
    hint.hidden = false;
    hint.textContent = `${status.error || "缺少运行环境"} —— 在项目目录执行 bash tools/setup-collector.sh，然后用 .venv/bin/python app.py 重新启动服务。`;
    $("douyin-login").disabled = true;
    return;
  }
  $("douyin-setup-hint").hidden = true;
  if (status.loggedIn) {
    setDouyinStatus("已登录", "ok");
    $("douyin-account").textContent = status.nickname ? `@${status.nickname}` : "抖音账号";
  } else if (status.running) {
    setDouyinStatus("浏览器已打开，等待扫码", "busy");
    $("douyin-account").textContent = "";
  } else {
    setDouyinStatus("未登录", "");
    $("douyin-account").textContent = "";
  }
  $("douyin-shutdown").hidden = !status.running;
  $("douyin-login").disabled = douyin.busy;
  $("douyin-refresh").disabled = douyin.busy || !status.loggedIn;
}

async function loadDouyinStatus(refresh) {
  try {
    const response = await fetch(`/api/douyin/status${refresh ? "?refresh=1" : ""}`);
    douyin.status = await response.json();
  } catch (error) {
    douyin.status = { available: false, error: "无法连接本地服务", loggedIn: false };
  }
  renderDouyinStatus();
  return douyin.status;
}

function stopLoginPolling() {
  if (douyin.pollTimer) {
    clearInterval(douyin.pollTimer);
    douyin.pollTimer = null;
  }
}

function pollLoginThenLoadWorks() {
  stopLoginPolling();
  let attempts = 0;
  douyin.pollTimer = setInterval(async () => {
    attempts += 1;
    const status = await loadDouyinStatus(true);
    if (status.loggedIn) {
      stopLoginPolling();
      setDouyinStatus("登录成功", "ok");
      showToast("抖音登录成功，正在读取作品列表…");
      loadWorks().catch((error) => showToast(error.message));
      return;
    }
    if (attempts > 80) {
      stopLoginPolling();
      setDouyinStatus("等待扫码超时", "bad");
    }
  }, 3000);
}

async function openDouyinLogin() {
  if (douyin.busy) return;
  setBusy(true);
  setDouyinStatus("正在打开浏览器…", "busy");
  try {
    const response = await fetch("/api/douyin/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const payload = await response.json();
    douyin.status = payload.status || douyin.status;
    renderDouyinStatus();
    if (!payload.ok) throw new Error(payload.error || "无法打开浏览器");
    if (payload.loggedIn) {
      showToast("已经登录过了，直接读取作品列表");
      await loadWorks();
    } else {
      setDouyinStatus("请在浏览器窗口扫码登录", "busy");
      showToast("已打开浏览器，请用抖音 App 扫码登录");
      pollLoginThenLoadWorks();
    }
  } catch (error) {
    setDouyinStatus("打开浏览器失败", "bad");
    showToast(error.message);
  } finally {
    setBusy(false);
    renderDouyinStatus();
  }
}

async function shutdownDouyinBrowser() {
  stopLoginPolling();
  try {
    await fetch("/api/douyin/shutdown", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) });
    douyin.works = [];
    douyin.selected.clear();
    renderWorks();
    await loadDouyinStatus(false);
    showToast("已关闭抓取用的浏览器");
  } catch (error) {
    showToast(error.message);
  }
}

async function loadWorks() {
  if (douyin.busy) return;
  setBusy(true);
  setDouyinStatus("正在读取作品列表…", "busy");
  try {
    const response = await fetch("/api/douyin/works?limit=60");
    const payload = await response.json();
    if (!payload.ok) throw new Error(payload.error || "读取作品列表失败");
    douyin.works = payload.works || [];
    douyin.selected.clear();
    renderWorks();
    setDouyinStatus(`已读取 ${douyin.works.length} 个作品`, "ok");
    if (!douyin.works.length) showToast("这个账号下没有读到公开作品");
  } catch (error) {
    setDouyinStatus("读取作品失败", "bad");
    showToast(error.message);
  } finally {
    setBusy(false);
    renderDouyinStatus();
  }
}

function renderWorks() {
  const shell = $("works-shell");
  const body = $("works-body");
  if (!douyin.works.length) {
    shell.hidden = true;
    body.innerHTML = "";
    $("works-summary").textContent = "";
    setBusy(douyin.busy);
    return;
  }
  shell.hidden = false;
  body.innerHTML = douyin.works
    .map((work) => {
      const checked = douyin.selected.has(work.awemeId) ? " checked" : "";
      const created = work.createdAt ? `<span>${escapeHtml(work.createdAt)}</span>` : "";
      return `<label class="work-item">
        <input type="checkbox" value="${escapeHtml(work.awemeId)}"${checked} />
        <span>
          <span class="work-title">${escapeHtml(work.title)}</span>
          <span class="work-meta">${created}<span>评论 ${Number(work.commentCount || 0)}</span></span>
        </span>
        <span class="work-badge">${escapeHtml(work.awemeId.slice(-6))}</span>
      </label>`;
    })
    .join("");
  const total = douyin.works.reduce((sum, work) => sum + Number(work.commentCount || 0), 0);
  $("works-summary").textContent = `共 ${douyin.works.length} 个作品 · 评论区合计约 ${total} 条`;
  $("works-select-all").checked = douyin.selected.size === douyin.works.length;
  setBusy(douyin.busy);
}

async function collectDouyin() {
  if (douyin.busy || !douyin.selected.size) return;
  const awemeIds = Array.from(douyin.selected);
  const works = douyin.works
    .filter((work) => douyin.selected.has(work.awemeId))
    .map((work) => ({ aweme_id: work.awemeId, desc: work.desc }));
  const maxComments = clamp(Number($("douyin-max").value), 20, 2000);
  const includeReplies = $("douyin-replies").checked;

  setBusy(true);
  $("douyin-collect").querySelector("span:first-child").textContent = "抓取中…";
  $("empty-state").hidden = true;
  $("table-shell").hidden = false;
  $("status-text").textContent = "正在抓取抖音评论";
  $("douyin-note").textContent = "正在打开视频页并滚动评论区，请勿关闭浏览器窗口…";
  setProgress(0, awemeIds.length, `0 / ${awemeIds.length} 个作品`);

  let finished = null;
  try {
    const response = await fetch("/api/douyin/collect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ awemeIds, works, maxComments, includeReplies, stream: true }),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || `抓取失败（HTTP ${response.status}）`);
    }
    await consumeStreamWith(response, (event) => {
      if (event.type === "start") {
        setDouyinStatus(`准备抓取 ${event.works} 个作品`, "busy");
      } else if (event.type === "work") {
        setProgress(event.index, awemeIds.length, `第 ${event.index + 1} / ${awemeIds.length} 个作品`);
      } else if (event.type === "progress") {
        $("douyin-note").textContent = `已抓取 ${event.count} 条 · 当前作品：${(event.title || "").slice(0, 24)}`;
      } else if (event.type === "note") {
        $("douyin-note").textContent = `已跳过 ${event.skippedNoText} 条纯图片评论（没有文字，无法分析）`;
      } else if (event.type === "error") {
        showToast(event.error);
      } else if (event.type === "done") {
        finished = event;
      }
    });
  } catch (error) {
    $("douyin-note").textContent = "";
    $("status-text").textContent = "抓取中断";
    showToast(error.message);
    return;
  } finally {
    $("douyin-collect").querySelector("span:first-child").textContent = "开始抓取";
    setBusy(false);
  }

  if (!finished || !finished.rows || !finished.rows.length) {
    $("douyin-note").textContent = "没有抓到可分析的评论。作品可能没有评论，或触发了风控。";
    $("status-text").textContent = "没有抓到评论";
    return;
  }

  const skipped = finished.skippedNoText ? `，跳过 ${finished.skippedNoText} 条纯图片评论` : "";
  setDouyinStatus(`抓取完成 ${finished.count} 条`, "ok");
  setDataset([finished.header, ...finished.rows], `抖音评论 · ${finished.count} 条`);
  // 前端还要按单次上限截断，所以用实际选中条数报数，避免两个数字打架。
  const toAnalyze = getSelectedRows().length;
  const capped = toAnalyze < finished.count ? `，本次送去筛选前 ${toAnalyze} 条（单次上限 ${finished.analyzeLimit}）` : "";
  $("douyin-note").textContent = `已抓取 ${finished.count} 条可分析评论${skipped}${capped}，正在送去筛选…`;
  $("max-rows-hint").textContent = `本次抓到 ${finished.count} 条，单次最多分析 ${finished.analyzeLimit} 条`;
  // 用户要的就是「抓完自动筛选」，所以这里直接接上分析。
  await analyze();
}

async function consumeStreamWith(response, handler) {
  if (!response.body || !response.body.getReader) {
    handler(await response.json());
    return;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    let newline = buffer.indexOf("\n");
    while (newline >= 0) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (line) handler(JSON.parse(line));
      newline = buffer.indexOf("\n");
    }
  }
  if (buffer.trim()) handler(JSON.parse(buffer.trim()));
}

function selectSource(name) {
  const isDouyin = name === "douyin";
  $("source-tab-douyin").classList.toggle("is-active", isDouyin);
  $("source-tab-csv").classList.toggle("is-active", !isDouyin);
  $("source-tab-douyin").setAttribute("aria-selected", String(isDouyin));
  $("source-tab-csv").setAttribute("aria-selected", String(!isDouyin));
  $("douyin-panel").hidden = !isDouyin;
  $("csv-panel").hidden = isDouyin;
}

// --------------------------------------------------------------------------
// Rendering
// --------------------------------------------------------------------------

function identityCell(item) {
  const row = state.rows[item.source_index];
  if (!Array.isArray(row) || state.identityColumn < 0) return "";
  return escapeHtml(row[state.identityColumn] || "");
}

function renderResults() {
  const sorted = Core.sortResults(state.results);
  $("results-body").innerHTML = sorted
    .map((item) => {
      if (item.error) {
        return `<tr><td><span class="badge badge-error">失败</span></td><td class="user-cell">${identityCell(item)}</td><td class="comment-cell">${escapeHtml(item.text)}</td><td colspan="4" class="error-cell">${escapeHtml(item.error)}</td></tr>`;
      }
      const badge = { "高": "high", "中": "medium", "低": "low", "人工复核": "review" }[item.priority] || "low";
      return `<tr>
        <td><span class="badge badge-${badge}">${escapeHtml(item.priority)}</span></td>
        <td class="user-cell">${identityCell(item)}</td>
        <td class="comment-cell">${escapeHtml(item.text)}</td>
        <td>${escapeHtml(intentLabels[item.intent] || item.intent)}</td>
        <td>${Number(item.purchaseScore).toFixed(1)} / 4</td>
        <td>${percent(item.urgentProbability)}</td>
        <td>${percent(item.intentConfidence)}<div class="progress"><i style="width:${percent(item.intentConfidence)}"></i></div></td>
      </tr>`;
    })
    .join("");

  $("high-count").textContent = state.results.filter((item) => item.priority === "高").length;
  $("purchase-count").textContent = state.results.filter((item) => item.intent === "purchase").length;
  $("objection-count").textContent = state.results.filter((item) => item.intent === "objection").length;
  $("review-count").textContent = state.results.filter((item) => item.priority === "人工复核").length;
  $("summary-grid").hidden = false;
  $("empty-state").hidden = true;
  $("table-shell").hidden = false;
  exportButton.hidden = state.results.length === 0;
  $("result-count").textContent = `${state.results.length} 条结果`;
}

// --------------------------------------------------------------------------
// Export
// --------------------------------------------------------------------------

function exportCsv() {
  if (!state.results.length) return;
  const matrix = Core.buildExportMatrix(state.headers, state.rows, state.results, intentLabels);
  const csv = Core.toCsv(matrix);
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `评论商机分析-${new Date().toISOString().slice(0, 10)}.csv`;
  anchor.click();
  URL.revokeObjectURL(url);
}

// --------------------------------------------------------------------------
// API key
// --------------------------------------------------------------------------

async function verifyKey() {
  const key = apiKey.value.trim();
  if (!key) {
    showToast("请先填写 API Key");
    return;
  }
  const status = $("key-status");
  status.textContent = "校验中…";
  status.className = "key-status";
  try {
    const response = await fetch("/api/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ apiKey: key }),
    });
    const payload = await response.json();
    if (payload.ok) {
      const names = (payload.models || []).map((item) => item.name).filter(Boolean);
      status.textContent = names.length ? `Key 可用 · 可用模型 ${names.join(" / ")}` : "Key 可用";
      status.className = "key-status ok";
      showToast("API Key 校验通过");
    } else {
      status.textContent = payload.error || "Key 校验失败";
      status.className = "key-status bad";
    }
  } catch (error) {
    status.textContent = error.message;
    status.className = "key-status bad";
  }
}

// --------------------------------------------------------------------------
// Wiring
// --------------------------------------------------------------------------

$("toggle-key").addEventListener("click", () => {
  const visible = apiKey.type === "text";
  apiKey.type = visible ? "password" : "text";
  $("toggle-key").textContent = visible ? "显示" : "隐藏";
});
apiKey.addEventListener("input", () => {
  updateAnalyzeState();
  $("key-status").textContent = "";
});
textColumn.addEventListener("change", updateAnalyzeState);
fileInput.addEventListener("change", () => loadFile(fileInput.files[0]).catch((error) => showToast(error.message)));
analyzeButton.addEventListener("click", () => analyze());
retryButton.addEventListener("click", retryFailed);
exportButton.addEventListener("click", exportCsv);
$("verify-button").addEventListener("click", verifyKey);
$("reset-settings").addEventListener("click", resetSettings);
THRESHOLD_FIELDS.forEach(([, id]) => $(id).addEventListener("change", saveSettings));

$("sample-button").addEventListener("click", () => {
  setDataset(
    [
      ["用户", "评论"],
      ["小林", "怎么买？可以发一下链接吗"],
      ["阿杰", "看着不错，就是有点担心售后"],
      ["琪琪", "能不能出一期新手完整教程"],
      ["老周", "已经三天没发货了，客服也不回复"],
      ["Moon", "哈哈哈这个演示太真实了"],
    ],
    "示例评论.csv"
  );
  selectSource("csv");
  showToast("已载入 5 条示例评论；填写 API Key 后即可分析");
});

// -- Douyin wiring ----------------------------------------------------------

$("source-tab-douyin").addEventListener("click", () => selectSource("douyin"));
$("source-tab-csv").addEventListener("click", () => selectSource("csv"));
$("douyin-login").addEventListener("click", openDouyinLogin);
$("douyin-refresh").addEventListener("click", () => loadWorks().catch((error) => showToast(error.message)));
$("douyin-shutdown").addEventListener("click", shutdownDouyinBrowser);
$("douyin-collect").addEventListener("click", collectDouyin);

$("works-body").addEventListener("change", (event) => {
  const input = event.target;
  if (!input || input.type !== "checkbox") return;
  if (input.checked) douyin.selected.add(input.value);
  else douyin.selected.delete(input.value);
  $("works-select-all").checked = douyin.selected.size === douyin.works.length;
  $("douyin-collect").disabled = douyin.busy || douyin.selected.size === 0;
  $("douyin-note").textContent = douyin.selected.size
    ? `已选择 ${douyin.selected.size} 个作品`
    : "";
});

$("works-select-all").addEventListener("change", (event) => {
  douyin.selected.clear();
  if (event.target.checked) douyin.works.forEach((work) => douyin.selected.add(work.awemeId));
  renderWorks();
  $("douyin-note").textContent = douyin.selected.size ? `已选择 ${douyin.selected.size} 个作品` : "";
});

window.addEventListener("beforeunload", stopLoginPolling);

["dragenter", "dragover"].forEach((event) =>
  dropzone.addEventListener(event, (e) => {
    e.preventDefault();
    dropzone.classList.add("is-dragging");
  })
);
["dragleave", "drop"].forEach((event) =>
  dropzone.addEventListener(event, (e) => {
    e.preventDefault();
    dropzone.classList.remove("is-dragging");
  })
);
dropzone.addEventListener("drop", (event) => loadFile(event.dataTransfer.files[0]).catch((error) => showToast(error.message)));

loadConfig()
  .then(() => loadDouyinStatus(false))
  .catch(() => showToast("无法读取本地配置，请确认服务已启动"));
