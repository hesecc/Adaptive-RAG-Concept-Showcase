"use strict";

// Untrusted answers and excerpts enter as text, never as executable markup.
globalThis.RagAnswerVisuals = (() => {
  const make = (tag, text) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const row = line => line.trim().slice(1, -1).split("|").map(cell => cell.trim());
  const pipe = line => /^\s*\|.+\|\s*$/.test(line || "") && !line.includes("\\|");
  const divider = line => pipe(line) && row(line).every(cell => /^:?-{3,}:?$/.test(cell));
  const validTable = (rows, maxRows = 121) => rows.length >= 2 && rows.length <= maxRows &&
    rows[0].length >= 2 && rows[0].length <= 6 && rows[0].every(Boolean) &&
    rows.every(cells => cells.length === rows[0].length && cells.every(cell => cell.length <= 160));
  function tableNode(rows) {
    const wrap = make("div"); wrap.className = "answer-table-scroll";
    wrap.tabIndex = 0; wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", "数据表格");
    const table = make("table"), head = make("thead"), body = make("tbody");
    rows.forEach((cells, i) => {
      const tr = make("tr");
      cells.forEach(value => {
        const cell = make(i ? "td" : "th", value);
        if (!i) cell.setAttribute("scope", "col");
        tr.append(cell);
      });
      (i ? body : head).append(tr);
    });
    table.append(head, body); wrap.append(table); return wrap;
  }
  function inline(node, text) {
    const pattern = /\*\*([^*\r\n]+)\*\*|\x60([^\x60\r\n]+)\x60/g;
    let start = 0;
    for (const match of text.matchAll(pattern)) {
      node.append(document.createTextNode(text.slice(start, match.index)),
        make(match[1] === undefined ? "code" : "strong", match[1] ?? match[2]));
      start = match.index + match[0].length;
    }
    node.append(document.createTextNode(text.slice(start)));
    return node;
  }
  function prose(text) {
    const block = make("div"); block.className = "answer-prose";
    const lines = text.split(/\r?\n/), fence = /^\s*\x60{3}([\w+-]*)\s*$/;
    const heading = /^\s*#{1,6}\s+(\S.*)$/;
    const bullet = /^\s*(?:([-+*])|(\d{1,4})[.)、])\s+(\S.*)$/;
    let paragraph = [];
    const flush = () => {
      if (paragraph.length) block.append(inline(make("p"), paragraph.join("\n")));
      paragraph = [];
    };
    for (let i = 0; i < lines.length;) {
      const line = lines[i], title = heading.exec(line), item = bullet.exec(line);
      const end = fence.test(line) ? lines.findIndex((value, at) => at > i && /^\s*\x60{3}\s*$/.test(value)) : -1;
      if (end > i) {
        flush(); const pre = make("pre"); pre.append(make("code", lines.slice(i + 1, end).join("\n")));
        block.append(pre); i = end + 1;
      } else if (pipe(line) && divider(lines[i + 1]) && row(line).length === row(lines[i + 1]).length) {
        let next = i + 2;
        while (pipe(lines[next])) next++;
        const rows = [row(line), ...lines.slice(i + 2, next).map(row)];
        if (validTable(rows, 60)) { flush(); block.append(tableNode(rows)); }
        else paragraph.push(...lines.slice(i, next));
        i = next;
      } else if (title) {
        flush(); block.append(inline(make("h4"), title[1])); i++;
      } else if (item) {
        flush(); const ordered = !!item[2], list = make(ordered ? "ol" : "ul");
        if (ordered) list.setAttribute("start", item[2]);
        for (let match = item; match && !!match[2] === ordered; match = bullet.exec(lines[i] || "")) {
          const li = inline(make("li"), match[3]);
          if (ordered) li.setAttribute("value", match[2]);
          list.append(li); i++;
        }
        block.append(list);
      } else {
        if (!line.trim()) flush(); else paragraph.push(line);
        i++;
      }
    }
    flush(); return block;
  }

  function htmlRows(table) {
    // MinerU emits HTML tables. Only parse a passive tag subset; complex cells
    // (including rowspan / colspan) deliberately do not become chart data.
    if (typeof DOMParser === "undefined" || /<(?!\/?(?:table|thead|tbody|tr|td|th)\s*>)/i.test(table)) return [];
    const parsed = new DOMParser().parseFromString(table.replace(/&nbsp;/g, "&#160;"), "application/xml");
    if (parsed.documentElement.localName !== "table") return [];
    const rows = [...parsed.querySelectorAll("tr")];
    if (parsed.querySelectorAll("table").length !== 1 || rows.some(tr =>
      !["table", "thead", "tbody"].includes(tr.parentNode.localName) || [...tr.children].some(cell =>
        !["td", "th"].includes(cell.localName) || cell.children.length))) return [];
    return rows.map(tr => [...tr.children].map(cell => cell.textContent.trim()));
  }
  function sourceTables(text) {
    if (typeof text !== "string" || text.length > 16000) return [];
    // Only extract literal tables, not a code example containing table syntax.
    const chunks = [];
    let plain = [], open = null;
    for (const line of text.split(/\r?\n/)) {
      const marker = /^ {0,3}(\x60{3,}|~{3,})(.*)$/.exec(line);
      if (open) {
        if (marker && marker[1][0] === open[0] && marker[1].length >= open.length && !marker[2].trim()) open = null;
      } else if (marker) {
        chunks.push(plain); plain = []; open = marker[1];
      } else plain.push(line);
    }
    chunks.push(plain);
    const tables = [];
    for (const lines of chunks) {
      for (let i = 0; i + 2 < lines.length; i++) {
        if (!pipe(lines[i]) || !divider(lines[i + 1]) || row(lines[i]).length !== row(lines[i + 1]).length) continue;
        const rows = [row(lines[i])];
        // Never draw just the valid prefix of a table with an unsupported row.
        let complete = true;
        for (i += 2; /^\s*\|/.test(lines[i] || ""); i++) {
          if (pipe(lines[i])) rows.push(row(lines[i])); else complete = false;
        }
        i--;
        if (complete && validTable(rows)) tables.push(rows);
      }
      for (const match of lines.join("\n").matchAll(/<table>[\s\S]*?<\/table>/gi)) {
        const rows = htmlRows(match[0]);
        if (validTable(rows)) tables.push(rows);
      }
    }
    return tables;
  }
  function period(label, kind) {
    if (kind === "quarter") {
      const q = /^(\d{4})(?:\s*[- ]?Q([1-4])|年?第?([1-4一二三四])季度)$/i.exec(label);
      if (!q || +q[1] < 1000) return null;
      const quarter = Number(q[2] || q[3]) || "一二三四".indexOf(q[3]) + 1;
      return Date.UTC(+q[1], (quarter - 1) * 3, 1);
    }
    const patterns = {
      year: /^(\d{4})年?$/,
      month: /^(\d{4})[-/年](\d{1,2})月?$/,
      day: /^(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?$/,
    };
    const match = patterns[kind].exec(label);
    if (!match) return null;
    const year = +match[1], month = +(match[2] || 1), day = +(match[3] || 1);
    if (year < 1000 || month < 1 || month > 12 || day < 1 || day > 31) return null;
    const time = Date.UTC(year, month - 1, day);
    return new Date(time).getUTCDate() === day ? time : null;
  }
  function chartData(rows) {
    const [[label, measure], ...data] = rows;
    if (rows[0].length !== 2 || data.length < 3 || new Set(data.map(([name]) => name)).size !== data.length ||
        data.some(([name]) => !name || name.length > 48)) return null;
    if (/^(?:步骤(?:号|序号)?|序号|step(?: no\.?| number)?)$/i.test(label)) {
      return /(?:步骤|操作|动作|流程|说明|action|instruction|procedure)/i.test(measure) &&
        data.every(([name, text], i) => name === String(i + 1) && /\p{L}/u.test(text)) ? {kind: "flow", rows} : null;
    }
    if (/(?:编号|编码|电话|邮编|账号|序号|\b(?:id|code|sku|phone|zip)\b)/i.test(measure)) return null;
    const numeric = /^[+\-−]?(?:0|[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+)(?:\.\d+)?(%?)$/;
    const parsed = data.map(([, value]) => numeric.exec(value));
    if (parsed.some(match => !match || match[1] !== parsed[0]?.[1])) return null;
    const values = data.map(([, value]) => Number(value.replace(/[,%]/g, "").replace("−", "-")));
    if (values.some(value => !Number.isFinite(value) || Math.abs(value) > 1e12)) return null;
    if (/^(?:日期|时间|年份|年度|年|季度|月份|年月|date|time|year|quarter|month|period)(?:\s*\(.*\))?$/i.test(label)) {
      for (const kind of ["year", "quarter", "month", "day"]) {
        const times = data.map(([name]) => period(name, kind));
        if (times.every(time => time !== null) && new Set(times).size === times.length) {
          const indexes = times.map((_, i) => i).sort((a, b) => times[a] - times[b]);
          return {kind: "line", rows: [rows[0], ...indexes.map(i => data[i])], originalRows: rows,
            values: indexes.map(i => values[i]), times: indexes.map(i => times[i]), unit: parsed[0][1]};
        }
      }
      return null;
    }
    if (!/^(?:id|编号|序号|步骤|step)$/i.test(label) && data.length <= 12) return {kind: "bar", rows, values};
    return null;
  }

  // With multiple measures, never mix units on one axis. Only project a wide
  // table if every measure is chartable and the dimension is unambiguous.
  function tableCharts(rows) {
    if (rows[0].length === 2) return [chartData(rows)].filter(Boolean);
    if (new Set(rows[0]).size !== rows[0].length) return [];
    const charts = rows[0].slice(1).map((_, j) => chartData(rows.map(r => [r[0], r[j + 1]])));
    if (charts.some(c => !c || c.kind === "flow")) return [];
    return charts.map(c => ({...c, originalRows: rows}));
  }

  function drawTrend(chart) {
    const svg = (tag, attrs, text) => {
      const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
      for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
      if (text !== undefined) node.textContent = text;
      return node;
    };
    const {values, times, rows, unit} = chart;
    const lo = Math.min(...values), hi = Math.max(...values);
    const x = i => 4 + 492 * (times[i] - times[0]) / (times.at(-1) - times[0]);
    const y = value => hi === lo ? 60 : 113 - 106 * ((value - lo) / (hi - lo));
    const graphic = svg("svg", {viewBox: "0 0 500 120", preserveAspectRatio: "none", class: "answer-trend"});
    for (const v of new Set([lo, hi])) {
      graphic.append(svg("line", {x1: 4, x2: 496, y1: y(v), y2: y(v), class: "answer-gridline"}));
    }
    graphic.append(svg("polyline", {points: values.map((v, i) => x(i) + "," + y(v)).join(" ")}));
    values.forEach((v, i) => {
      const point = svg("circle", {cx: x(i), cy: y(v), r: 3});
      point.append(svg("title", {}, rows[i + 1].join("："))); graphic.append(point);
    });
    const plot = make("div"), ticks = make("div"), dates = make("div");
    plot.className = "answer-trend-plot"; plot.setAttribute("aria-hidden", "true");
    ticks.className = "answer-trend-ticks"; dates.className = "answer-trend-dates";
    for (const v of new Set([hi, lo])) ticks.append(make("span", String(v) + unit));
    dates.append(make("span", rows[1][0]), make("span", rows.at(-1)[0]));
    plot.append(ticks, graphic, dates); return plot;
  }
  function drawBars(chart) {
    const {values, rows} = chart;
    const lo = Math.min(0, ...values), hi = Math.max(0, ...values);
    const scale = hi === lo ? 1 : hi - lo, zero = -lo / scale;
    const list = make("ul"); list.className = "answer-bars";
    values.forEach((value, i) => {
      const li = make("li"), track = make("div"), bar = make("span"), axis = make("i");
      track.className = "answer-bar-track"; track.setAttribute("aria-hidden", "true");
      bar.style.insetInlineStart = 100 * (zero + Math.min(value, 0) / scale) + "%";
      bar.style.inlineSize = 100 * Math.abs(value) / scale + "%";
      axis.style.insetInlineStart = 100 * zero + "%";
      track.append(bar, axis); li.append(make("span", rows[i + 1][0]), track, make("strong", rows[i + 1][1]));
      list.append(li);
    });
    return list;
  }
  function summary(chart) {
    const section = make("figure"); section.className = "answer-viz";
    const kind = {line: "趋势", bar: "对比", flow: "流程"}[chart.kind];
    section.append(make("figcaption", chart.rows[0][1] + " · " + kind));
    if (chart.kind === "line") section.append(drawTrend(chart));
    else if (chart.kind === "bar") section.append(drawBars(chart));
    else {
      const steps = make("ol"); steps.className = "answer-flow";
      chart.rows.slice(1).forEach(([, text]) => steps.append(make("li", text)));
      section.append(steps);
    }
    const data = make("details");
    data.append(make("summary", "查看原始数据"), tableNode(chart.originalRows || chart.rows)); section.append(data);
    return section;
  }
  function charts(result, citation) {
    if (!["answered", "partial", "evidence_preview"].includes(result.status)) return null;
    const cited = new Set((result.claims || []).flatMap(claim => claim.evidence_ids || []));
    const group = make("section"); group.className = "answer-visuals";
    const seen = new Set();
    for (const [index, item] of (result.evidence || []).entries()) {
      // Ingestion can split a large table between parents even when this
      // entire parent fits in the evidence excerpt (truncated === false).
      if (item.truncated || /;structure_fragment:/.test(item.locator || "") ||
          (result.status !== "evidence_preview" && !cited.has(item.evidence_id))) continue;
      for (const rows of sourceTables(item.text)) {
        for (const chart of tableCharts(rows)) {
          const key = JSON.stringify(chart.rows);
          if (seen.has(key)) continue;
          if (!seen.size) group.append(make("h3", "证据图表"));
          seen.add(key);
          const view = summary(chart); view.append(citation(item, index + 1)); group.append(view);
          if (seen.size === 2) return group;
        }
      }
    }
    return seen.size ? group : null;
  }
  return Object.freeze({prose, charts});
})();
