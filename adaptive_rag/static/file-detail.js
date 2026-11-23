"use strict";

const fileDetail = (() => {
  const {byId, element, state, path, json, response, contentPath, revision, time, action} = fileCenter;
  const modal = byId("file-detail"), root = byId("detail-body");
  const downloadTypes = new Map([
    ["text/plain", "txt"], ["application/pdf", "pdf"], ["application/msword", "doc"],
    ["application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"],
    ["application/vnd.ms-excel", "xls"], ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"],
    ["application/vnd.ms-powerpoint", "ppt"], ["application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"],
    ["image/png", "png"], ["image/webp", "webp"], ["image/jpeg", "jpg"],
  ]);
  let request, previewRequest;
  function stop() { request?.abort(); previewRequest?.abort(); root.replaceChildren(); }
  modal.addEventListener("close", () => { if (!modal.open) stop(); });

  function download(label, path, id, signal) {
    return action(label, async () => {
      const res = await response(path, {signal});
      const extension = downloadTypes.get(res.headers.get("Content-Type")?.split(";")[0].trim().toLowerCase());
      if (!extension) throw Error("无法下载此内容类型。");
      const blob = await res.blob();
      if (signal.aborted) return;
      pageOwner.assertActive();
      const link = element("a");
      link.href = URL.createObjectURL(blob);
      link.download = `${id}.${extension}`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    });
  }

  async function preview(file, rev, container, signal) {
    const url = contentPath(file, rev);
    container.textContent = "正在读取预览…";
    try {
      const res = await response(url, {signal});
      const type = res.headers.get("Content-Type")?.split(";")[0].trim().toLowerCase();
      if (type === "text/plain") {
        container.replaceChildren(element("pre", await res.text()));
      } else if (["image/jpeg", "image/png", "image/webp", "application/pdf"].includes(type)) {
        container.replaceChildren(download(type === "application/pdf" ? "下载 PDF" : "下载原图", url, file.id, signal));
        if (type.startsWith("image/")) {
          const img = element("img");
          img.alt = file.title || file.id;
          // The page CSP permits data: images, but not blob: images. Reuse the validated response.
          const blob = await res.blob();
          if (signal.aborted) return;
          const reader = new FileReader();
          reader.onload = () => { if (!signal.aborted) { img.src = reader.result; container.prepend(img); } };
          reader.readAsDataURL(blob);
        }
      } else container.textContent = "无法预览此内容类型。";
    } catch (error) { if (!signal.aborted) container.textContent = error.message; }
  }
  function governance(file, mutate, generation) {
    const section = element("section");
    section.append(element("h3", "发布与访问权限"));
    const choices = element("div");
    RagDomains.render(choices, file.data_domains || []);
    section.append(choices);
    if (file.revoked || file.allowed_subjects?.length) section.append(element("p", "保存分类不会更改现有账号限制或撤销状态。", "file-meta"));
    const actions = element("div", null, "file-actions");
    const error = element("p");
    error.setAttribute("role", "alert");
    const submit = async (endpoint, body, message) => {
      try { await mutate(endpoint, body, message, () => modal.close()); }
      catch (e) { error.textContent = e.message; }
    };
    actions.append(action("保存分类", () => {
      const domains = RagDomains.values(choices);
      if (!domains.length) { error.textContent = "请至少选择一个数据分类。"; return; }
      return submit("/documents/access", {document_id: file.id, data_domains: domains}, "分类已更新。");
    }));
    for (const job of file.staged_jobs || []) actions.append(action("发布版本 " + (job.version || job.revision_id || job.job_id),
      () => submit("/publish", {job_id: job.job_id, expected_generation: generation}, "版本已发布。")));
    section.append(actions, error);
    return section;
  }
  async function open(file, mutate) {
    if (file.trash_id || state.view === "trash") return;
    stop();
    request = new AbortController();
    const {signal} = request;
    const generation = state.generation;
    byId("detail-heading").textContent = file.title || file.id;
    root.textContent = "正在读取详情…";
    modal.showModal();
    try {
      const detail = await json(path(file), "GET", undefined, signal);
      if (signal.aborted) return;
      const info = fileCenter.normalize({...file, ...(detail.file || detail)});
      root.replaceChildren(element("p", "ID：" + info.id + " · 所有者：" + (info.owner_name || info.owner_id || "—")),
        element("p", "更新人：" + (info.updated_by_name || info.updated_by || "—") + " · " + time(info.updated_at)),
        element("p", "数据分类：" + RagDomains.text(info.data_domains)));
      if (info.description) root.append(element("p", info.description));
      const versions = detail.versions || info.versions || [];
      root.append(element("h3", "版本历史"));
      const list = element("ul"), select = element("select");
      select.setAttribute("aria-label", "预览版本");
      const makeOption = (label, value) => { const o = element("option", label); o.value = value || ""; select.append(o); };
      makeOption("当前版本 · " + (info.version || revision(info) || "—"), revision(info));
      for (const v of versions) {
        list.append(element("li", [v.version || revision(v), v.title, v.state, v.updated_by_name || v.updated_by || v.created_by_name || v.created_by, time(v.updated_at ?? v.created_at)].filter(Boolean).join(" · ")));
        if (info.kind === "document" && revision(v) && revision(v) !== revision(info)) makeOption(v.version || revision(v), revision(v));
      }
      root.append(versions.length ? list : element("p", "暂无版本记录。"));
      if (!info.trash_id && state.view !== "trash") {
        root.append(element("h3", "内容预览"));
        if (info.kind === "document") root.append(select);
        const holder = element("div");
        root.append(holder);
        const show = () => {
          const output = element("div");
          holder.replaceChildren(output);
          previewRequest?.abort();
          previewRequest = new AbortController();
          if (info.kind === "document") {
            holder.prepend(download("下载原文件", contentPath(info, select.value, true), info.id, previewRequest.signal));
          }
          preview(info, select.value, output, previewRequest.signal);
        };
        select.addEventListener("change", show);
        show();
        if (state.session.admin && info.kind === "document") root.append(governance(info, mutate, generation));
      }
      root.append(element("h3", "变更记录"));
      const events = detail.events || info.events || [];
      const history = element("ul");
      for (const e of events) history.append(element("li", [e.action || e.event || e.type, e.actor_name || e.updated_by_name || e.actor_id || e.actor || e.updated_by,
        time(e.at ?? e.timestamp ?? e.created_at ?? e.updated_at)].filter(Boolean).join(" · ")));
      root.append(events.length ? history : element("p", "暂无变更记录。"));
    } catch (error) { if (!signal.aborted) root.textContent = error.message; }
  }
  return {open};
})();
