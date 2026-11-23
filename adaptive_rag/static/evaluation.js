"use strict";

// Read-only view of saved evaluation reports. Report text reaches the page only through textContent.
(() => {
  const KIND_LABEL = {retrieval: "检索评测", permissions: "权限矩阵"};
  const MODE_TEXT = {model_free: "本地检索，不调用模型", cloud: "云端模型检索"};
  const CELL_TEXT = {
    readable: "可读，符合预期", blocked: "被拦截，符合预期",
    leak: "泄漏：不该看到却看到了", missing: "缺失：该看到却没看到", unknown: "报告里没有这个单元",
  };
  const state = {reports: [], selected: "", ticket: 0, tenant: ""};

  const byId = id => document.getElementById(id);
  const array = value => Array.isArray(value) ? value : [];
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const score = value => finite(value) ? value.toFixed(2) : "—";
  const millis = value => finite(value) ? value.toFixed(value >= 100 ? 0 : 1) : "—";
  const count = value => finite(value) ? String(value) : "—";
  const baseName = path => String(path).split(/[\\/]/).pop();

  function el(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = text;
    return element;
  }

  function decorative(element) {
    element.setAttribute("aria-hidden", "true");
    return element;
  }

  function scopeText(values) {
    if (values === null || values === undefined) return "全部分类";
    if (!Array.isArray(values) || !values.length) return "仅公共资料";
    return RagDomains.text(values);
  }

  // Local time to the minute. A report whose created_at cannot be parsed falls back to the time in its ID.
  function formatTime(report) {
    let date = new Date(Date.parse(report.created_at));
    if (Number.isNaN(date.getTime())) {
      const match = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z/.exec(report.report_id || "");
      date = match ? new Date(Date.UTC(+match[1], +match[2] - 1, +match[3], +match[4], +match[5], +match[6])) : null;
    }
    if (!date) return "时间未知";
    const pad = value => String(value).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  async function api(path) {
    let response;
    try { response = await fetch(path, {credentials: "same-origin", cache: "no-store"}); }
    catch { throw Error("无法连接服务，请检查网络或服务状态后重试。"); }
    if (response.status === 401) { location.href = "/"; throw Error("请先登录工作台。"); }
    if (response.status === 429) throw Error("请求过于频繁，请稍后重试。");
    let value = null;
    try { value = await response.json(); }
    catch { if (response.ok) throw Error(`服务返回格式错误（HTTP ${response.status}），请稍后重试。`); }
    if (response.status === 403) throw Error("只有管理员可以查看评测报告。");
    if (response.status === 404 && path.startsWith("/evaluation/reports/")) throw Error("这份报告不存在，或不属于当前租户。");
    if (!response.ok) throw Error(value?.error?.message || `请求失败（HTTP ${response.status}）`);
    return value;
  }

  function notice(message, error = false) {
    const element = byId("evaluation-notice");
    element.textContent = message || "";
    element.hidden = !message;
    element.classList.toggle("error", error);
  }

  function pill(tone, symbol, label) {
    const element = el("span", tone ? `pill ${tone}` : "pill");
    element.append(decorative(el("span", undefined, symbol)), label);
    return element;
  }

  // ---- Report list -------------------------------------------------------------------------------------------

  function reportLine(report) {
    const line = el("span", "report-item-line");
    if (report.kind === "permissions") {
      const [tone, symbol, label] = report.passed === true ? ["ok", "✓", "通过"]
        : report.passed === false ? ["err", "✕", "未通过"] : ["", "–", "结果未知"];
      line.append(pill(tone, symbol, label), ` ${count(report.cells)} 个单元 · 泄漏 ${count(report.leaks)} · 缺失 ${count(report.missing)}`);
    } else if (report.kind === "retrieval") {
      line.textContent = `Hit@${report.top_k ?? "K"} ${score(report.metrics?.hit_at_k)} · ${count(report.case_count)} 个问题`;
    } else {
      line.textContent = "此版本页面无法识别这份报告的类型";
    }
    return line;
  }

  function markSelected(reportId) {
    state.selected = reportId;
    for (const button of byId("evaluation-list").querySelectorAll(".report-item")) {
      button.setAttribute("aria-current", String(button.dataset.reportId === reportId));
    }
  }

  function renderList() {
    const root = byId("evaluation-list");
    root.replaceChildren();
    if (!state.reports.length) {
      const item = el("li");
      item.append(el("div", "plain-empty", "还没有已保存的报告。"));
      root.append(item);
      return;
    }
    for (const report of state.reports) {
      const item = el("li");
      const button = el("button", "report-item");
      button.type = "button";
      button.dataset.reportId = report.report_id;
      button.setAttribute("aria-current", String(report.report_id === state.selected));
      const head = el("span", "report-item-head");
      head.append(el("span", undefined, KIND_LABEL[report.kind] || "未知类型"), el("span", "report-item-time", formatTime(report)));
      button.append(head, reportLine(report));
      button.addEventListener("click", () => choose(report.report_id));
      item.append(button);
      root.append(item);
    }
  }

  async function loadList() {
    const value = await api("/evaluation/reports");
    state.reports = array(value.reports);
    renderList();
  }

  // ---- Shared building blocks -------------------------------------------------------------------------------

  function sheetHead(report, title) {
    const head = el("header", "sheet-head");
    head.append(el("h2", undefined, title), el("p", "sheet-id", `${report.report_id} · ${formatTime(report)}`));
    return head;
  }

  function chips(items) {
    const root = el("div", "chips");
    for (const text of items) if (text) root.append(el("span", "small-pill", text));
    return root;
  }

  function block(title, note) {
    const section = el("section", "evaluation-block");
    section.append(el("h3", undefined, title));
    if (note) section.append(el("p", "block-note", note));
    return section;
  }

  function scopeBox(items) {
    const box = el("section", "evaluation-scope");
    const list = el("ul");
    for (const text of items) list.append(el("li", undefined, text));
    box.append(el("h3", undefined, "这份报告能说明什么"), list);
    return box;
  }

  function metricTile(label, value, note, {unit, meter, tone} = {}) {
    const tile = el("div", tone ? `eval-tile ${tone}` : "eval-tile");
    const number = el("strong", "eval-tile-value", value);
    if (unit) number.append(el("small", undefined, unit));
    tile.append(el("span", "eval-tile-label", label), number, el("span", "eval-tile-note", note));
    if (finite(meter)) {
      const track = decorative(el("span", "meter"));
      const fill = el("i");
      fill.style.width = `${Math.max(0, Math.min(1, meter)) * 100}%`;
      track.append(fill);
      tile.append(track);
    }
    return tile;
  }

  function tableOf(className, caption, columns) {
    const table = el("table", className);
    const row = el("tr");
    for (const column of columns) {
      const cell = el("th", column.className, column.text);
      cell.scope = "col";
      row.append(cell);
    }
    const head = el("thead");
    head.append(row);
    const body = el("tbody");
    table.append(el("caption", "sr-only", caption), head, body);
    return {table, body};
  }

  function scroller(label, table) {
    const wrap = el("div", "evaluation-scroll");
    wrap.tabIndex = 0;
    wrap.setAttribute("role", "region");
    wrap.setAttribute("aria-label", label);
    wrap.append(table);
    return wrap;
  }

  function rowHeader(className, text) {
    const cell = el("th", className, text);
    cell.scope = "row";
    return cell;
  }

  // ---- Retrieval report ------------------------------------------------------------------------------------

  function retrievalMetrics(report, topK) {
    const metrics = report.metrics || {};
    const section = block("检索指标", `只统计有标注的问题（${count(report.positive_case_count)} 个）。分数在 0 到 1 之间，越高越好。`);
    const grid = el("div", "eval-tiles");
    const specs = [
      [`Hit@${topK}`, metrics.hit_at_k, `前 ${topK} 条里至少有一个标注答案的问题占比`],
      [`Recall@${topK}`, metrics.recall_at_k, "标注答案被找回的平均比例"],
      [`MRR@${topK}`, metrics.mrr_at_k, "第一个标注答案名次的倒数，取平均"],
      [`NDCG@${topK}`, metrics.ndcg_at_k, "标注答案排得越靠前，分数越高"],
    ];
    for (const [label, value, note] of specs) {
      grid.append(metricTile(label, score(value), finite(value) ? note : "没有带标注的问题，无法计算", {meter: value}));
    }
    if (finite(report.retrieval_empty_rate)) {
      grid.append(metricTile("未找到率", `${Math.round(report.retrieval_empty_rate * 100)}%`,
        `无标注问题（${count(report.unlabelled_case_count)} 个）里没有找到相同或相似内容的占比，不代表拒答质量`));
    }
    section.append(grid);
    return section;
  }

  function latencyBlock(report) {
    const latency = report.latency_ms;
    if (!latency || typeof latency !== "object") return block("耗时", "这份报告没有记录耗时。");
    const few = finite(latency.count) && latency.count < 20;
    const section = block("耗时", `每个问题从发起检索到返回的时间，共 ${count(latency.count)} 个样本。`);
    const grid = el("div", "eval-tiles");
    grid.append(
      metricTile("中位数 p50", millis(latency.p50), "一半的问题比这更快", {unit: "ms"}),
      metricTile("p95", millis(latency.p95), few ? `样本只有 ${count(latency.count)} 个，p95 等于最大值` : "95% 的问题比这更快", {unit: "ms"}),
      metricTile("最大值", millis(latency.max), "最慢的一个问题", {unit: "ms"}),
    );
    section.append(grid);
    return section;
  }

  function casesBlock(report) {
    const cases = array(report.cases);
    const section = block("逐题结果", "每个问题的标注答案和实际检索结果，✓ 表示检索到的 ID 是标注答案。");
    if (!cases.length) { section.append(el("p", "block-note", "这份报告没有逐题记录。")); return section; }
    const {table, body} = tableOf("evaluation-table", "逐题检索结果", [
      {text: "问题"}, {text: "标注答案"}, {text: "检索到"}, {text: "结果"},
      {text: "Recall", className: "num"}, {text: "MRR", className: "num"}, {text: "NDCG", className: "num"}, {text: "耗时 ms", className: "num"},
    ]);
    for (const item of cases) {
      const gold = array(item.gold_ids);
      const goldSet = new Set(gold);
      const row = el("tr");
      const question = rowHeader("case-question", item.question || "（空问题）");
      question.append(el("span", "case-id", [item.id, item.sku ? `SKU ${item.sku}` : ""].filter(Boolean).join(" · ")));
      const goldCell = el("td", "id-cell");
      if (gold.length) {
        goldCell.append(el("span", "cell-kind", item.gold_type === "chunk" ? "片段" : "文档"));
        for (const id of gold) goldCell.append(el("span", "id-chip", id));
      } else {
        goldCell.append(el("span", "none", "无标注"));
      }
      const retrieved = array(item.retrieved_ids);
      const foundCell = el("td", "id-cell");
      if (!retrieved.length) foundCell.append(el("span", "none", "未找到"));
      for (const id of retrieved) {
        const hit = goldSet.has(id);
        const chip = el("span", hit ? "id-chip hit" : "id-chip", hit ? `✓ ${id}` : id);
        if (hit) chip.title = "命中标注答案";
        foundCell.append(chip);
      }
      const scores = item.metrics;
      const resultCell = el("td");
      resultCell.append(!scores ? pill("", "–", "无标注") : scores.hit_at_k >= 1 ? pill("ok", "✓", "命中") : pill("err", "✕", "未命中"));
      row.append(question, goldCell, foundCell, resultCell,
        el("td", "num", score(scores?.recall_at_k)), el("td", "num", score(scores?.mrr_at_k)),
        el("td", "num", score(scores?.ndcg_at_k)), el("td", "num", millis(item.duration_ms)));
      body.append(row);
    }
    section.append(scroller("逐题检索结果，可左右滚动", table));
    return section;
  }

  function retrievalSheet(report) {
    const parameters = report.parameters || {};
    const topK = parameters.top_k ?? "K";
    const sheet = el("article", "evaluation-sheet");
    sheet.append(sheetHead(report, "检索评测"), chips([
      `前 ${topK} 条`,
      `检索范围：${scopeText(parameters.data_domains ?? null)}`,
      parameters.fixture_path ? `标注文件：${baseName(parameters.fixture_path)}` : "",
      `${count(report.case_count)} 个问题，${count(report.positive_case_count)} 个有标注`,
      `模型调用 ${count(report.model_calls)} 次`,
      MODE_TEXT[report.capabilities?.mode] || "",
    ]), retrievalMetrics(report, topK), latencyBlock(report), casesBlock(report));
    const scope = [
      "只评估检索：看标注答案有没有被检索到、排在第几位。不评估答案写得好不好，也没有用大模型打分。",
      "指标只统计有标注的问题，没有标注的问题不进入平均。",
      "耗时是墙钟时间（毫秒），分位数按最近秩法计算。",
    ];
    if (finite(report.positive_case_count) && report.positive_case_count < 30) {
      scope.push(`有标注的问题只有 ${report.positive_case_count} 个，分数会随单个问题的变化大幅波动。适合对比同一批问题的前后两次结果，不适合当作绝对水平。`);
    }
    if (report.capabilities?.mode === "cloud") scope.push("这次使用云端模型，耗时包含网络往返，波动比本地检索大。");
    sheet.append(scopeBox(scope));
    return sheet;
  }

  // ---- Permission report -----------------------------------------------------------------------------------

  function documentMeta(doc) {
    const parts = [RagDomains.text(array(doc.data_domains))];
    if (array(doc.allowed_subjects).length) parts.push("仅指名账号");
    if (doc.revoked) parts.push("已撤销");
    return parts.join(" · ");
  }

  function matrixCell(identity, doc, cell) {
    const kind = !cell ? "unknown" : cell.result === "leak" ? "leak" : cell.result === "missing" ? "missing"
      : cell.visible ? "readable" : "blocked";
    const failing = kind === "leak" || kind === "missing";
    const td = el("td", failing ? kind : undefined);
    td.title = `${identity.label || identity.identity} × ${doc.label || doc.document_id}：${CELL_TEXT[kind]}`;
    if (failing) {
      td.tabIndex = 0;
      const flag = el("span", "cell-flag");
      flag.append(decorative(el("span", undefined, kind === "leak" ? "✕" : "!")), kind === "leak" ? "泄漏" : "缺失");
      td.append(flag);
    } else if (kind === "unknown") {
      td.append(el("span", "none", "–"));
    } else {
      td.append(decorative(el("span", `mark ${kind}`)), el("span", "sr-only", kind === "readable" ? "可读" : "拦截"));
    }
    return td;
  }

  // Highlight the row and column of the cell under the pointer or focus, so a long grid stays readable.
  function trackHover(table) {
    const clear = () => { for (const item of table.querySelectorAll(".hot")) item.classList.remove("hot"); };
    const mark = event => {
      const cell = event.target.closest("td");
      if (!cell || !table.contains(cell)) return;
      clear();
      table.tHead.rows[0].cells[cell.cellIndex]?.classList.add("hot");
      cell.parentElement.cells[0].classList.add("hot");
    };
    table.addEventListener("pointerover", mark);
    table.addEventListener("focusin", mark);
    table.addEventListener("pointerleave", clear);
    table.addEventListener("focusout", clear);
  }

  function legendItem(symbol, text) {
    const item = el("li");
    item.append(symbol, text);
    return item;
  }

  function permissionVerdict(summary) {
    const passed = summary.passed;
    const box = el("div", passed === true ? "verdict ok" : passed === false ? "verdict err" : "verdict");
    const text = passed === true
      ? `通过。${count(summary.cells)} 个单元全部符合预期，${count(summary.broad_checks)} 项整体检查全部一致。`
      : passed === false
        ? `未通过。${count(summary.leaks)} 处泄漏，${count(summary.missing)} 处缺失，${count(summary.broad_failures)} 项整体检查不一致。`
        : "报告缺少汇总结果。";
    box.append(decorative(el("span", "icon", passed === true ? "✓" : passed === false ? "✕" : "–")), el("span", undefined, text));
    return box;
  }

  function permissionSheet(report) {
    const parameters = report.parameters || {};
    const summary = report.summary || {};
    const docs = array(report.documents);
    const identities = array(report.identities);
    const cells = new Map(array(report.cells).map(cell => [`${cell.identity}\0${cell.document_id}`, cell]));
    const docLabels = new Map(docs.map(doc => [doc.document_id, doc.label || doc.document_id]));
    const identityLabels = new Map(identities.map(identity => [identity.identity, identity.label || identity.identity]));
    const sheet = el("article", "evaluation-sheet");
    sheet.append(sheetHead(report, "权限矩阵"), chips([
      `${docs.length} 份文档 × ${identities.length} 种身份`, "合成语料，不含真实资料", `模型调用 ${count(report.model_calls)} 次`,
    ]), permissionVerdict(summary));

    const tiles = el("div", "eval-tiles");
    tiles.append(
      metricTile("检查单元", count(summary.cells), `${count(summary.ok)} 个符合预期`),
      metricTile("泄漏", count(summary.leaks), "不该看到却看到了", {tone: summary.leaks > 0 ? "err" : ""}),
      metricTile("缺失", count(summary.missing), "该看到却没看到", {tone: summary.missing > 0 ? "warn" : ""}),
      metricTile("整体检查", `${count(summary.broad_checks - summary.broad_failures)} / ${count(summary.broad_checks)}`,
        "一次问遍全部文档，看到的集合必须与预期完全一致", {tone: summary.broad_failures > 0 ? "err" : ""}),
    );
    sheet.append(tiles);

    const grid = block("身份 × 文档", "每个单元：这个身份检索这份文档独有的词，能不能在检索证据里看到它。");
    const legend = el("ul", "legend");
    legend.setAttribute("aria-label", "图例");
    legend.append(
      legendItem(decorative(el("span", "mark readable")), "可读，符合预期"),
      legendItem(decorative(el("span", "mark blocked")), "被拦截，符合预期"),
      legendItem(pill("err", "✕", "泄漏"), "不该看到却看到了"),
      legendItem(pill("warn", "!", "缺失"), "该看到却没看到"),
    );
    const {table, body} = tableOf("matrix", "文档与身份的可见性矩阵：每行是一份文档，每列是一种身份", [
      {text: "文档 / 身份", className: "corner"}, ...identities.map(identity => ({text: identity.label || identity.identity})),
    ]);
    for (const doc of docs) {
      const row = el("tr");
      const header = rowHeader(undefined);
      header.append(el("span", "doc-name", doc.label || doc.document_id), el("span", "doc-meta", documentMeta(doc)));
      row.append(header, ...identities.map(identity => matrixCell(identity, doc, cells.get(`${identity.identity}\0${doc.document_id}`))));
      body.append(row);
    }
    trackHover(table);
    grid.append(legend, scroller("权限矩阵，可左右滚动", table));
    sheet.append(grid);

    const broad = block("整体检查", "用一个所有文档都含有的词检索，每个身份看到的文档集合必须与预期完全一致。");
    const broadTable = tableOf("evaluation-table", "整体检查结果", [
      {text: "身份"}, {text: "应看到", className: "num"}, {text: "实际看到", className: "num"}, {text: "结果"},
    ]);
    for (const item of array(report.broad)) {
      const row = el("tr");
      const result = el("td");
      result.append(item.ok ? pill("ok", "✓", "一致") : pill("err", "✕", "不一致"));
      for (const [name, ids] of [["泄漏", item.leaks], ["缺失", item.missing]]) {
        if (array(ids).length) result.append(el("span", "broad-detail", `${name}：${array(ids).map(id => docLabels.get(id) || id).join("、")}`));
      }
      row.append(rowHeader(undefined, identityLabels.get(item.identity) || item.identity),
        el("td", "num", `${array(item.expected_ids).length} 份`), el("td", "num", `${array(item.returned_ids).length} 份`), result);
      broadTable.body.append(row);
    }
    broad.append(scroller("整体检查结果", broadTable.table));
    sheet.append(broad, scopeBox([
      `语料是合成的：${docs.length} 份文档、${identities.length} 种身份，不含任何真实资料，也没有调用模型（${count(report.model_calls)} 次）。`,
      "每个身份该看到哪些文档，是按《数据分类与检索鉴权》（docs/data-permissions.md）手写的预期，不是从被测代码里推出来的。",
      `每个单元用文档独有的词检索，取前 ${count(parameters.targeted_top_k)} 条；整体检查用共享词检索，取前 ${count(parameters.broad_top_k)} 条。`,
      "只覆盖检索证据的权限过滤。云端稠密检索路径、trace 回放、图片资料和答案内容不在范围内。",
    ]));
    return sheet;
  }

  function unknownSheet(report) {
    const sheet = el("article", "evaluation-sheet");
    sheet.append(sheetHead(report, "无法显示的报告"),
      el("p", "muted", `这份报告的类型是“${report.kind ?? "缺失"}”，当前页面只能显示检索评测和权限矩阵。`));
    return sheet;
  }

  // ---- Detail pane -----------------------------------------------------------------------------------------

  function setBusy(busy) {
    const detail = byId("evaluation-detail");
    detail.classList.toggle("loading", busy);
    detail.setAttribute("aria-busy", String(busy));
  }

  function showFailure(message) {
    const box = el("div", "evaluation-error", message);
    box.setAttribute("role", "alert");
    byId("evaluation-detail").replaceChildren(box);
  }

  function showEmpty() {
    state.ticket += 1;
    markSelected("");
    setBusy(false);
    const box = el("div", "plain-empty");
    box.append(el("p", undefined, "这个租户还没有评测报告。"),
      el("p", undefined, `在服务所在的机器上运行 adaptive-rag eval-permissions --tenant ${state.tenant}，再点击左侧“刷新”。`));
    byId("evaluation-detail").replaceChildren(box);
  }

  async function showReport(reportId) {
    const ticket = ++state.ticket;
    markSelected(reportId);
    setBusy(true);
    try {
      const report = await api(`/evaluation/reports/${encodeURIComponent(reportId)}`);
      if (ticket !== state.ticket) return;
      byId("evaluation-detail").replaceChildren(report.kind === "permissions" ? permissionSheet(report)
        : report.kind === "retrieval" ? retrievalSheet(report) : unknownSheet(report));
      byId("evaluation-status").textContent = `已显示报告：${KIND_LABEL[report.kind] || "未知类型"}，${formatTime(report)}`;
      if (byId("evaluation-notice").classList.contains("error")) notice("");
    } catch (error) {
      if (ticket !== state.ticket) return;
      showFailure(error.message);
    } finally {
      if (ticket === state.ticket) setBusy(false);
    }
  }

  function showNewest() {
    const newest = state.reports[0]?.report_id;
    if (!newest) { showEmpty(); return Promise.resolve(); }
    history.replaceState(null, "", `#${newest}`);
    return showReport(newest);
  }

  function hashId() {
    try { return decodeURIComponent(location.hash.slice(1)); } catch { return ""; }
  }

  function choose(reportId) {
    if (hashId() === reportId) showReport(reportId); else location.hash = reportId;
    // The detail sits below the list on a narrow screen, so bring it into view.
    if (matchMedia("(max-width: 720px)").matches) {
      const calm = matchMedia("(prefers-reduced-motion: reduce)").matches;
      byId("evaluation-detail").scrollIntoView({block: "start", behavior: calm ? "auto" : "smooth"});
    }
  }

  window.addEventListener("hashchange", () => {
    if (byId("evaluation-workspace").hidden) return;
    const wanted = hashId();
    if (wanted) showReport(wanted); else showNewest();
  });

  byId("evaluation-refresh").addEventListener("click", async () => {
    const button = byId("evaluation-refresh");
    button.disabled = true;
    try {
      await loadList();
      if (!state.selected) await showNewest();
      notice(`已读取最新报告列表，共 ${state.reports.length} 份。`);
    } catch (error) { notice(error.message, true); }
    finally { button.disabled = false; }
  });

  (async () => {
    try {
      const session = await api("/console/session");
      byId("evaluation-account").textContent = session.username;
      if (!session.admin) throw Error("只有管理员可以查看评测报告。请返回知识库对话，按当前权限检索资料。");
      byId("evaluation-open").hidden = false;
      state.tenant = session.tenant_id;
      await RagDomains.load();
      byId("evaluation-tenant").textContent = `租户：${session.tenant_id}`;
      byId("evaluation-workspace").hidden = false;
      await loadList();
      const wanted = hashId();
      if (wanted) await showReport(wanted); else await showNewest();
    } catch (error) {
      notice(error.message, true);
      if (!byId("evaluation-workspace").hidden) showFailure(error.message);
    }
  })();
})();
