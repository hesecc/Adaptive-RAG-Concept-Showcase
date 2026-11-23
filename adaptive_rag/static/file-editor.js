"use strict";

const fileEditor = (() => {
  const {byId, state, json, path, response, contentPath, revision, mayManage} = fileCenter;
  const field = name => byId("editor-" + name);
  const modal = byId("file-editor");
  const extensions = ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx"];
  let editing = null;
  let request;

  function configure() {
    const kind = field("kind").value;
    const image = kind === "image";
    const parse = kind === "parse";
    field("upload-label").hidden = image && !!editing;
    field("upload").required = parse || (image && !editing);
    field("upload").accept = image ? "image/png,image/jpeg,image/webp" : parse ? extensions.map(e => "." + e).join(",") : ".md,.markdown,.txt,.html,.htm";
    field("notice").hidden = !parse;
    field("consent").required = parse;
    field("version-label").hidden = image;
    field("version").required = !image;
    field("text-fields").hidden = kind !== "document";
    field("content").required = kind === "document";
    field("image-fields").hidden = !image;
    field("domain-fields").hidden = !!editing || (image && !state.session.admin);
    field("hint").textContent = parse ? "PDF、Word、Excel 或 PowerPoint，最大 10 MB。" : image ?
      (editing ? "更新图片说明，不替换原图或访问权限。" : "PNG、JPEG 或 WebP，最大 10 MB。") : "支持粘贴正文或读取不超过 2 MB 的文本、Markdown、HTML。";
    field("save").textContent = image ? "保存图片" : "暂存文档";
  }
  function base64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result.split(",")[1]);
      reader.onerror = () => reject(Error("无法读取所选文件。"));
      reader.readAsDataURL(file);
    });
  }
  async function payload() {
    const kind = field("kind").value;
    const id = editing?.id || field("id").value.trim();
    const title = field("title").value.trim();
    if (!title) throw Error("请填写标题。");
    const body = {title};
    if (!editing && (kind !== "image" || state.session.admin)) {
      const domains = RagDomains.values(field("domains")).filter(d => state.session.admin || state.session.data_domains?.includes(d));
      if (domains.length) body.data_domains = domains;
    }
    if (kind === "image") {
      Object.assign(body, {description: field("description").value, ocr_text: field("ocr").value,
        tags: field("tags").value.split(/[,，]/).map(t => t.trim()).filter(Boolean)});
      if (editing) {
        body.expected_revision = revision(editing);
        if (!body.expected_revision) throw Error("缺少图片版本，请刷新。");
        if (state.session.admin && Array.isArray(editing.allowed_subjects)) body.allowed_subjects = editing.allowed_subjects;
        return ["/files/images/" + encodeURIComponent(id), "PATCH", body];
      }
      body.asset_id = id;
    } else {
      body.document_id = id;
      body.version = field("version").value.trim();
      if (!body.version) throw Error("请填写新版本号。");
      if (kind === "document") {
        body.media_type = field("format").value;
        body.content = field("content").value;
        return ["/files/documents", "POST", body];
      }
    }
    const file = field("upload").files[0];
    if (!file || !file.size || file.size > 10_000_000) throw Error("请选择非空且不超过 10 MB 的文件。");
    if (kind === "parse") {
      const extension = /\.([a-z0-9]+)$/i.exec(file.name)?.[1].toLowerCase();
      if (!extensions.includes(extension)) throw Error("请选择支持的 PDF 或 Office 文件。");
      if (!field("consent").checked) throw Error("请先确认同意外部解析。");
      Object.assign(body, {extension, cloud_consent: true});
    }
    body.content_base64 = await base64(file);
    return [kind === "parse" ? "/files/parse" : "/files/images", "POST", body];
  }
  async function open(file = null) {
    if (state.busy || (file && !mayManage(file))) return;
    request?.abort();
    request = new AbortController();
    const {signal} = request;
    editing = file;
    byId("editor-form").reset();
    field("error").textContent = "";
    field("heading").textContent = file ? "更新文件" : "添加文件";
    field("id").readOnly = !!file;
    field("id").value = file?.id || crypto.randomUUID();
    field("kind").value = file?.kind || "document";
    for (const option of field("kind").options) option.disabled = !!file && (file.kind === "image") !== (option.value === "image");
    field("title").value = file?.title || "";
    field("version").value = file ? "" : "v1";
    field("version").placeholder = file ? "当前版本：" + (file.version || "—") : "";
    RagDomains.render(field("domains"), [], {allowed: state.session.admin ? null : state.session.data_domains || []});
    configure();
    modal.showModal();
    if (!file) return;
    field("fields").disabled = field("save").disabled = true;
    try {
      const detail = await json(path(file), "GET", undefined, signal);
      if (signal.aborted) return;
      editing = fileCenter.normalize({...file, ...(detail.file || detail)});
      if (!mayManage(editing)) throw Error("文件权限已改变，请关闭并刷新。");
      field("title").value = editing.title || "";
      if (file.kind === "image") {
        field("description").value = editing.description || "";
        field("ocr").value = editing.ocr_text || "";
        field("tags").value = (editing.tags || []).join(", ");
      } else if (extensions.includes(editing.source_extension)) {
        field("kind").value = "parse";
      } else {
        try {
          const res = await response(contentPath(file, revision(editing)), {signal});
          const type = res.headers.get("Content-Type")?.split(";")[0];
          if (type === "text/plain") {
            const content = await res.text();
            if (signal.aborted) return;
            field("content").value = content;
            field("format").value = ["text/plain", "text/html", "text/markdown"].includes(editing.media_type) ? editing.media_type : "text/plain";
          } else if (type === "application/pdf") {
            if (signal.aborted) return;
            field("kind").value = "parse";
          }
        } catch (error) {
          if (signal.aborted) return;
          field("error").textContent = "未能读取原正文。可提交新的文本、PDF 或 Office 文件。";
        }
      }
      if (signal.aborted) return;
      configure();
      field("fields").disabled = field("save").disabled = false;
    } catch (error) { if (!signal.aborted) field("error").textContent = error.message; }
  }
  modal.addEventListener("close", () => {
    if (modal.open) return;
    request?.abort();
    byId("editor-form").reset();
    field("fields").disabled = field("save").disabled = false;
  });
  field("kind").addEventListener("change", () => { field("upload").value = ""; field("consent").checked = false; field("error").textContent = ""; configure(); });
  field("upload").addEventListener("change", async () => {
    const input = field("upload"), file = input.files[0];
    field("consent").checked = false;
    if (!file) return;
    try {
      field("error").textContent = "";
      if (field("kind").value === "document") {
        if (file.size > 2_000_000 || !/\.(md|markdown|txt|html|htm)$/i.test(file.name)) throw Error("请选择不超过 2 MB 的文本、Markdown 或 HTML 文件。");
        const text = await file.text();
        if (input.files[0] !== file) return;
        field("content").value = text;
        field("format").value = /\.html?$/i.test(file.name) ? "text/html" : /\.txt$/i.test(file.name) ? "text/plain" : "text/markdown";
      } else if (file.size > 10_000_000) throw Error("文件不能超过 10 MB。");
      if (!field("title").value.trim()) field("title").value = file.name.slice(0, 256);
    } catch (error) { input.value = ""; field("error").textContent = error.message; }
  });
  function bind(saved) {
    byId("editor-form").addEventListener("submit", async event => {
      event.preventDefault();
      if (state.busy || field("fields").disabled) return;
      state.busy = true;
      field("fields").disabled = field("save").disabled = true;
      field("error").textContent = "";
      if (field("kind").value === "parse") field("status").textContent = "正在提交文件并等待 MinerU 解析，请稍候…";
      try {
        const args = await payload();
        try { await json(...args); }
        catch (error) {
          if (args[0] !== "/files/parse") throw error;
          // Provider errors may quote document content or include private URLs.
          field("consent").checked = false;
          throw Error("未能完成外部解析。请刷新核对版本后重试，或联系管理员。");
        }
        modal.close();
        await saved(args[0].startsWith("/files/images") ? "图片已保存。" : "文档已暂存，等待管理员发布。");
      } catch (error) { field("error").textContent = error.message; }
      finally { field("status").textContent = ""; state.busy = false; field("fields").disabled = field("save").disabled = false; }
    });
  }
  return {open, bind};
})();
