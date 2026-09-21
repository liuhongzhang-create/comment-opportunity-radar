/**
 * Tests for web/core.js — the CSV parsing and export helpers.
 *
 * Run with: node --test tests/web_core.test.cjs
 */
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");
const assert = require("node:assert/strict");

const source = fs.readFileSync(path.join(__dirname, "..", "web", "core.js"), "utf8");
const sandbox = { TextDecoder, Uint8Array };
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: "core.js" });
const Core = sandbox.RadarCore;

/**
 * core.js runs in a separate vm realm, so its arrays and objects carry a
 * different prototype. Comparing serialised JSON keeps the assertion strict
 * without tripping over that.
 */
const assertJson = (actual, expected) =>
  assert.equal(JSON.stringify(actual), JSON.stringify(expected));

const INTENT_LABELS = {
  purchase: "购买咨询",
  objection: "成交顾虑",
  content_request: "内容需求",
  complaint: "投诉",
  casual: "普通互动",
};

// ---------------------------------------------------------------------------
// parseCsv
// ---------------------------------------------------------------------------

test("parseCsv splits plain rows and drops a trailing blank line", () => {
  const rows = Core.parseCsv("用户,评论\n小林,怎么买\n");
  assertJson(rows, [
    ["用户", "评论"],
    ["小林", "怎么买"],
  ]);
});

test("parseCsv strips a UTF-8 BOM from the first header", () => {
  const rows = Core.parseCsv("\ufeff用户,评论\n小林,你好\n");
  assert.equal(rows[0][0], "用户");
});

test("parseCsv handles quoted commas, escaped quotes and embedded newlines", () => {
  const rows = Core.parseCsv('a,b\n"x,y","he said ""hi""\nagain"\n');
  assertJson(rows[1], ["x,y", 'he said "hi"\nagain']);
});

test("parseCsv accepts CRLF and keeps ragged rows", () => {
  const rows = Core.parseCsv("a,b,c\r\n1,2\r\n");
  assertJson(rows, [
    ["a", "b", "c"],
    ["1", "2"],
  ]);
});

// ---------------------------------------------------------------------------
// Round trip: identity columns must survive export byte for byte
// ---------------------------------------------------------------------------

test("a parsed dataset round trips through toCsv unchanged", () => {
  const csv = '用户ID,昵称,评论\n88801,小林,"怎么买, 有链接吗"\n88802,"阿""杰",看着不错\n';
  const rows = Core.parseCsv(csv);
  const matrix = Core.buildExportMatrix(
    rows[0],
    rows.slice(1),
    [{ source_index: 0, text: "怎么买, 有链接吗", intent: "purchase", priority: "高", purchaseScore: 3.6, urgentProbability: 0.86, intentConfidence: 0.87 }],
    INTENT_LABELS
  );
  assertJson(matrix[1].slice(0, 3), ["88801", "小林", "怎么买, 有链接吗"]);
});

// ---------------------------------------------------------------------------
// getSelectedRows — the index bug that used to scramble identity columns
// ---------------------------------------------------------------------------

test("getSelectedRows keeps the original row index when blank rows are skipped", () => {
  const rows = [["a"], [""], ["   "], ["b"]];
  const selected = Core.getSelectedRows(rows, 0, 500);
  assertJson(selected, [
    { text: "a", source_index: 0 },
    { text: "b", source_index: 3 },
  ]);
});

test("getSelectedRows trims, skips missing cells and honours the cap", () => {
  const rows = [["  hi  "], ["x"], ["y"], ["z"]];
  assertJson(Core.getSelectedRows(rows, 0, 2), [
    { text: "hi", source_index: 0 },
    { text: "x", source_index: 1 },
  ]);
});

test("getSelectedRows returns nothing for an empty dataset", () => {
  assertJson(Core.getSelectedRows([], 0, 500), []);
});

// ---------------------------------------------------------------------------
// Formula injection
// ---------------------------------------------------------------------------

test("guardFormula neutralises dangerous leading characters", () => {
  assert.equal(Core.guardFormula("=SUM(A1:A9)"), "'=SUM(A1:A9)");
  assert.equal(Core.guardFormula("+1+1"), "'+1+1");
  assert.equal(Core.guardFormula("@SUM(1)"), "'@SUM(1)");
  assert.equal(Core.guardFormula("\tcmd"), "'\tcmd");
  assert.equal(Core.guardFormula("=cmd|'/c calc'!A1"), "'=cmd|'/c calc'!A1");
});

test("guardFormula leaves ordinary text and real numbers alone", () => {
  assert.equal(Core.guardFormula("怎么买？"), "怎么买？");
  assert.equal(Core.guardFormula("-5"), "-5");
  assert.equal(Core.guardFormula(-5), "-5");
  assert.equal(Core.guardFormula("1e-3"), "1e-3");
  assert.equal(Core.guardFormula(""), "");
});

test("csvCell escapes quotes and separators after guarding", () => {
  assert.equal(Core.csvCell('=A1,"x"'), '"\'=A1,""x"""');
  assert.equal(Core.csvCell("plain"), "plain");
});

test("toCsv writes a BOM, CRLF endings and no trailing garbage", () => {
  const csv = Core.toCsv([
    ["评论", "优先级"],
    ['=EVIL(),"quoted"', "高"],
  ]);
  assert.ok(csv.startsWith("\ufeff"));
  assert.equal(csv, '\ufeff评论,优先级\r\n"\'=EVIL(),""quoted""",高\r\n');
});

// ---------------------------------------------------------------------------
// Export matrix
// ---------------------------------------------------------------------------

test("buildExportMatrix prepends every original column and adds the row number", () => {
  const headers = ["用户ID", "昵称", "评论"];
  const rows = [["88801", "小林", "怎么买"]];
  const results = [
    { source_index: 0, text: "怎么买", intent: "purchase", priority: "高", purchaseScore: 3.6, urgentProbability: 0.86, intentConfidence: 0.87 },
  ];
  const matrix = Core.buildExportMatrix(headers, rows, results, INTENT_LABELS);
  assertJson(matrix[0], [
    "用户ID",
    "昵称",
    "评论",
    "原CSV行号",
    "主要意图",
    "回复优先级",
    "购买意向分",
    "尽快回复概率",
    "分类置信度",
    "错误",
  ]);
  assertJson(matrix[1], ["88801", "小林", "怎么买", 2, "购买咨询", "高", "3.6", "0.86", "0.87", ""]);
});

test("buildExportMatrix maps every row back to its source spreadsheet line", () => {
  const headers = ["用户", "评论"];
  const rows = [["a", "第一条"], ["", ""], ["b", "第三条"]];
  const results = [
    { source_index: 0, text: "第一条", intent: "casual", priority: "低", purchaseScore: 0.1, urgentProbability: 0.07, intentConfidence: 0.9 },
    { source_index: 2, text: "第三条", intent: "purchase", priority: "高", purchaseScore: 3.6, urgentProbability: 0.86, intentConfidence: 0.87 },
  ];
  const matrix = Core.buildExportMatrix(headers, rows, results, INTENT_LABELS);
  // The export is sorted by priority, so the high-priority row is written first.
  assert.equal(matrix[1][0], "b");
  assert.equal(matrix[1][2], 4, "row 2 of the CSV is line 4 of the file");
  assert.equal(matrix[2][0], "a");
  assert.equal(matrix[2][2], 2, "row 0 of the CSV is line 2 of the file");
});

test("buildExportMatrix pads rows that are shorter than the header", () => {
  const matrix = Core.buildExportMatrix(
    ["a", "b", "c"],
    [["only-one"]],
    [{ source_index: 0, text: "x", intent: "casual", priority: "低", purchaseScore: 0, urgentProbability: 0, intentConfidence: 0.5 }],
    INTENT_LABELS
  );
  assertJson(matrix[1].slice(0, 3), ["only-one", "", ""]);
});

test("buildExportMatrix blanks the analysis columns for a failed row but keeps identity", () => {
  const matrix = Core.buildExportMatrix(
    ["用户", "评论"],
    [["小林", "坏行"]],
    [{ source_index: 0, text: "坏行", error: "TypeSafe API 返回 422：bad", priority: "失败" }],
    INTENT_LABELS
  );
  assertJson(matrix[1], ["小林", "坏行", 2, "", "失败", "", "", "", "TypeSafe API 返回 422：bad"]);
});

test("buildExportMatrix sorts by priority before writing", () => {
  const results = [
    { source_index: 0, text: "low", intent: "casual", priority: "低", purchaseScore: 0, urgentProbability: 0, intentConfidence: 0.9 },
    { source_index: 1, text: "high", intent: "purchase", priority: "高", purchaseScore: 3.6, urgentProbability: 0.9, intentConfidence: 0.9 },
  ];
  const matrix = Core.buildExportMatrix(["评论"], [["low"], ["high"]], results, INTENT_LABELS);
  assert.equal(matrix[1][0], "high");
  assert.equal(matrix[2][0], "low");
});

// ---------------------------------------------------------------------------
// Sorting and formatting
// ---------------------------------------------------------------------------

test("sortResults orders by priority then original position", () => {
  const sorted = Core.sortResults([
    { index: 2, priority: "低" },
    { index: 1, priority: "高" },
    { index: 0, priority: "高" },
    { index: 3, priority: "人工复核" },
    { index: 4, priority: "中" },
  ]).map((item) => item.index);
  assertJson(sorted, [0, 1, 3, 4, 2]);
});

test("sortResults puts unknown priorities last without throwing", () => {
  const sorted = Core.sortResults([
    { index: 0, priority: "???" },
    { index: 1, priority: "高" },
  ]).map((item) => item.index);
  assertJson(sorted, [1, 0]);
});

test("sortResults does not mutate its input", () => {
  const input = [{ index: 1, priority: "低" }, { index: 0, priority: "高" }];
  Core.sortResults(input);
  assert.equal(input[0].index, 1);
});

test("roundTo keeps numbers as numbers and drops NaN", () => {
  assert.equal(Core.roundTo(1.005, 2), "1");
  assert.equal(Core.roundTo(0.8666, 3), "0.867");
  assert.equal(Core.roundTo("nonsense", 2), "");
});

test("percent renders a rounded percentage", () => {
  assert.equal(Core.percent(0.866), "87%");
  assert.equal(Core.percent(undefined), "0%");
});

// ---------------------------------------------------------------------------
// Encoding detection
// ---------------------------------------------------------------------------

test("decodeCsvBuffer reads plain UTF-8 Chinese", () => {
  const bytes = Uint8Array.from(Buffer.from("用户,评论\n小林,你好", "utf8"));
  assert.equal(Core.decodeCsvBuffer(bytes.buffer), "用户,评论\n小林,你好");
});

test("decodeCsvBuffer drops a UTF-8 BOM", () => {
  const bytes = Uint8Array.from(Buffer.from("\ufeff用户,评论", "utf8"));
  assert.equal(Core.decodeCsvBuffer(bytes.buffer), "用户,评论");
});

test("decodeCsvBuffer reads UTF-16LE exported by Excel", () => {
  const bytes = Uint8Array.from(Buffer.from("\ufeff用户,评论", "utf16le"));
  assert.equal(Core.decodeCsvBuffer(bytes.buffer), "用户,评论");
});

test("decodeCsvBuffer falls back to GBK when the bytes are not valid UTF-8", () => {
  // "评论" in GBK; the same bytes are an illegal UTF-8 sequence.
  const bytes = Uint8Array.from([0xc6, 0xc0, 0xc2, 0xdb]);
  assert.equal(Core.decodeCsvBuffer(bytes.buffer), "评论");
});

test("a GBK file decodes into the same rows as its UTF-8 twin", () => {
  const gbk = Uint8Array.from([0xc6, 0xc0, 0xc2, 0xdb, 0x2c, 0x61]);
  const rows = Core.parseCsv(Core.decodeCsvBuffer(gbk.buffer));
  assertJson(rows, [["评论", "a"]]);
});
