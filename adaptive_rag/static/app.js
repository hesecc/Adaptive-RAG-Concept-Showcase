const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = {
  user: null,
  activeQuestion: "",
  messages: [],
  usersEpoch: 0,
  connected: false,
  capabilities: null,
  stages: [],
  trace: null,
  result: null,
  traceId: "",
  selected: null,
  running: false,
  controller: null,
  conversationId: null,
  conversations: [],
  listEpoch: 0,
  nextOffset: null,
  restoring: false,
  tab: "inspect",
  started: 0,
  timer: null,
  sessionEpoch: 0,
  viewEpoch: 0,
  sourceEpoch: 0,
  connecting: false,
};

const labels = {
  "decision.preflight": ["前置决策", "根据当前策略决定是否进入检索。"],
  planner: ["问题规划", "理解问题，拆分需要检索的子问题。"],
  rewrite: ["查询改写", "为检索生成更明确的查询表达。"],
  multi_query: ["多查询扩展", "生成不同角度的查询变体。"],
  hyde: ["HyDE 扩展", "用假设文档辅助召回。"],
  snapshot: ["固定知识快照", "为本次检索选择一个不可变知识版本。"],
  authorized_chunks: [
    "权限与范围过滤",
    "按当前身份、权限和 SKU 筛选可检索的块。",
  ],
  lexical: ["BM25 关键词检索", "根据词项匹配召回候选内容。"],
  dense: ["向量语义检索", "按语义相似度召回授权候选内容。"],
  fusion: ["RRF 结果融合", "融合各检索通道的排名，合并重复命中。"],
  reranker: ["相关性重排", "对融合后的候选执行已配置的排序适配器。"],
  relevance: [
    "相似度判定",
    "按词面重合或重排分数判断候选是否与问题相同或相似，不相似的不会作为来源。",
  ],
  parent_backfill: ["父块回填", "将检索到的子块回填为更完整的上下文。"],
  judge: ["证据充分性检查", "判断证据是否覆盖各个子问题。"],
  "decision.gap": ["补检索决策", "根据缺项和剩余预算判断是否补充检索。"],
  gap: ["缺项查询生成", "针对未覆盖的问题生成定向补检查询。"],
  "decision.generate": ["生成前决策", "判断是否继续进入答案生成阶段。"],
  generator: [
    "生成引用回答",
    "根据当前证据生成回答草稿与引用。草稿需要后续核验。",
  ],
  verifier: ["回答与引用核验", "执行当前配置的引用、支持性及完整性检查。"],
  final_access_check: [
    "最终权限复核",
    "在返回结果前重新确认当前证据读取权限。",
  ],
};
const roleNames = {
  parser: "文档解析",
  cleaner: "文本清洗",
  chunker: "父子切块",
  store: "知识存储",
  objects: "原文归档",
  trace: "链路记录",
  decision: "决策策略",
  planner: "问题规划",
  rewrite: "查询改写",
  multi_query: "多查询",
  hyde: "HyDE",
  dense: "语义检索",
  lexical: "关键词检索",
  fusion: "结果融合",
  reranker: "相关性重排",
  judge: "证据检查",
  gap: "定向补检",
  generator: "答案生成",
  verifier: "回答核验",
};
const statuses = {
  ok: "已完成",
  running: "执行中",
  disabled: "未启用",
  skipped: "已跳过",
  error: "失败",
  timeout: "超时",
  cancelled: "已停止",
  answered: "已回答",
  partial: "部分回答",
  insufficient: "证据不足",
  evidence_preview: "证据预览",
  no_index: "暂无知识索引",
  empty: "未找到相似内容",
  clarify: "需要补充信息",
  direct_reply: "对话提示",
  capability_unavailable: "能力暂不可用",
  policy_stopped: "策略已停止",
};
const nameFor = (name) =>
  labels[name]?.[0] || labels[name.split(".")[0]]?.[0] || name;
const descriptionFor = (name) =>
  labels[name]?.[1] ||
  labels[name.split(".")[0]]?.[1] ||
  "查看该适配器实际执行的结构化结果。";
const number = (value) =>
  typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })
    : "—";
const ms = (value) => (typeof value === "number" ? `${number(value)} ms` : "—");
function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = String(text);
  return node;
}
function json(value) {
  return JSON.stringify(value, null, 2);
}
function empty(target, text) {
  target.replaceChildren(el("div", "plain-empty", text));
}
function notice(message = "", type = "") {
  const node = $("#notice");
  node.textContent = message;
  node.className = `run-notice ${type}`;
  node.hidden = !message;
}
const failedStatuses = ["error", "timeout", "cancelled"];
// What the answer and evidence panels say when a query ends without an answer.
const noAnswerNotes = {
  direct_reply: "这是工作台提示，没有使用知识库资料。",
  evidence_preview: "当前模式返回证据预览，尚未启用答案生成。",
  empty:
    "知识库中没有找到与问题相同或相似的内容，所以没有生成回答。可以换一种说法，或请管理员补充资料。",
  insufficient: "本轮证据不足，未生成答案。",
};
// A figure travels inside the evidence text as one Markdown line that holds its storage key.
// The page shows the picture itself, so the line is replaced by a short placeholder.
const FIGURE_LINE = /!\[[^[\]\n]*\]\(ragfig:[0-9a-f]{64}(?: "[^"\n]*")?\)/g;
const withoutFigureLines = (text) =>
  (text || "").replace(FIGURE_LINE, "［插图］");
const figureLoads = new WeakMap();
const figureUrls = new Set();
const figureObserver = new IntersectionObserver(entries => {
  for (const {target, isIntersecting} of entries) if (isIntersecting) {
    figureObserver.unobserve(target);
    figureLoads.get(target)?.();
  }
});
function figureNode(item, image, label = "") {
  const generation =
    item.generation || state.result?.generation || state.trace?.generation;
  const url = `/evidence/${encodeURIComponent(generation)}/${encodeURIComponent(item.evidence_id)}/images/${image.key}`;
  const figure = el("figure", "evidence-figure");
  const link = el("a");
  link.target = "_blank";
  link.rel = "noopener";
  link.title = "在新标签页打开原图";
  const picture = el("img");
  picture.alt = image.caption || "文档插图";
  picture.loading = "lazy";
  picture.decoding = "async";
  // Native img/link requests cannot carry the expected owner. The page CSP
  // permits data: for inline images; the original link uses the checked blob.
  const epoch = state.viewEpoch;
  figureLoads.set(picture, async () => {
    if (epoch !== state.viewEpoch) return;
    try {
      const blob = await (await api(url)).blob();
      if (epoch !== state.viewEpoch) return;
      if (!["image/png", "image/jpeg", "image/webp"].includes(blob.type)) throw new Error();
      const reader = new FileReader();
      reader.onload = () => {
        if (epoch !== state.viewEpoch) return;
        picture.src = reader.result;
        link.href = URL.createObjectURL(blob);
        figureUrls.add(link.href);
      };
      reader.onerror = () => link.replaceWith(el("p", "muted", "这张图片暂时无法显示。"));
      reader.readAsDataURL(blob);
    } catch {
      if (epoch === state.viewEpoch) link.replaceWith(el("p", "muted", "这张图片暂时无法显示。"));
    }
  });
  figureObserver.observe(picture);
  picture.addEventListener(
    "error",
    () => link.replaceWith(el("p", "muted", "这张图片暂时无法显示。")),
    { once: true },
  );
  link.append(picture);
  const caption = el("figcaption");
  if (image.caption) caption.append(el("span", "", image.caption));
  caption.append(
    el(
      "span",
      "figure-source",
      `来源：${[item.document_id, image.page ? `第 ${image.page} 页` : "", label]
        .filter(Boolean)
        .join(" · ")}`,
    ),
  );
  figure.append(link, caption);
  return figure;
}
function failureDetails(stage, trace = state.trace) {
  if (stage?.error?.message) return stage.error;
  const call = trace?.usage?.calls?.findLast((item) =>
    ["network_error", "timeout", "http_error", "invalid_response"].includes(
      item.status,
    ),
  );
  if (call?.error?.message) return call.error;
  if (call?.status === "network_error")
    return {
      code: "cloud_network_error",
      message:
        "云模型连接中断，本次请求未获得完整响应。已保留此前完成的步骤，可手动重试。",
    };
  if (call?.status === "timeout" || stage?.status === "timeout")
    return {
      code: "request_timeout",
      message: "此步骤已超时，未获得有效返回。请稍后手动重试。",
    };
  if (call?.status === "http_error")
    return {
      code: "cloud_http_error",
      message: `云平台返回请求错误${call.http_status ? `（HTTP ${call.http_status}）` : ""}，请检查模型服务状态与配置。`,
    };
  if (call?.status === "invalid_response")
    return {
      code: "cloud_invalid_response",
      message: "云平台未返回符合协议的数据，本步骤没有有效返回。",
    };
  if (stage?.status === "cancelled")
    return { code: "query_cancelled", message: "查询已停止，此步骤没有完成。" };
  if (stage?.reason === "output_validation_failed")
    return {
      code: "output_validation_failed",
      message: "插件返回未通过数据校验，已停止后续执行。",
    };
  return {
    code: "step_failed",
    message: "此步骤执行失败，没有产生有效返回。已完成步骤可在执行链路中查看。",
  };
}
function failureMessage(trace = state.trace) {
  const stage = trace?.stages?.findLast((item) =>
    failedStatuses.includes(item.status),
  );
  const detail = failureDetails(stage, trace);
  return `${stage ? `${nameFor(stage.name)}：` : ""}${detail.message}`;
}
let toastTimer;
function toast(message) {
  const box = $("#toast");
  box.textContent = message;
  box.hidden = false;
  if (box.showPopover) {
    // A popover sits in the top layer, so the message stays visible above an open modal <dialog>.
    // Hiding first re-enters the top layer when a dialog was opened after an earlier toast.
    box.hidePopover();
    box.showPopover();
  }
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    if (box.hidePopover) box.hidePopover();
    box.hidden = true;
  }, 3000);
}

function dialog(id) {
  if (!$(id).open) $(id).showModal();
}
$$("[data-close]").forEach((button) =>
  button.addEventListener("click", () =>
    document.getElementById(button.dataset.close).close(),
  ),
);
$$("dialog").forEach((node) =>
  node.addEventListener("click", (event) => {
    if (event.target === node) {
      const r = node.getBoundingClientRect();
      if (
        event.clientX < r.left ||
        event.clientX > r.right ||
        event.clientY < r.top ||
        event.clientY > r.bottom
      )
        node.close();
    }
  }),
);
$("#plugins-open").addEventListener("click", () => dialog("#plugins-dialog"));

const ownerHeaders = {subject_id: "X-RAG-Principal", tenant_id: "X-RAG-Tenant"};
const transport = window.fetch.bind(window);
// The shared domain picker fetches its catalog directly. Apply the workbench
// session contract to that endpoint without changing the picker on other pages.
window.fetch = (path, options) => path === "/access/domains" ? api(path, options) : transport(path, options);
const sessionChanged = "登录身份已变更，请重新登录或刷新工作台。";
const sessionSignal = "rag.session.changed";
function notifyOtherTabs() {
  try { window.localStorage.setItem(sessionSignal, crypto.randomUUID()); } catch {
    // Storage may be unavailable. Requests still carry the owner precondition.
  }
}
function matchesOwner(headers, owner) {
  try {
    return Object.entries(ownerHeaders).every(([key, header]) =>
      headers.has(header) && decodeURIComponent(headers.get(header)) === owner[key]);
  } catch { return false; }
}
async function api(path, options = {}, owner = state.user) {
  const epoch = state.sessionEpoch;
  if (!owner && path !== "/auth/login" && path !== "/console/session") throw new Error(sessionChanged);
  const headers = new Headers(options.headers);
  if (owner) for (const [key, header] of Object.entries(ownerHeaders)) {
    const encoded = encodeURIComponent(owner[key]).replace(/[!'()*]/g, c => "%" + c.charCodeAt(0).toString(16).toUpperCase());
    headers.set(header, encoded);
  }
  let response;
  try {
    response = await transport(path, {
      ...options,
      headers,
      cache: "no-store",
      credentials: "same-origin",
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw Object.assign(new Error("无法连接服务，请检查网络或服务状态后重试。"), { status: 0 });
  }
  if (epoch !== state.sessionEpoch) throw new Error(sessionChanged);
  // Gate both JSON and SSE before handing a single byte to a renderer. Gateways
  // may return unsigned errors (e.g. 503); do not display their body either.
  const tagged = Object.values(ownerHeaders).some(header => response.headers.has(header));
  if (owner && (response.ok || tagged) && !matchesOwner(response.headers, owner)) {
    showLogin(sessionChanged);
    throw new Error(sessionChanged);
  }
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`;
    let code;
    if (!owner || tagged) try {
      const body = await response.json();
      code = body.error?.code;
      message = body.error?.message || body.detail || message;
    } catch {}
    if (response.status === 401) {
      message = path === "/auth/login" ? "账号或密码不正确。" : "登录已失效，请重新登录。";
      if (path !== "/auth/login" && epoch === state.sessionEpoch && state.connected)
        showLogin(message);
    }
    if (response.status === 403) message = "当前账号没有执行此操作的权限。";
    if (response.status === 429) message = "请求过于频繁，请稍后重试。";
    const error = new Error(message);
    error.status = response.status;
    error.code = code;
    throw error;
  }
  return response;
}

const optionalQuerySteps = ["rewrite", "multi_query", "hyde"];
const topKChoices = [3, 5, 10, 20];
const skuPattern = /^[\p{L}\p{N}_.:-]{1,128}$/u;
function preferenceKey(user) {
  return "rag.retrieval.v1:" + JSON.stringify([user.tenant_id, user.subject_id]);
}
function restoreQueryPreferences(reset = false) {
  let value;
  if (state.user && !reset) {
    try { value = JSON.parse(localStorage.getItem(preferenceKey(state.user))); } catch {}
  }
  $("#top-k").value = String(topKChoices.includes(value?.top_k) ? value.top_k : 5);
  $("#sku").value = typeof value?.sku === "string" && skuPattern.test(value.sku) ? value.sku : "";
  for (const step of optionalQuerySteps) {
    const enabled = state.capabilities?.plugins?.[step]?.enabled === true;
    const input = $("#query-" + step);
    input.disabled = !enabled;
    input.checked = enabled && !(Array.isArray(value?.skip_query_steps) && value.skip_query_steps.includes(step));
    $("#query-" + step + "-state").textContent = enabled ? "" : "服务端未启用";
  }
}
function queryPreferences() {
  const selection = Number($("#top-k").value);
  return { top_k: topKChoices.includes(selection) ? selection : 5,
    skip_query_steps: optionalQuerySteps.filter(step =>
      state.capabilities?.plugins?.[step]?.enabled === true && !$("#query-" + step).checked) };
}
function saveQueryPreferences() {
  if (!state.user) return;
  const sku = $("#sku").value.trim();
  try {
    localStorage.setItem(preferenceKey(state.user), JSON.stringify({ ...queryPreferences(),
      sku: skuPattern.test(sku) ? sku : "" }));
  } catch {}
}
for (const id of ["top-k", "sku", ...optionalQuerySteps.map(step => "query-" + step)])
  $("#" + id).addEventListener("change", saveQueryPreferences);
$("#query-options-reset").addEventListener("click", () => {
  restoreQueryPreferences(true);
  saveQueryPreferences();
});

function showLogin(message = "") {
  state.sessionEpoch++;
  state.usersEpoch++;
  state.controller?.abort();
  state.controller = null;
  setRunning(false);
  rememberConversation(null);
  resetList();
  state.connected = false;
  state.user = null;
  state.capabilities = null;
  restoreQueryPreferences(true);
  clearData();
  $$("dialog[open]").forEach((node) => node.close());
  $("#users-list").replaceChildren();
  $("#user-create-form").reset();
  $("#users-error").textContent = "";
  $("#account-name").textContent = "";
  $(".account-avatar").textContent = "";
  clearTimeout(toastTimer);
  $("#toast").textContent = "";
  $("#toast").hidden = true;
  $("#account-role").textContent = "";
  $("#users-open").hidden = true;
  $("#knowledge-open").hidden = true;
  $("#evaluation-open").hidden = true;
  $("#account-domains").textContent = "";
  $("#app-shell").hidden = true;
  $("#boot-page").hidden = true;
  $("#login-page").hidden = false;
  $("#login-password").value = "";
  $("#login-error").textContent = message;
  renderPlugins();
}
function acceptSession(session) {
  if (!session?.subject_id || !session.tenant_id) throw new Error("服务未返回完整的登录身份。");
  state.sessionEpoch++;
  resetList();
  clearData();
  state.user = session;
  state.connected = true;
  state.capabilities = session.capabilities;
  restoreQueryPreferences();
  $("#account-name").textContent = session.username || session.subject_id;
  $(".account-avatar").textContent = (session.username || "U").slice(0, 1).toUpperCase();
  $("#account-role").textContent = session.admin ? "管理员" : "成员";
  $("#users-open").hidden = !session.admin;
  $("#knowledge-open").hidden = false;
  $("#evaluation-open").hidden = !session.admin;
  $("#account-domains").textContent = `数据权限：${RagDomains.text(session.data_domains)}`;
  RagDomains.load().then(() => {
    if (state.user !== session) return;
    $("#account-domains").textContent = `数据权限：${RagDomains.text(session.data_domains)}`;
    RagDomains.render($("#new-user-domains"), ["public"], {publicRequired: true});
  }).catch(error => { if (state.user === session) toast(error.message); });
  $("#login-password").value = "";
  $("#login-error").textContent = "";
  $("#boot-page").hidden = true;
  $("#login-page").hidden = true;
  $("#app-shell").hidden = false;
  renderPlugins();
  loadConversations();
  const saved = history.state?.ragConversation;
  if (saved?.subject === session.subject_id && saved.tenant === session.tenant_id && saved.id) openConversation(saved.id);
  else rememberConversation(null);
  $("#question").focus({ preventScroll: true });
}
$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.connecting) return;
  const username = $("#login-username").value.trim();
  const password = $("#login-password").value;
  if (!username || !password) return;
  const epoch = state.sessionEpoch;
  state.connecting = true;
  $("#login-submit").disabled = true;
  $("#login-error").textContent = "";
  try {
    const session = await (await api("/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    })).json();
    notifyOtherTabs();
    if (epoch === state.sessionEpoch) acceptSession(session);
  } catch (error) {
    if (epoch === state.sessionEpoch) $("#login-error").textContent = error.message;
  } finally {
    state.connecting = false;
    $("#login-submit").disabled = false;
  }
});
$("#logout-button").addEventListener("click", async () => {
  if (state.connecting) return;
  const owner = state.user;
  // Hide account data immediately; wait for cookie invalidation before another login.
  showLogin();
  state.connecting = true;
  $("#login-submit").disabled = true;
  try {
    await api("/auth/logout", { method: "POST" }, owner);
    notifyOtherTabs();
    toast("已退出登录。");
  } catch (error) {
    $("#login-error").textContent = `退出请求未完成：${error.message} 请刷新页面后重试退出。`;
  } finally {
    state.connecting = false;
    $("#login-submit").disabled = false;
  }
});
// The page opens on a neutral "connecting" screen. The login form appears only when the server
// answers 401, so a request that fails for a passing reason never passes for a signed-out user.
function showBoot(message, final = false) {
  $("#app-shell").hidden = true;
  $("#login-page").hidden = true;
  $("#boot-page").hidden = false;
  $("#boot-message").textContent = message;
  $("#boot-actions").classList.toggle("shown", final);
}
// Network failure (0), the gateway's rate limit (429) or a service that is restarting (502 to 504).
const passingStatuses = [0, 429, 502, 503, 504];
const retryDelays = [2000, 4000, 8000];

async function restoreSession() {
  const epoch = state.sessionEpoch;
  state.connecting = true;
  $("#login-submit").disabled = true;
  try {
    if (!window.RagDomains) {
      showBoot("页面脚本没有加载完整，请重新加载页面。", true);
      return;
    }
    for (let attempt = 0; ; attempt++) {
      let session;
      try {
        session = await (await api("/console/session")).json();
      } catch (error) {
        if (epoch !== state.sessionEpoch) return;
        if (error.status === 401) return showLogin();
        if (!passingStatuses.includes(error.status) || attempt === retryDelays.length)
          return showBoot(`暂时无法连接工作台：${error.message}`, true);
        showBoot(`服务暂时繁忙，正在自动重试（${attempt + 1}/${retryDelays.length}）…`);
        await new Promise((resolve) => setTimeout(resolve, retryDelays[attempt]));
        if (epoch !== state.sessionEpoch) return;
        continue;
      }
      if (epoch !== state.sessionEpoch) return;
      try {
        acceptSession(session);
      } catch (error) {
        // acceptSession has already moved the session epoch on, so an unreported error would leave this screen.
        showBoot(`工作台初始化失败：${error.message}`, true);
      }
      return;
    }
  } finally {
    state.connecting = false;
    $("#login-submit").disabled = false;
  }
}

async function loadUsers() {
  if (!state.user?.admin) return;
  const session = state.sessionEpoch;
  const epoch = ++state.usersEpoch;
  $("#users-error").textContent = "";
  $("#users-list").replaceChildren(el("p", "muted", "正在读取账号…"));
  try {
    await RagDomains.load();
    if (session !== state.sessionEpoch || epoch !== state.usersEpoch) return;
    const data = await (await api("/admin/users")).json();
    if (session !== state.sessionEpoch || epoch !== state.usersEpoch) return;
    $("#users-list").replaceChildren();
    for (const user of data.users || []) {
      const row = el("div", "user-row");
      const info = el("div");
      info.append(el("strong", "", user.username));
      const date = user.created_at ? new Date(user.created_at).toLocaleString("zh-CN") : "";
      info.append(el("p", "", `${user.admin ? "管理员" : "普通成员"}${date ? ` · ${date}` : ""}`));
      info.append(el("p", "user-domain-summary", `数据权限：${RagDomains.text(user.data_domains)}`));
      const actions = el("div", "user-actions");
      if (user.admin || user.username === "ragadmin") actions.append(el("span", "fixed-user", "固定管理员"));
      else {
        const edit = el("button", "permission-edit", "修改权限");
        edit.type = "button";
        edit.setAttribute("aria-label", `修改账号 ${user.username} 的数据权限`);
        edit.addEventListener("click", () => {
          const editor = el("div", "user-domain-editor");
          const choices = el("div");
          choices.setAttribute("role", "group");
          choices.setAttribute("aria-label", `账号 ${user.username} 的数据访问权限`);
          RagDomains.render(choices, user.data_domains || [], {publicRequired: true});
          const save = el("button", "permission-edit", "保存权限");
          save.type = "button";
          const cancel = el("button", "delete-cancel", "取消");
          cancel.type = "button";
          cancel.addEventListener("click", () => { editor.remove(); edit.disabled = false; });
          save.addEventListener("click", async () => {
            save.disabled = true; cancel.disabled = true;
            $("#users-error").textContent = "";
            try {
              await api(`/admin/users/${encodeURIComponent(user.username)}/domains`, {
                method: "PATCH", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({data_domains: RagDomains.values(choices)}),
              });
              if (session !== state.sessionEpoch) return;
              toast(`账号 ${user.username} 的权限已更新，后续请求立即生效。`);
              await loadUsers();
            } catch (error) {
              if (session === state.sessionEpoch) $("#users-error").textContent = error.message;
              save.disabled = false; cancel.disabled = false;
            }
          });
          editor.append(choices, save, cancel);
          info.append(editor); edit.disabled = true;
        });
        actions.append(edit);
        const remove = el("button", "", "删除");
        remove.type = "button";
        remove.setAttribute("aria-label", `删除账号 ${user.username}`);
        remove.addEventListener("click", () => {
          const confirm = el("button", "delete-confirm", "确认删除");
          confirm.type = "button";
          confirm.setAttribute("aria-label", `确认删除账号 ${user.username}`);
          const cancel = el("button", "delete-cancel", "取消");
          cancel.type = "button";
          cancel.addEventListener("click", () => actions.replaceChildren(edit, remove));
          confirm.addEventListener("click", async () => {
            confirm.disabled = true; cancel.disabled = true;
            $("#users-error").textContent = "";
            try {
              await api(`/admin/users/${encodeURIComponent(user.username)}`, { method: "DELETE" });
              if (session !== state.sessionEpoch) return;
              toast(`账号 ${user.username} 已删除。`);
              await loadUsers();
            } catch (error) {
              if (session === state.sessionEpoch) {
                $("#users-error").textContent = error.message;
                confirm.disabled = false; cancel.disabled = false;
              }
            }
          });
          actions.replaceChildren(confirm, cancel);
          confirm.focus();
        });
        actions.append(remove);
      }
      row.append(info, actions); $("#users-list").append(row);
    }
  } catch (error) {
    if (session === state.sessionEpoch && epoch === state.usersEpoch) {
      $("#users-list").replaceChildren();
      $("#users-error").textContent = error.message;
    }
  }
}
$("#users-open").addEventListener("click", () => {
  if (!state.user?.admin) return;
  $("#users-error").textContent = "";
  dialog("#users-dialog");
  loadUsers();
});
$("#user-create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.user?.admin || $("#user-create-submit").disabled) return;
  const username = $("#new-username").value.trim();
  const password = $("#new-password").value;
  if (!username || !password) return;
  const session = state.sessionEpoch;
  $("#user-create-submit").disabled = true;
  $("#users-error").textContent = "";
  try {
    await RagDomains.load();
    if (session !== state.sessionEpoch) return;
    await api("/admin/users", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password, data_domains: RagDomains.values($("#new-user-domains")) }) });
    if (session !== state.sessionEpoch) return;
    $("#user-create-form").reset();
    RagDomains.render($("#new-user-domains"), ["public"], {publicRequired: true});
    toast(`账号 ${username} 已创建。`);
    await loadUsers();
  } catch (error) {
    if (session === state.sessionEpoch) $("#users-error").textContent = error.message;
  } finally {
    $("#user-create-submit").disabled = false;
  }
});

function renderPlugins() {
  const plugins = state.capabilities?.plugins;
  $("#plugin-summary").replaceChildren();
  $("#plugins-list").replaceChildren();
  if (!plugins) {
    $("#mode-label").textContent = "未连接";
    $("#plugin-summary").append(el("p", "muted", "连接后读取实际插件配置。"));
    $("#plugins-list").textContent = "连接后查看实际配置。";
    return;
  }
  $("#mode-label").textContent =
    state.capabilities.mode === "cloud" ? "云端已接入" : "无模型模式";
  for (const role of [
    "decision",
    "planner",
    "lexical",
    "dense",
    "reranker",
    "generator",
    "verifier",
  ]) {
    const plugin = plugins[role];
    if (!plugin) continue;
    const row = el("div", "plugin-line");
    row.append(el("span", "", roleNames[role]));
    const value = el("span", "plugin-value");
    const dot = el("span", "status-dot");
    if (!plugin.enabled) dot.style.background = "#b9c0ad";
    value.append(
      dot,
      document.createTextNode(
        plugin.plugin || plugin.name || plugin.selector || "—",
      ),
    );
    row.append(value);
    $("#plugin-summary").append(row);
  }
  for (const [role, plugin] of Object.entries(plugins)) {
    const tile = el("div", "plugin-tile");
    tile.append(
      el("strong", "", roleNames[role] || role),
      el(
        "code",
        "",
        `${role} / ${plugin.plugin || plugin.name || plugin.selector || "—"}`,
      ),
      el(
        "span",
        "",
        `${plugin.enabled ? "已启用" : "未启用"}${plugin.requires_model ? " · 云端模型" : ""}`,
      ),
    );
    $("#plugins-list").append(tile);
  }
}

function setRunning(value) {
  state.running = value;
  $("#run-button").disabled = value;
  $("#run-button span").textContent = value ? "正在检索…" : "发送问题";
  $("#cancel-button").hidden = !value;
  $("#new-chat").disabled = value;
  for (const button of $$("#conversation-list button, #chat-history button")) button.disabled = value;
  $("#timeline-dot").classList.toggle("running-dot", value);
  clearInterval(state.timer);
  if (value)
    state.timer = setInterval(() => {
      if (state.running) {
        $("#metric-duration").replaceChildren(
          document.createTextNode(number(performance.now() - state.started)),
          el("small", "", " ms"),
        );
        $("#duration-caption").textContent = "页面等待耗时 · 实时";
      }
    }, 150);
}
function resetCurrent() {
  state.viewEpoch++;
  figureObserver.disconnect();
  for (const url of figureUrls) URL.revokeObjectURL(url);
  figureUrls.clear();
  state.sourceEpoch++;
  $("#source-content").replaceChildren();
  if ($("#source-dialog").open) $("#source-dialog").close();
  $("#current-chat").hidden = false;
  state.stages = [];
  state.trace = null;
  state.result = null;
  state.traceId = "";
  state.selected = null;
  state.activeQuestion = "";
  $("#chat-question").textContent = "";
  $("#question-message").hidden = true;
  $("#timeline").replaceChildren();
  $("#inspector-body").replaceChildren(
    el("div", "plain-empty", "步骤开始后，点击查看详情。"),
  );
  $("#inspector-heading").textContent = "步骤详情";
  $("#inspector-subtitle").textContent = "选择左侧步骤，查看实际返回。";
  $("#raw-data").textContent = "等待服务器返回实际运行数据…";
  $("#export-button").disabled = true;
  $("#metric-duration").replaceChildren(
    document.createTextNode("—"),
    el("small", "", " ms"),
  );
  $("#metric-stages").textContent = "0";
  $("#metric-lanes").textContent = "—";
  $("#metric-evidence").textContent = "—";
  $("#duration-caption").textContent = "等待服务器执行";
  $("#stages-caption").textContent = "按实际运行记录";
  $("#stage-count").textContent = "0";
  $("#evidence-count").textContent = "0";
  $("#run-status").textContent = "连接中";
  $("#timeline-heading").textContent = "等待服务器开始执行";
  empty($("#panel-answer"), "查询执行中，核验完成后展示回答。");
  empty($("#panel-evidence"), "查询执行中，权限复核后展示证据。");
  empty($("#panel-models"), "查询完成后展示实际模型调用。");
  $$(".pipeline-map>div").forEach((node) =>
    node.classList.remove("done", "running"),
  );
  notice();
  switchTab("inspect");
}
function clearData() {
  resetCurrent();
  renderWelcome();
  empty($("#panel-evidence"), "开始检索后，查看授权引用证据。");
  empty($("#panel-models"), "开始检索后，查看实际模型调用。");
  state.conversationId = null;
  state.messages = [];
  state.restoring = false;
  $("#run-button").disabled = false;
  $("#chat-history").replaceChildren();
  $("#question").value = "";
  $("#char-count").textContent = "0 / 4000";
  $("#run-status").textContent = "待运行";
  $("#timeline-heading").textContent = "准备就绪，等待一个好问题";
  $("#raw-data").textContent = "尚无运行数据。";
  $("#timeline").replaceChildren(
    el("div", "plain-empty", "输入问题，开始追踪一次真实检索。"),
  );
}

$("#question").addEventListener("input", () => {
  $("#char-count").textContent = `${$("#question").value.length} / 4000`;
});
$("#question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    $("#query-form").requestSubmit();
  }
});
$("#query-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.running || state.restoring) return;
  if (state.connecting) {
    toast("正在验证连接，请稍候。");
    return;
  }
  if (!state.connected) {
    showLogin("请先登录工作台。");
    return;
  }
  const question = $("#question").value.trim();
  if (!question) {
    toast("请输入一个非空问题。");
    return;
  }
  const sku = $("#sku").value.trim();
  if (sku && !skuPattern.test(sku)) {
    toast("SKU 仅支持字母、数字、下划线、点、冒号和连字符。");
    return;
  }
  resetCurrent();
  state.activeQuestion = question;
  showQuestion(question);
  renderChatHistory();
  revealCurrentReply();
  $("#question").value = "";
  $("#char-count").textContent = "0 / 4000";
  state.started = performance.now();
  setRunning(true);
  const controller = new AbortController();
  state.controller = controller;
  const epoch = state.viewEpoch;
  const session = state.sessionEpoch;
  const current = () =>
    epoch === state.viewEpoch && session === state.sessionEpoch;
  let terminal = false;
  const { top_k, skip_query_steps } = queryPreferences();
  saveQueryPreferences();
  try {
    const response = await api("/query/stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify({
        question,
        top_k,
        ...(skip_query_steps.length ? { skip_query_steps } : {}),
        persist_conversation: true,
        ...(state.conversationId ? { conversation_id: state.conversationId } : {}),
        ...(sku ? { sku } : {}),
      }),
      signal: controller.signal,
    });
    if (!response.headers.get("content-type")?.includes("text/event-stream"))
      throw new Error("服务器没有返回实时链路，请检查服务版本。");
    await readStream(response, (type, data) => {
      if (!current()) return;
      if (type === "trace") {
        state.traceId = data.trace_id;
      } else if (type === "stage") {
        acceptStage(data);
      } else if (type === "result") {
        terminal = true;
        applyConfirmedTurn(data.result, data.trace);
      } else if (type === "error") {
        terminal = true;
        const code = data.code || data.error?.code;
        if (code === "unauthorized") {
          showLogin("登录已失效，请重新登录。");
          return;
        }
        if (code === "not_found" || code === "forbidden") {
          inaccessibleConversation();
          return;
        }
        if (code === "conversation_full") throw Object.assign(new Error("这段对话已达到轮数上限，请新建对话。"), {code});
        state.traceId = data.trace_id || data.trace?.trace_id || state.traceId;
        if (data.trace) {
          applyConfirmedTurn(data.trace.final_result || null, data.trace);
        }
        throw new Error(data.message || "查询执行失败。");
      }
    });
    if (current() && !terminal)
      throw new Error(
        "连接提前结束，尚未收到最终结果。请从历史对话查看执行记录。",
      );
  } catch (error) {
    if (!current()) return;
    if ([403, 404].includes(error.status)) return inaccessibleConversation();
    if (error.code === "conversation_full" && state.conversationId) {
      // A competing turn filled the group. This run has no stored trace;
      // restore only what is still readable, including the winning turn.
      state.controller = null;
      setRunning(false);
      loadConversations();
      return openConversation(state.conversationId, "这段对话已达到轮数上限，本轮未保存。请新建对话。");
    }
    let aborted = error.name === "AbortError" || controller.signal.aborted;
    let diagnosticLoadError = "";
    // Read-only recovery also supports older servers and an interrupted SSE
    // connection. It never repeats the paid query.
    if (!aborted && state.traceId && !state.trace) {
      try {
        const trace = await (
          await api(`/traces/${encodeURIComponent(state.traceId)}`, {
            signal: controller.signal,
          })
        ).json();
        if (!current()) return;
        if (trace.trace_id !== state.traceId) throw new Error("执行记录不匹配。");
        applyConfirmedTurn(trace.final_result || null, trace);
      } catch (loadError) {
        if (!current()) return;
        if ([403, 404].includes(loadError.status)) return inaccessibleConversation();
        aborted = loadError.name === "AbortError" || controller.signal.aborted;
        if (!aborted)
          diagnosticLoadError = ` 执行记录暂不可读取：${loadError.message}`;
      }
    }
    if (!current()) return;
    if (state.trace) {
      const failed = failedStatuses.includes(state.trace.status);
      if (failed) notice(`${failureMessage()}${diagnosticLoadError}`, "error");
      else
        notice(
          "实时连接中断后，已按当前权限恢复完整执行记录。请查看实际结果。",
        );
      return;
    }
    $("#run-status").textContent = aborted ? "已停止" : "执行失败";
    $("#timeline-heading").textContent = aborted ? "查询已停止" : "执行未完成";
    notice(
      aborted
        ? "已停止等待并取消查询。已完成的步骤保留在时间线中。"
        : `${error.message}${diagnosticLoadError}`,
      aborted ? "" : "error",
    );
    for (const stage of state.stages) {
      if (stage.status === "running")
        stage.status = aborted ? "cancelled" : "error";
    }
    renderTimeline();
    state.selected = state.stages.findLastIndex((stage) =>
      failedStatuses.includes(stage.status),
    );
    renderInspector(state.selected);
    empty(
      $("#panel-answer"),
      aborted
        ? "查询已停止，未获得核验完成的回答。"
        : "查询执行失败，未返回最终回答。请查看失败步骤。",
    );
    empty(
      $("#panel-evidence"),
      "本次未获得最终授权证据。已完成步骤的记录可通过 Trace 查看。",
    );
    empty(
      $("#panel-models"),
      "未能读取完整调用记录。请从历史对话重新打开本轮流程。",
    );
    $("#duration-caption").textContent = "页面等待耗时";
  } finally {
    if (state.controller === controller) {
      setRunning(false);
      state.controller = null;
      if (current()) {
        if (state.traceId && (state.trace || state.result)) {
          state.messages.push({trace_id: state.traceId, question,
            answer: state.result?.answer, message: state.result?.message,
            status: state.result?.status || state.trace?.status || (controller.signal.aborted ? "cancelled" : "error")});
        }
        loadConversations();
        revealCurrentReply();
      }
    }
  }
});
$("#cancel-button").addEventListener("click", () => state.controller?.abort());

async function readStream(response, consume) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      buffer += done
        ? decoder.decode()
        : decoder.decode(value, { stream: true });
      if (buffer.length > 8_000_000)
        throw new Error("单次事件过大，已停止展示。");
      let match;
      while ((match = /\r?\n\r?\n/.exec(buffer))) {
        const block = buffer.slice(0, match.index);
        buffer = buffer.slice(match.index + match[0].length);
        let type = "message";
        const parts = [];
        for (const line of block.split(/\r?\n/)) {
          if (line.startsWith("event:")) type = line.slice(6).trim();
          else if (line.startsWith("data:"))
            parts.push(line.slice(5).replace(/^ /, ""));
        }
        if (parts.length) consume(type, JSON.parse(parts.join("\n")));
      }
      if (done) {
        if (buffer.trim() && !buffer.trim().startsWith(":"))
          throw new Error("实时事件不完整。");
        break;
      }
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

function acceptStage(data) {
  const index = Number.isInteger(data.index) ? data.index : state.stages.length;
  state.stages[index] = { ...state.stages[index], ...data };
  if (state.selected === null) state.selected = index;
  $("#run-status").textContent = "执行中";
  $("#timeline-heading").textContent =
    data.status === "running"
      ? `正在${nameFor(data.name)}`
      : `${nameFor(data.name)} · ${statuses[data.status] || data.status}`;
  renderTimeline();
  renderMetrics();
  if (state.selected === index) renderInspector(index);
}
function phase(name) {
  if (/^(planner|rewrite|multi_query|hyde|decision.preflight)/.test(name))
    return "plan";
  if (/^(snapshot|authorized_chunks|lexical|dense|gap|decision.gap)/.test(name))
    return "retrieve";
  if (/^(fusion|reranker|relevance|parent_backfill|judge)/.test(name)) return "rank";
  if (/^(generator|decision.generate)/.test(name)) return "generate";
  return "verify";
}
function renderTimeline() {
  const target = $("#timeline");
  const scroll = target.scrollTop;
  target.replaceChildren();
  state.stages.forEach((stage, index) => {
    if (!stage) return;
    if (
      !$("#show-disabled").checked &&
      ["disabled", "skipped"].includes(stage.status)
    )
      return;
    const row = el("div", "timeline-row");
    row.dataset.status = stage.status;
    row.append(
      el(
        "span",
        "stage-node",
        stage.status === "ok"
          ? "✓"
          : ["disabled", "skipped"].includes(stage.status)
            ? "–"
            : String(index + 1).padStart(2, "0"),
      ),
    );
    const button = el(
      "button",
      `stage-button${state.selected === index ? " selected" : ""}`,
    );
    button.type = "button";
    button.setAttribute(
      "aria-label",
      `${index + 1}. ${nameFor(stage.name)} ${statuses[stage.status] || stage.status} ${ms(stage.duration_ms)}`,
    );
    const main = el("span", "stage-main");
    main.append(
      el("span", "stage-name", nameFor(stage.name)),
      el(
        "span",
        "stage-code",
        `${stage.name} · ${statuses[stage.status] || stage.status}`,
      ),
    );
    button.append(
      main,
      el(
        "span",
        "stage-time",
        stage.status === "running" ? "运行中" : ms(stage.duration_ms),
      ),
      el("span", "stage-arrow", "↗"),
    );
    button.addEventListener("click", () => {
      state.selected = index;
      renderTimeline();
      renderInspector(index);
      switchTab("inspect");
    });
    row.append(button);
    target.append(row);
  });
  if (!target.childElementCount)
    target.append(
      el(
        "div",
        "plain-empty",
        state.stages.length ? "当前筛选下没有步骤。" : "等待实际步骤…",
      ),
    );
  target.scrollTop = scroll;
  $$(".pipeline-map>div").forEach((node) => {
    const rows = state.stages.filter(
      (stage) =>
        stage &&
        !stage.name.startsWith("decision.") &&
        phase(stage.name) === node.dataset.phase,
    );
    node.classList.toggle(
      "running",
      rows.some((stage) => stage.status === "running"),
    );
    node.classList.toggle(
      "done",
      rows.some((stage) => stage.status === "ok") &&
        !rows.some((stage) => stage.status === "running"),
    );
  });
}
$("#show-disabled").addEventListener("change", renderTimeline);
function renderMetrics() {
  const complete = state.stages.filter(
    (stage) => stage && stage.status !== "running",
  ).length;
  $("#metric-stages").textContent = String(state.stages.length);
  $("#stages-caption").textContent = `${complete} 步已结束`;
  $("#stage-count").textContent = String(state.stages.length);
  if (state.trace) {
    $("#metric-duration").replaceChildren(
      document.createTextNode(number(state.trace.duration_ms)),
      el("small", "", " ms"),
    );
    $("#duration-caption").textContent = "服务端实测 · 含链路处理";
    $("#metric-lanes").textContent = number(
      state.trace.lane_count ?? state.trace.retrieval_lanes?.length ?? 0,
    );
    $("#metric-evidence").textContent = number(
      state.result?.evidence?.length ?? state.trace.evidence?.length ?? 0,
    );
  } else {
    $("#metric-lanes").textContent = String(
      state.stages.filter((stage) =>
        /^(lexical|dense)\./.test(stage?.name || ""),
      ).length,
    );
  }
}

function applyRun(result, trace, { transcript = true } = {}) {
  clearInterval(state.timer);
  state.result = result || null;
  state.trace = trace || null;
  state.stages = trace?.stages || state.stages;
  state.traceId = result?.trace_id || trace?.trace_id || state.traceId;
  state.activeQuestion = trace?.question || state.activeQuestion;
  if (transcript) {
    showQuestion(state.activeQuestion);
    renderChatHistory();
  }
  $("#export-button").disabled = false;
  const status = result?.status || trace?.status || "ok";
  $("#run-status").textContent = statuses[status] || status;
  $("#timeline-heading").textContent =
    `执行结束 · ${statuses[status] || status}`;
  state.selected = state.stages.findLastIndex(
    (stage) => !["disabled", "skipped"].includes(stage.status),
  );
  const failedIndex = state.stages.findLastIndex((stage) =>
    failedStatuses.includes(stage.status),
  );
  if (failedIndex >= 0) state.selected = failedIndex;
  renderTimeline();
  renderMetrics();
  renderInspector(state.selected);
  if (transcript) renderAnswer();
  renderEvidence();
  renderModels();
  $("#raw-data").textContent = json({
    result: state.result,
    trace: state.trace,
  });
  if (
    [
      "partial",
      "no_index",
      "empty",
      "clarify",
      "capability_unavailable",
    ].includes(status)
  )
    notice(result?.message || `本次结果：${statuses[status]}`);
  else if (failedStatuses.includes(status))
    notice(failureMessage(trace), "error");
  else notice();
  if (transcript) revealCurrentReply();
}

function renderInspector(index) {
  const stage = state.stages[index];
  if (!stage) return;
  $("#inspector-heading").textContent = nameFor(stage.name);
  $("#inspector-subtitle").textContent =
    `步骤 ${String(index + 1).padStart(2, "0")} / ${statuses[stage.status] || stage.status}`;
  const target = $("#inspector-body");
  target.replaceChildren(
    el("p", "inspector-code", stage.name),
    el("p", "inspector-description", descriptionFor(stage.name)),
  );
  const meta = el("div", "inspector-meta");
  for (const [label, value] of [
    ["实际耗时", ms(stage.duration_ms)],
    ["开始偏移", ms(stage.started_ms)],
    ["执行状态", statuses[stage.status] || stage.status],
    ["结果来源", "服务器 Trace"],
  ]) {
    const item = el("div");
    item.append(el("span", "", label), el("strong", "", value));
    meta.append(item);
  }
  target.append(meta);
  if (stage.error_type)
    target.append(el("p", "output-summary", `异常类型：${stage.error_type}`));
  if (failedStatuses.includes(stage.status)) {
    const detail = failureDetails(stage);
    target.append(el("p", "run-notice error", detail.message));
    target.append(el("p", "stage-code", `诊断代码：${detail.code}`));
  }
  if (stage.reason)
    target.append(
      el(
        "p",
        "output-summary",
        `${["disabled", "skipped"].includes(stage.status) ? "跳过原因" : "处理原因"}：${stage.reason === "user_preference" ? "已在个人检索设置中关闭" : stage.reason === "intent_not_selected" ? "本轮意图无需此步骤" : stage.reason}`,
      ),
    );
  const section = el("div", "inspector-section");
  section.append(el("h3", "", "结构化返回结果"));
  if (stage.output !== undefined) {
    renderStageOutput(section, stage.output);
  } else
    section.append(
      el(
        "p",
        "output-summary",
        stage.status === "running"
          ? "步骤正在执行，服务端计时中。"
          : failedStatuses.includes(stage.status)
            ? "本步骤未正常完成，没有有效返回。此前已完成步骤的返回内容保留在执行链路中。"
            : ["disabled", "skipped"].includes(stage.status)
              ? "本次没有调用此步骤。"
              : state.trace
                ? "此 Trace 未记录该步骤的返回内容。"
                : "查询完成并通过权限复核后，展示返回内容。",
      ),
    );
  target.append(section);
}

function renderStageOutput(target, output) {
  if (output?.checkpoint === "preflight" && output.route) {
    const routes = {kb: "检索知识库", exact: "按标识符查资料", realtime: "实时业务查询", direct: "简单对话", handoff: "请求人工", clarify: "需要澄清"};
    const sources = {deepseek: "DeepSeek 意图判断", local_guard: "本地规则", rule_fallback: "意图服务不可用，已回退到规则"};
    const steps = {rewrite: "改写", decompose: "拆题", multi_query: "多路扩展", hyde: "HyDE"};
    target.append(el("p", "output-summary", `${sources[output.source] || "意图判断"}：${routes[output.route] || output.route}`));
    if (output.source === "rule_fallback" && output.fallback_reason) {
      const reasons = {cloud_timeout: "等待意图服务超时", cloud_http_error: "意图服务返回 HTTP 错误",
        cloud_network_error: "无法连接意图服务", capability_unavailable: "未获得可用的分类结果"};
      target.append(el("p", "output-summary", `回退原因：${reasons[output.fallback_reason] || "意图判断未完成"}。`));
    }
    if (["kb", "exact"].includes(output.route)) {
      const selected = (output.selected_steps || []).map(step => steps[step] || step);
      target.append(el("p", "output-summary", selected.length ? `建议：${selected.join("、")}。实际执行仍受个人设置和服务端预算限制。` : "按原问题检索，不调用额外的查询增强。"));
    }
  }
  const items = output?.hits || output?.parents || output?.queries;
  if (output?.query) target.append(el("p", "output-query", output.query));
  if (Array.isArray(items)) {
    target.append(el("p", "output-summary", `实际返回 ${items.length} 条记录`));
    const list = el("div", "output-items");
    items.forEach((item, index) => {
      const card = el("div", "output-item");
      const caption = el("div", "output-item-top");
      caption.append(
        el("span", "", item.query_id || `#${item.rank ?? index + 1}`),
        el(
          "span",
          "",
          item.score !== undefined ? `score ${number(item.score)}` : "",
        ),
      );
      card.append(caption);
      if (item.document_id)
        card.append(el("p", "output-item-title", item.document_id));
      if (item.text_preview || item.text)
        card.append(
          el("p", "output-item-text", item.text_preview || item.text),
        );
      if (item.preview_truncated)
        card.append(
          el(
            "p",
            "output-item-note",
            "预览已截取；完整授权证据见「证据」视图。",
          ),
        );
      if (item.chunk_id || item.parent_id)
        card.append(el("p", "stage-code", item.chunk_id || item.parent_id));
      list.append(card);
    });
    target.append(list);
    const details = el("details", "output-details");
    details.append(
      el("summary", "", "查看完整结构化数据"),
      el("pre", "json-view", json(output)),
    );
    target.append(details);
  } else target.append(el("pre", "json-view", json(output)));
}

// Compact history has only the exported answer string. Match the suffix emitted
// by make_evidence + CloudGenerator, never arbitrary numbers or bracketed text.
function legacyParagraphs(answer) {
  const paragraphs = [];
  let lines = [];
  for (const line of answer.split(/\r?\n/)) {
    const suffix = line.match(/(?:[ \t]+\[[0-9a-f]{64}\.\d+\.[0-9a-f]{16}\])+[ \t]*$/);
    lines.push(suffix ? line.slice(0, suffix.index) : line);
    if (suffix) {
      paragraphs.push({text: lines.join("\n"), evidence_ids: [...suffix[0].matchAll(/\[([^\]]+)\]/g)].map(m => m[1])});
      lines = [];
    }
  }
  if (lines.some(line => line.trim())) paragraphs.push({text: lines.join("\n"), evidence_ids: []});
  return paragraphs;
}

function answerCitation(item, position) {
  const button = el("button", "citation-button", "来源 " + position);
  button.type = "button";
  button.title = [item.document_id, ...(item.heading_path || [])].filter(Boolean).join(" / ");
  button.setAttribute("aria-label", "查看来源 " + position + " 的原文");
  button.addEventListener("click", () => showSource(item));
  return button;
}

function answerReader(result) {
  const reader = el("div", "answer-reader");
  const evidence = new Map((result.evidence || []).map((item, i) => [item.evidence_id, {item, number: i + 1}]));
  const paragraphs = result.claims?.length ? result.claims : legacyParagraphs(result.answer || "");
  for (const claim of paragraphs) {
    const paragraph = el("div", "answer-paragraph");
    const visuals = globalThis.RagAnswerVisuals;
    paragraph.append(visuals?.prose(claim.text) || el("p", "", claim.text));
    const citations = el("div", "answer-citations");
    for (const id of new Set(claim.evidence_ids || [])) {
      const source = evidence.get(id);
      if (source) citations.append(answerCitation(source.item, source.number));
    }
    if (citations.childElementCount) paragraph.append(citations);
    reader.append(paragraph);
  }
  const charts = globalThis.RagAnswerVisuals?.charts(result, answerCitation);
  if (charts) reader.append(charts);
  return reader;
}

// Only the final, access-checked result can supply passages. Compact history and
// intermediate trace candidates are not a substitute for an authorized result.
function insufficientReply(result) {
  const section = el("section", "closest-passages");
  const evidence = result.evidence || [];
  if (!evidence.length) {
    section.append(el("p", "history-answer", "本轮证据不足，未生成答案。没有可展示的授权资料。"));
    return section;
  }
  // Older saved results included a UI promise in the status message.
  const reason = result.message?.replace(/；列出的是最接近的资料。$/, "。");
  section.append(el("p", "history-answer", reason || noAnswerNotes.insufficient),
    el("h3", "", `相关资料 · ${evidence.length} 个片段（仅供参考）`));
  const list = el("ol");
  for (const item of evidence) {
    const row = el("li");
    const title = item.document_id || "未命名文档";
    row.append(el("strong", "", title));
    const path = [...(item.heading_path || []), item.locator].filter(Boolean).join(" / ");
    if (path) row.append(el("p", "evidence-path", path));
    const text = withoutFigureLines(item.text).trim();
    if (text) row.append(el("p", "closest-excerpt", text.length > 200 ? text.slice(0, 200) + "…" : text));
    const open = el("button", "source-button", "查看原文 ↗");
    open.type = "button";
    open.setAttribute("aria-label", `查看第 ${list.childElementCount + 1} 个片段的原文：${title}`);
    open.addEventListener("click", () => showSource(item));
    row.append(open); list.append(row);
  }
  section.append(list);
  return section;
}

function renderAnswer() {
  const target = $("#panel-answer");
  target.replaceChildren();
  const result = state.result;
  if (!result) {
    empty(
      target,
      failedStatuses.includes(state.trace?.status)
        ? "本次执行未完成，没有生成核验通过的最终回答。请查看失败步骤；此前完成的检索结果仍可逐步查看。"
        : "此历史 Trace 未记录最终回答，详情请查看步骤和证据。",
    );
    return;
  }
  if (result.status === "insufficient") {
    target.append(insufficientReply(result));
    return;
  }
  target.append(
    el(
      "div",
      "answer-status",
      `${statuses[result.status] || result.status} · ${result.message || ""}`,
    ),
  );
  if (result.answer) target.append(answerReader(result));
  else
    target.append(
      el(
        "p",
        "muted",
        noAnswerNotes[result.status] || "本次没有返回可确认的最终答案。",
      ),
    );
  if (!result.answer && result.status === "evidence_preview") target.append(answerReader(result));
  // Figures of the passages the answer rests on. A closest-match list ("insufficient") is not an answer,
  // so its figures stay on the evidence cards.
  if (["answered", "partial", "evidence_preview"].includes(result.status)) {
    const cited = new Set(
      (result.claims || []).flatMap((claim) => claim.evidence_ids || []),
    );
    const seen = new Set();
    const figures = [];
    (result.evidence || []).forEach((item, index) => {
      if (cited.size && !cited.has(item.evidence_id)) return;
      for (const image of item.images || []) {
        if (seen.has(image.key)) continue;
        seen.add(image.key);
        figures.push(
          figureNode(item, image, `证据 E${String(index + 1).padStart(2, "0")}`),
        );
      }
    });
    if (figures.length) {
      const gallery = el("div", "figure-list");
      gallery.append(el("h3", "", "相关插图"), ...figures);
      target.append(gallery);
    }
  }
  // An empty result already says nothing was found; listing raw sub-question ids next to it is noise.
  if (result.missing_query_ids?.length && result.status !== "empty")
    target.append(
      el(
        "div",
        "missing-box",
        `仍需补充的子问题：${result.missing_query_ids.join("、")}`,
      ),
    );
}

function renderEvidence() {
  const evidence = state.result?.evidence || state.trace?.evidence || [];
  $("#evidence-count").textContent = String(evidence.length);
  const target = $("#panel-evidence");
  target.replaceChildren();
  if (!evidence.length) {
    empty(
      target,
      state.result?.status === "empty"
        ? "没有找到相同或相似的内容，所以没有可引用的来源。"
        : "本次没有可展示的授权证据。",
    );
    return;
  }
  if (state.result?.status === "insufficient")
    target.append(
      el("p", "missing-box", "以下是与问题最接近的资料，但不足以回答，请谨慎参考。"),
    );
  evidence.forEach((item, index) => {
    const card = el("article", "evidence-card");
    card.id = `evidence-${index}`;
    card.tabIndex = -1;
    const top = el("div", "evidence-top");
    top.append(
      el("h3", "", item.document_id || "未命名文档"),
      el("span", "evidence-number", `E${String(index + 1).padStart(2, "0")}`),
    );
    const bottom = el("div", "evidence-bottom");
    bottom.append(
      el(
        "span",
        "",
        `${item.version || "—"} · ${item.query_ids?.join(", ") || "证据"}${item.truncated ? " · 已按预算截取" : ""}`,
      ),
    );
    const button = el("button", "source-button", "查看原文 ↗");
    button.type = "button";
    button.addEventListener("click", () => showSource(item));
    bottom.append(button);
    card.append(
      top,
      el(
        "p",
        "evidence-path",
        [...(item.heading_path || []), item.locator]
          .filter(Boolean)
          .join(" / "),
      ),
      el("p", "evidence-text", withoutFigureLines(item.text)),
    );
    if (item.images?.length) {
      const figures = el("div", "evidence-figures");
      figures.append(...item.images.map((image) => figureNode(item, image)));
      card.append(figures);
    }
    card.append(bottom);
    const details = el("details");
    details.append(
      el("summary", "", "引用元数据"),
      el(
        "pre",
        "",
        json(
          Object.fromEntries(
            Object.entries(item).filter(([key]) => key !== "text"),
          ),
        ),
      ),
    );
    card.append(details);
    target.append(card);
  });
}
async function showSource(item) {
  dialog("#source-dialog");
  const target = $("#source-content");
  const epoch = ++state.sourceEpoch;
  const session = state.sessionEpoch;
  target.replaceChildren(el("p", "muted", "正在回源并复核当前权限…"));
  try {
    const generation =
      item.generation || state.result?.generation || state.trace?.generation;
    const response = await api(
      `/evidence/${encodeURIComponent(generation)}/${encodeURIComponent(item.evidence_id)}`,
    );
    const data = await response.json();
    if (epoch !== state.sourceEpoch || session !== state.sessionEpoch) return;
    target.replaceChildren(
      el(
        "p",
        "source-meta",
        `${data.document_id} / ${data.version}\n${(data.heading_path || []).join(" / ")}\n${data.locator || ""}${data.truncated ? "\n按原查询证据预算截取，未包含整个文档。" : ""}`,
      ),
      el("div", "source-body", withoutFigureLines(data.text)),
    );
    if (data.images?.length) {
      const figures = el("div", "evidence-figures");
      figures.append(
        ...data.images.map((image) =>
          figureNode({ ...data, generation }, image),
        ),
      );
      target.append(figures);
    }
  } catch (error) {
    if (epoch !== state.sourceEpoch || session !== state.sessionEpoch) return;
    if ([403, 404].includes(error.status)) return inaccessibleConversation();
    target.replaceChildren(el("div", "form-error", `无法读取证据：${error.message}`));
  }
}

function renderModels() {
  const target = $("#panel-models");
  target.replaceChildren();
  const usage = state.trace?.usage || state.result?.capabilities?.usage;
  if (!usage) {
    empty(target, "此 Trace 未记录模型调用用量。");
    return;
  }
  const metrics = el("div", "usage-metrics");
  for (const [label, value, caption] of [
    ["模型请求", state.trace?.model_calls ?? usage.model_calls, "含失败与重试"],
    ["输入 Token", usage.input_tokens, "按平台返回记录"],
    ["输出 Token", usage.output_tokens, "按平台返回记录"],
  ]) {
    const item = el("div");
    item.append(
      el("span", "", label),
      el("strong", "", number(value)),
      el("span", "", caption),
    );
    metrics.append(item);
  }
  target.append(metrics);
  const calls = usage.calls || [];
  if (!calls.length) {
    target.append(el("div", "plain-empty", "本次没有执行模型调用。"));
    return;
  }
  const wrap = el("div", "table-scroll");
  const table = el("table", "data-table");
  const head = el("thead");
  const row = el("tr");
  for (const label of ["模型 / 接口", "尝试", "状态", "耗时"])
    row.append(el("th", "", label));
  head.append(row);
  const body = el("tbody");
  for (const call of calls) {
    const row = el("tr");
    const model = el("td");
    model.append(
      document.createTextNode(call.model || "—"),
      el("div", "stage-code", call.endpoint || ""),
    );
    row.append(
      model,
      el("td", "", call.attempt ?? "—"),
      el(
        "td",
        "",
        `${call.status || "—"}${call.http_status ? ` / ${call.http_status}` : ""}`,
      ),
      el("td", "", ms(call.duration_ms)),
    );
    if (call.error_code || call.error_type)
      row.children[2].append(
        el(
          "div",
          "output-summary",
          [call.error_code, call.error_type].filter(Boolean).join(" / "),
        ),
      );
    body.append(row);
  }
  table.append(head, body);
  wrap.append(table);
  target.append(wrap);
  const details = el("details");
  details.append(el("summary", "", "完整用量记录"), el("pre", "", json(usage)));
  target.append(details);
}

function switchTab(name) {
  state.tab = name;
  $$("[data-tab]").forEach((button) => {
    const active = button.dataset.tab === name;
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  $$(".tab-panel").forEach(
    (panel) => (panel.hidden = panel.id !== `panel-${name}`),
  );
}
$$("[data-tab]").forEach((button) => {
  button.addEventListener("click", () => switchTab(button.dataset.tab));
  button.addEventListener("keydown", (event) => {
    const tabs = $$("[data-tab]");
    const index = tabs.indexOf(button);
    let next;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft")
      next = (index + tabs.length - 1) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;
    event.preventDefault();
    switchTab(tabs[next].dataset.tab);
    tabs[next].focus();
  });
});
// Only an owner-scoped bookmark survives a reload; titles, answers and traces do not.
function rememberConversation(id) {
  const owner = state.user;
  history.replaceState({...history.state, ragConversation: id && owner ? {subject: owner.subject_id, tenant: owner.tenant_id, id} : null}, "");
}
function applyConfirmedTurn(result, trace) {
  // Only a terminal result or a readable stored trace confirms persistence.
  const id = trace?.conversation_id || result?.conversation_id || trace?.trace_id || result?.trace_id;
  if (!state.conversationId && id) {
    state.conversationId = id;
    rememberConversation(id);
  }
  applyRun(result, trace);
}
function resetList() {
  state.listEpoch++;
  state.conversations = [];
  state.nextOffset = null;
  $("#conversation-list").replaceChildren();
  $("#conversation-status").textContent = "";
  $("#conversation-more").hidden = true;
}
function renderConversations() {
  const list = $("#conversation-list");
  list.replaceChildren();
  for (const {conversation_id: id, title, updated_at} of state.conversations) {
    const button = el("button", "conversation-row");
    button.type = "button";
    button.disabled = state.running;
    button.setAttribute("aria-current", String(state.conversationId === id));
    button.title = title;
    button.append(el("span", "recent-title", title || "无标题对话"));
    const date = new Date(updated_at);
    if (updated_at && Number.isFinite(date.getTime())) {
      const time = el("time", "", date.toLocaleDateString("zh-CN", {month: "numeric", day: "numeric"}));
      time.dateTime = date.toISOString();
      time.title = date.toLocaleString("zh-CN");
      button.append(time);
    }
    button.addEventListener("click", () => openConversation(id));
    list.append(button);
  }
}
async function loadConversations(offset = 0) {
  if (!state.connected) return;
  if (!offset) resetList();
  const epoch = ++state.listEpoch;
  const status = $("#conversation-status");
  const more = $("#conversation-more");
  more.disabled = true;
  status.textContent = "正在读取对话…";
  try {
    const page = await (await api(`/conversations?limit=20&offset=${offset}`)).json();
    if (epoch !== state.listEpoch) return;
    if (!Array.isArray(page.items) || (page.has_more && !(Number.isSafeInteger(page.next_offset) && page.next_offset > offset)))
      throw new Error("对话列表返回格式不正确。");
    const unique = new Map(state.conversations.map(row => [row.conversation_id, row]));
    for (const row of page.items) unique.set(row.conversation_id, row);
    state.conversations = [...unique.values()];
    state.nextOffset = page.has_more ? page.next_offset : null;
    renderConversations();
    status.textContent = state.conversations.length ? "" : page.has_more ? "本页没有可读对话。" : "暂无历史对话。";
    more.textContent = "加载更多";
    more.hidden = !page.has_more;
  } catch (error) {
    if (epoch !== state.listEpoch) return;
    state.nextOffset = offset;
    status.textContent = error.message;
    more.textContent = "重试";
    more.hidden = false;
  } finally {
    if (epoch === state.listEpoch) more.disabled = false;
  }
}
$("#conversation-more").addEventListener("click", () => {
  if (state.nextOffset !== null) loadConversations(state.nextOffset);
});
function newChat() {
  rememberConversation(null);
  clearData();
  renderConversations();
}
$("#new-chat").addEventListener("click", () => {
  if (state.running) return;
  newChat();
  $("#question").focus();
});
function inaccessibleConversation() {
  newChat();
  notice("这段对话已不可读取，请重新选择。", "error");
  loadConversations();
}
async function openConversation(id, restoredNotice = "") {
  if (!state.connected || state.running) return;
  clearData();
  if (history.state?.ragConversation?.id !== id) rememberConversation(null);
  const epoch = state.viewEpoch;
  state.restoring = true;
  $("#run-button").disabled = true;
  $("#current-chat").hidden = true;
  renderConversations();
  notice("正在读取对话…");
  try {
    const detail = await (await api(`/conversations/${encodeURIComponent(id)}`)).json();
    if (epoch !== state.viewEpoch) return;
    if (detail.conversation_id !== id || !Array.isArray(detail.turns)) throw new Error("对话记录返回格式不正确。");
    state.messages = detail.turns;
    state.conversationId = id;
    rememberConversation(id);
    renderConversations();
    renderChatHistory();
    notice(state.messages.length ? "" : "这段对话暂无可读记录。");
    state.restoring = false;
    $("#run-button").disabled = false;
  } catch (error) {
    if (epoch !== state.viewEpoch) return;
    if ([403, 404].includes(error.status)) inaccessibleConversation();
    else detailError(error, () => openConversation(id, restoredNotice));
    return;
  }
  const latest = state.messages.at(-1);
  if (latest) {
    $("#chat-scroll").scrollTop = $("#chat-scroll").scrollHeight;
    await inspectTurn(latest.trace_id, restoredNotice);
  } else if (restoredNotice) {
    notice(restoredNotice, "error");
  }
}
function detailError(error, retry) {
  $("#run-status").textContent = "读取失败";
  $("#timeline-heading").textContent = "记录暂不可读取";
  for (const id of ["panel-answer", "panel-evidence", "panel-models", "timeline", "inspector-body"])
    empty($("#" + id), "记录暂不可读取。");
  $("#raw-data").textContent = "记录暂不可读取。";
  $("#duration-caption").textContent = "暂无执行记录";
  notice(error.message, "error");
  const button = el("button", "", "重试");
  button.type = "button";
  button.addEventListener("click", retry);
  $("#notice").append(" ", button);
}
async function inspectTurn(id, restoredNotice = "") {
  if (!state.connected || state.running) return;
  resetCurrent();
  const epoch = state.viewEpoch;
  $("#current-chat").hidden = true;
  renderChatHistory();
  $("#run-status").textContent = "正在读取";
  try {
    const trace = await (await api(`/traces/${encodeURIComponent(id)}`)).json();
    if (epoch !== state.viewEpoch) return;
    if (trace.trace_id !== id) throw new Error("执行记录不匹配。");
    applyRun(trace.final_result, trace, {transcript: false});
    renderChatHistory();
    if (restoredNotice) notice(restoredNotice, "error");
  } catch (error) {
    if (epoch !== state.viewEpoch) return;
    if ([403, 404].includes(error.status)) return inaccessibleConversation();
    detailError(error, () => inspectTurn(id, restoredNotice));
  }
}
$("#export-button").addEventListener("click", () => {
  const blob = new Blob([json({ result: state.result, trace: state.trace })], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const link = el("a");
  link.href = url;
  link.download = `rag-trace-${state.traceId || "run"}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
function revealCurrentReply() {
  const epoch = state.viewEpoch;
  const session = state.sessionEpoch;
  requestAnimationFrame(() => {
    if (epoch !== state.viewEpoch || session !== state.sessionEpoch) return;
    const scroll = $("#chat-scroll");
    const question = $("#question-message");
    if (question.hidden) return;
    scroll.scrollTop += question.getBoundingClientRect().top - scroll.getBoundingClientRect().top - 12;
    if (window.matchMedia("(max-width: 970px)").matches) {
      const bounds = scroll.getBoundingClientRect();
      if (bounds.top < 12 || bounds.bottom > window.innerHeight - 12)
        window.scrollBy({ top: bounds.top - 12, behavior: "instant" });
    }
  });
}
function showQuestion(question) {
  $("#chat-question").textContent = question || "";
  $("#question-message").hidden = !question;
}
function renderWelcome() {
  const welcome = el("div", "chat-welcome");
  const icon = el("div", "welcome-icon", "⌕");
  icon.setAttribute("aria-hidden", "true");
  const pills = el("div", "welcome-pills");
  for (const label of ["真实检索", "逐步耗时", "引用回源"]) pills.append(el("span", "", label));
  welcome.append(icon, el("h2", "", "从你的问题开始"),
    el("p", "", "在下方输入问题。左侧追踪检索流程，右侧查看每一步的证据与结果。"), pills);
  $("#panel-answer").replaceChildren(welcome);
}
function renderChatHistory() {
  const target = $("#chat-history"); target.replaceChildren();
  for (const message of state.messages) {
    if (!$("#current-chat").hidden && message.trace_id === state.traceId) continue;
    const question = el("article", "chat-message user-message");
    question.append(el("div", "message-label", "你"), el("p", "", message.question));
    const answer = el("article", "chat-message assistant-message");
    const label = el("div", "message-label");
    label.append(el("span", "assistant-mark", "A"), document.createTextNode("Adaptive RAG"));
    const selected = message.trace_id === state.traceId;
    const result = selected && state.result ? state.result : message;
    answer.append(label);
    if (result.status === "insufficient") {
      answer.append(selected && state.result ? insufficientReply(result) : el("p", "history-answer", noAnswerNotes.insufficient));
    } else if (!result.answer) answer.append(el("p", "history-answer", result.message || noAnswerNotes[result.status] || "本轮没有返回回答。"));
    if (result.answer || (selected && result.status === "evidence_preview")) answer.append(answerReader(result));
    const footer = el("div", "history-caption");
    footer.append(el("span", "", statuses[message.status] || message.status));
    const button = el("button", "", selected ? "正在查看本轮流程" : message.status === "insufficient" ? "查看本轮资料与流程 →" : "查看本轮流程 →");
    button.type = "button";
    button.disabled = state.running;
    button.setAttribute("aria-pressed", String(selected));
    button.addEventListener("click", () => inspectTurn(message.trace_id));
    footer.append(button); answer.append(footer); target.append(question, answer);
  }
}
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    if (state.connected) $("#question").focus();
    else $("#login-username").focus();
  }
});
// A back/forward-cache restore bypasses the normal session and permission checks.
window.addEventListener("pageshow", event => { if (event.persisted) location.reload(); });
window.addEventListener("storage", event => {
  if (event.key === sessionSignal) showLogin(sessionChanged);
});
window.addEventListener("focus", () => {
  if (state.connected) api("/console/session").catch(() => {});
});
restoreSession();
