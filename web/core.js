/**
 * Pure helpers shared by the browser UI and the Node test suite.
 *
 * Loaded as a classic script in the browser; `tests/test_web_core.cjs` reads
 * the same file through `vm` so both sides run identical code.
 */
(function (root) {
  "use strict";

  var FORMULA_START = /^[=+\-@\t\r]/;
  var NUMBER_LIKE = /^-?\d+(\.\d+)?([eE][+-]?\d+)?$/;

  /** Strip a UTF-8 BOM that Excel and Google Sheets like to prepend. */
  function stripBom(text) {
    return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
  }

  /**
   * Decode a CSV file's bytes. Handles UTF-8 BOM, UTF-16 with BOM, and falls
   * back to GBK — the encoding Excel on Chinese Windows still exports by
   * default, which is the usual reason an imported file looks like mojibake.
   */
  function decodeCsvBuffer(buffer) {
    var bytes = new Uint8Array(buffer);
    var Decoder = typeof TextDecoder !== "undefined" ? TextDecoder : null;
    if (!Decoder) return String.fromCharCode.apply(null, bytes.slice(0, 0));

    if (bytes.length >= 3 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) {
      return new Decoder("utf-8").decode(bytes.subarray(3));
    }
    if (bytes.length >= 2 && bytes[0] === 0xff && bytes[1] === 0xfe) {
      return new Decoder("utf-16le").decode(bytes.subarray(2));
    }
    if (bytes.length >= 2 && bytes[0] === 0xfe && bytes[1] === 0xff) {
      return new Decoder("utf-16be").decode(bytes.subarray(2));
    }

    try {
      // fatal:true throws instead of silently inserting U+FFFD, which is what
      // lets us tell a real UTF-8 file from a GBK one.
      return new Decoder("utf-8", { fatal: true }).decode(bytes);
    } catch (error) {
      /* not valid UTF-8 */
    }
    try {
      return new Decoder("gbk").decode(bytes);
    } catch (error) {
      return new Decoder("utf-8").decode(bytes);
    }
  }

  /** Minimal RFC 4180 parser: quoted fields, escaped quotes, embedded newlines. */
  function parseCsv(text) {
    var source = stripBom(String(text));
    var rows = [];
    var row = [];
    var field = "";
    var quoted = false;
    for (var i = 0; i < source.length; i += 1) {
      var char = source[i];
      if (quoted) {
        if (char === '"' && source[i + 1] === '"') {
          field += '"';
          i += 1;
        } else if (char === '"') {
          quoted = false;
        } else {
          field += char;
        }
      } else if (char === '"') {
        quoted = true;
      } else if (char === ",") {
        row.push(field);
        field = "";
      } else if (char === "\n") {
        row.push(field);
        rows.push(row);
        row = [];
        field = "";
      } else if (char !== "\r") {
        field += char;
      }
    }
    if (field || row.length) {
      row.push(field);
      rows.push(row);
    }
    return rows.filter(function (item) {
      return item.some(function (value) {
        return value.trim();
      });
    });
  }

  function csvEscape(value) {
    var text = String(value === null || value === undefined ? "" : value);
    return /[",\n\r]/.test(text) ? '"' + text.replace(/"/g, '""') + '"' : text;
  }

  /**
   * Guard against CSV formula injection: a cell starting with = + - @ or a
   * tab/CR is executed as a formula when the export is opened in Excel.
   * Applied only to text — a real number like -3 must stay a number.
   */
  function guardFormula(value) {
    var text = String(value === null || value === undefined ? "" : value);
    if (!text) return text;
    if (typeof value === "number") return text;
    if (NUMBER_LIKE.test(text)) return text;
    return FORMULA_START.test(text) ? "'" + text : text;
  }

  function csvCell(value) {
    return csvEscape(guardFormula(value));
  }

  /** Serialise a matrix to CSV text with a BOM and CRLF line endings. */
  function toCsv(matrix) {
    var lines = matrix.map(function (row) {
      return row.map(csvCell).join(",");
    });
    return "\ufeff" + lines.join("\r\n") + "\r\n";
  }

  /** Pick the analysed rows while remembering where each one came from. */
  function getSelectedRows(rows, column, limit) {
    var selected = [];
    var cap = typeof limit === "number" ? limit : rows.length;
    for (var i = 0; i < rows.length && selected.length < cap; i += 1) {
      var row = rows[i] || [];
      var text = String(row[column] === undefined ? "" : row[column]).trim();
      if (!text) continue;
      selected.push({ text: text, source_index: i });
    }
    return selected;
  }

  var PRIORITY_RANK = { "高": 0, "人工复核": 1, "中": 2, "低": 3, "失败": 4 };

  function sortResults(results) {
    return results.slice().sort(function (a, b) {
      var diff =
        (PRIORITY_RANK[a.priority] === undefined ? 9 : PRIORITY_RANK[a.priority]) -
        (PRIORITY_RANK[b.priority] === undefined ? 9 : PRIORITY_RANK[b.priority]);
      if (diff) return diff;
      return (a.index || 0) - (b.index || 0);
    });
  }

  function percent(value) {
    return Math.round((Number(value) || 0) * 100) + "%";
  }

  function roundTo(value, digits) {
    var number = Number(value);
    if (!isFinite(number)) return "";
    return Number(number.toFixed(digits === undefined ? 3 : digits)).toString();
  }

  /**
   * Build the export matrix: every original column first (so the nickname, id
   * or profile link of the commenter survives), then the analysis columns.
   */
  function buildExportMatrix(headers, rows, results, intentLabels) {
    var labels = intentLabels || {};
    var matrix = [
      headers.concat([
        "原CSV行号",
        "主要意图",
        "回复优先级",
        "购买意向分",
        "尽快回复概率",
        "分类置信度",
        "错误",
      ]),
    ];
    sortResults(results).forEach(function (item) {
      var source = rows[item.source_index];
      if (!Array.isArray(source)) source = [];
      var cells = headers.map(function (_header, index) {
        return source[index] === undefined ? "" : source[index];
      });
      var failed = Boolean(item.error);
      matrix.push(
        cells.concat([
          (Number(item.source_index) || 0) + 2,
          failed ? "" : labels[item.intent] || item.intent || "",
          item.priority || "",
          failed ? "" : roundTo(item.purchaseScore, 2),
          failed ? "" : roundTo(item.urgentProbability, 3),
          failed ? "" : roundTo(item.intentConfidence, 3),
          item.error || "",
        ])
      );
    });
    return matrix;
  }

  var api = {
    stripBom: stripBom,
    decodeCsvBuffer: decodeCsvBuffer,
    parseCsv: parseCsv,
    csvEscape: csvEscape,
    guardFormula: guardFormula,
    csvCell: csvCell,
    toCsv: toCsv,
    getSelectedRows: getSelectedRows,
    sortResults: sortResults,
    percent: percent,
    roundTo: roundTo,
    buildExportMatrix: buildExportMatrix,
  };

  root.RadarCore = api;
  if (typeof module === "object" && module && module.exports) {
    module.exports = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
