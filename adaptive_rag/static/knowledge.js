"use strict";

(() => {
  const {byId, element, state, json, path, revision, mayManage, time, kinds, statuses, action, notice} = fileCenter;
  let listRequest;
  function render() {
    const root = byId("file-list");
    const query = byId("file-query").value.trim().toLowerCase();
    const kind = byId("file-kind").value, status = byId("file-status").value;
    const files = state.files.filter(f => (!kind || f.kind === kind) && (!status || f.status === status) &&
      [f.title, f.id, f.owner_name, f.owner_id].some(v => String(v || "").toLowerCase().includes(query)));
    root.replaceChildren();
    byId("knowledge-count").textContent = files.length + " 个文件";
    const generation = state.generation;
    for (const file of files) {
      const row = element("article", null, "file-row");
      const identity = element("div"), title = element("h3");
      if (state.view === "trash") {
        title.textContent = file.title || file.id;
      } else {
        const name = action(file.title || file.id, () => fileDetail.open(file, mutate));
        name.className = "file-name";
        title.append(name);
      }
      identity.append(title, element("p", kinds[file.kind] + " · " + file.id, "file-meta"));
      const metadata = element("div");
      metadata.append(element("p", [statuses[file.status] || file.status, file.version || revision(file)?.slice(0, 12)].filter(Boolean).join(" · ")),
        element("p", "所有者：" + (file.owner_name || file.owner_id || "—"), "file-meta"));
      const changed = element("div"), actions = element("div", null, "file-actions");
      if (state.view === "trash") {
        changed.append(element("p", "删除：" + (file.deleted_by_name || file.deleted_by || "—") + " · " + time(file.deleted_at), "file-meta"),
          element("p", "恢复截止：" + time(file.purge_after), "file-meta"));
        identity.append(element("p", "回收记录：" + file.trash_id, "file-meta"));
        if (file.can_restore && file.trash_id) actions.append(action("还原", () => mutate(path(file) + "/restore",
          {trash_id: file.trash_id, expected_generation: generation}, "文件已还原。")));
      } else {
        changed.append(element("p", time(file.updated_at)), element("p", "更新人：" + (file.updated_by_name || file.updated_by || "—"), "file-meta"));
        if (mayManage(file)) actions.append(action("更新", () => fileEditor.open(file)), action("移入回收站", () => {
          if (!revision(file)) throw Error("缺少文件版本，请刷新后重试。");
          if (window.confirm("将“" + (file.title || file.id) + "”移入回收站？")) return mutate(path(file) + "/trash",
            {expected_generation: generation, expected_revision: revision(file)}, "文件已移入回收站。");
        }));
      }
      row.append(identity, metadata, changed, actions);
      root.append(row);
    }
    if (!files.length) root.append(element("p", query || kind || status ? "没有符合筛选条件的文件。" : state.view === "trash" ? "回收站为空。" : "暂无文件。", "plain-empty"));
  }
  async function refresh(view = state.view) {
    listRequest?.abort();
    listRequest = new AbortController();
    const {signal} = listRequest;
    state.view = view;
    state.files = []; state.generation = null;
    for (const b of document.querySelectorAll("[data-view]")) b.setAttribute("aria-pressed", String(b.dataset.view === view));
    byId("knowledge-count").textContent = byId("knowledge-generation").textContent = "";
    byId("file-list").textContent = "正在读取文件…";
    try {
      const value = await json("/files?view=" + view, "GET", undefined, signal);
      if (signal.aborted) return;
      if (!Array.isArray(value.files)) throw Error("文件列表格式错误。");
      state.files = value.files.map(fileCenter.normalize);
      state.generation = value.generation ?? null;
      const select = byId("file-status"), selected = select.value;
      select.replaceChildren(new Option("全部状态", ""));
      for (const s of new Set(state.files.map(f => f.status).filter(Boolean))) select.add(new Option(statuses[s] || s, s));
      if ([...select.options].some(o => o.value === selected)) select.value = selected;
      byId("knowledge-generation").textContent = state.generation ? "当前快照：" + state.generation : "尚无已发布快照";
      render();
    } catch (error) {
      if (signal.aborted) return;
      byId("file-list").textContent = "未能读取文件。请刷新重试。";
      throw error;
    }
  }
  async function saved(message) {
    try { await refresh(); notice(message); }
    catch (error) { notice(message + " 列表未刷新：" + error.message, true); }
  }
  async function mutate(url, body, message, onSuccess) {
    if (state.busy) throw Error("另一项变更正在进行。");
    state.busy = true;
    try { await json(url, "POST", body); onSuccess?.(); await saved(message); }
    finally { state.busy = false; }
  }
  function load(view) { notice(""); refresh(view).catch(e => notice(e.message, true)); }
  for (const b of document.querySelectorAll("[data-view]")) b.addEventListener("click", () => { byId("file-status").value = ""; load(b.dataset.view); });
  for (const id of ["file-kind", "file-status", "file-query"]) byId(id).addEventListener("input", render);
  byId("knowledge-refresh").addEventListener("click", () => load(state.view));
  byId("file-add").addEventListener("click", () => fileEditor.open());
  for (const b of document.querySelectorAll("[data-close]")) b.addEventListener("click", () => { if (!state.busy) b.closest("dialog").close(); });
  for (const d of document.querySelectorAll("dialog")) d.addEventListener("cancel", e => { if (state.busy) e.preventDefault(); });
  fileEditor.bind(saved);
  (async () => {
    state.session = await pageOwner.session();
    byId("knowledge-account").textContent = state.session.username || state.session.subject_id;
    byId("evaluation-open").hidden = !state.session.admin;
    document.querySelector('[data-view="all"]').hidden = !state.session.admin;
    state.view = state.session.admin ? "all" : "mine";
    await RagDomains.load(pageOwner.request);
    byId("knowledge-domains").textContent = "数据权限：" + RagDomains.text(state.session.data_domains);
    byId("knowledge-workspace").hidden = false;
    await refresh();
  })().catch(e => notice(e.message, true));
})();
