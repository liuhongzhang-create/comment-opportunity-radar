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
const IDENTITY_PATTERN = /昵称|用户|作者|账号|达人|昵称|username|nickname|user|author|name/i;

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
  return headers.findIndex((header) => IDENTITY_PATTERN.test(header));
}

function updateAnalyzeState() {
  const selected = getSelectedRows();
  analyzeButton.disabled = state.running || !apiKey.value.trim() || selected.length === 0;
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
  showToast("已载入 5 条示例评论；填写 API Key 后即可分析");
});

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

loadConfig().catch(() => showToast("无法读取本地配置，请确认服务已启动"));
