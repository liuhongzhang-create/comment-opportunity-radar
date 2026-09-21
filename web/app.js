const state = { headers: [], rows: [], results: [] };

const $ = (id) => document.getElementById(id);
const apiKey = $("api-key");
const fileInput = $("file-input");
const textColumn = $("text-column");
const analyzeButton = $("analyze-button");
const exportButton = $("export-button");
const dropzone = $("dropzone");

const intentLabels = {
  purchase: "购买咨询",
  objection: "成交顾虑",
  content_request: "内容需求",
  complaint: "投诉",
  casual: "普通互动",
  unknown: "未知",
};

function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.hidden = true; }, 3600);
}

function parseCsv(text) {
  const rows = [];
  let row = [], field = "", quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    if (quoted) {
      if (char === '"' && text[i + 1] === '"') { field += '"'; i += 1; }
      else if (char === '"') quoted = false;
      else field += char;
    } else if (char === '"') quoted = true;
    else if (char === ",") { row.push(field); field = ""; }
    else if (char === "\n") { row.push(field); rows.push(row); row = []; field = ""; }
    else if (char !== "\r") field += char;
  }
  if (field || row.length) { row.push(field); rows.push(row); }
  return rows.filter((item) => item.some((value) => value.trim()));
}

function csvEscape(value) {
  const text = String(value ?? "");
  return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function updateAnalyzeState() {
  const validRows = getCommentRows();
  analyzeButton.disabled = !apiKey.value.trim() || validRows.length === 0;
  $("row-count").textContent = validRows.length;
}

function getCommentRows() {
  const column = Number(textColumn.value || 0);
  return state.rows.map((row) => (row[column] || "").trim()).filter(Boolean).slice(0, 500);
}

function setDataset(matrix, filename) {
  if (matrix.length < 2) throw new Error("CSV 至少需要一行列名和一行内容");
  state.headers = matrix[0].map((value, index) => value.trim() || `列${index + 1}`);
  state.rows = matrix.slice(1);
  textColumn.innerHTML = state.headers.map((header, index) => `<option value="${index}">${escapeHtml(header)}</option>`).join("");
  const likely = state.headers.findIndex((header) => /评论|内容|文本|comment|content|text/i.test(header));
  textColumn.value = String(likely >= 0 ? likely : 0);
  $("file-controls").hidden = false;
  $("drop-title").textContent = filename;
  $("drop-meta").textContent = `${state.rows.length} 行数据 · 已识别 ${state.headers.length} 列`;
  updateAnalyzeState();
}

async function loadFile(file) {
  if (!file || !file.name.toLowerCase().endsWith(".csv")) throw new Error("请选择 CSV 文件");
  setDataset(parseCsv(await file.text()), file.name);
}

function escapeHtml(text) {
  return String(text).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function percent(value) { return `${Math.round((Number(value) || 0) * 100)}%`; }

function renderResults() {
  const sorted = [...state.results].sort((a, b) => {
    const rank = { "高": 0, "人工复核": 1, "中": 2, "低": 3, "失败": 4 };
    return (rank[a.priority] ?? 9) - (rank[b.priority] ?? 9) || a.index - b.index;
  });
  $("results-body").innerHTML = sorted.map((item) => {
    if (item.error) return `<tr><td><span class="badge badge-error">失败</span></td><td class="comment-cell">${escapeHtml(item.text)}</td><td colspan="4">${escapeHtml(item.error)}</td></tr>`;
    const badge = { "高": "high", "中": "medium", "低": "low", "人工复核": "review" }[item.priority] || "low";
    return `<tr>
      <td><span class="badge badge-${badge}">${escapeHtml(item.priority)}</span></td>
      <td class="comment-cell">${escapeHtml(item.text)}</td>
      <td>${escapeHtml(intentLabels[item.intent] || item.intent)}</td>
      <td>${Number(item.purchaseScore).toFixed(1)} / 4</td>
      <td>${percent(item.urgentProbability)}</td>
      <td>${percent(item.intentConfidence)}<div class="progress"><i style="width:${percent(item.intentConfidence)}"></i></div></td>
    </tr>`;
  }).join("");
  $("high-count").textContent = state.results.filter((item) => item.priority === "高").length;
  $("purchase-count").textContent = state.results.filter((item) => item.intent === "purchase").length;
  $("objection-count").textContent = state.results.filter((item) => item.intent === "objection").length;
  $("review-count").textContent = state.results.filter((item) => item.priority === "人工复核").length;
  $("summary-grid").hidden = false;
  $("empty-state").hidden = true;
  $("table-shell").hidden = false;
  exportButton.hidden = false;
}

async function analyze() {
  const rows = getCommentRows();
  if (!apiKey.value.trim() || !rows.length) return;
  analyzeButton.disabled = true;
  analyzeButton.querySelector("span:first-child").textContent = "分析中…";
  $("status-text").textContent = `正在处理 ${rows.length} 条评论`;
  try {
    const response = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ apiKey: apiKey.value.trim(), rows }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "分析失败");
    state.results = payload.results;
    renderResults();
    const failed = state.results.filter((item) => item.error).length;
    $("status-text").textContent = failed ? `完成，${failed} 条失败` : `已分析 ${state.results.length} 条`;
  } catch (error) {
    showToast(error.message);
    $("status-text").textContent = "分析失败，请检查 Key 或网络";
  } finally {
    analyzeButton.querySelector("span:first-child").textContent = "开始分析";
    updateAnalyzeState();
  }
}

function exportCsv() {
  const headers = ["评论", "主要意图", "回复优先级", "购买意向分", "尽快回复概率", "分类置信度", "错误"];
  const rows = state.results.map((item) => [item.text, intentLabels[item.intent] || item.intent || "", item.priority, item.purchaseScore ?? "", item.urgentProbability ?? "", item.intentConfidence ?? "", item.error || ""]);
  const csv = `\ufeff${[headers, ...rows].map((row) => row.map(csvEscape).join(",")).join("\n")}`;
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url; anchor.download = `评论商机分析-${new Date().toISOString().slice(0, 10)}.csv`; anchor.click();
  URL.revokeObjectURL(url);
}

$("toggle-key").addEventListener("click", () => {
  const visible = apiKey.type === "text";
  apiKey.type = visible ? "password" : "text";
  $("toggle-key").textContent = visible ? "显示" : "隐藏";
});
apiKey.addEventListener("input", updateAnalyzeState);
textColumn.addEventListener("change", updateAnalyzeState);
fileInput.addEventListener("change", () => loadFile(fileInput.files[0]).catch((error) => showToast(error.message)));
analyzeButton.addEventListener("click", analyze);
exportButton.addEventListener("click", exportCsv);
$("sample-button").addEventListener("click", () => {
  setDataset([
    ["用户", "评论"],
    ["小林", "怎么买？可以发一下链接吗"],
    ["阿杰", "看着不错，就是有点担心售后"],
    ["琪琪", "能不能出一期新手完整教程"],
    ["老周", "已经三天没发货了，客服也不回复"],
    ["Moon", "哈哈哈这个演示太真实了"],
  ], "示例评论.csv");
  showToast("已载入 5 条示例评论；填写 API Key 后即可分析");
});
["dragenter", "dragover"].forEach((event) => dropzone.addEventListener(event, (e) => { e.preventDefault(); dropzone.classList.add("is-dragging"); }));
["dragleave", "drop"].forEach((event) => dropzone.addEventListener(event, (e) => { e.preventDefault(); dropzone.classList.remove("is-dragging"); }));
dropzone.addEventListener("drop", (event) => loadFile(event.dataTransfer.files[0]).catch((error) => showToast(error.message)));
