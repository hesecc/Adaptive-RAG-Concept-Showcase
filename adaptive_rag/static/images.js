"use strict";

const byId = id => document.getElementById(id);
let imageAdmin = false;
let imageCloud = false;
let editingAsset = null;
const statusLabels = {
  pending: "待建索引", ready: "可语义检索", failed: "索引失败",
  awaiting_description: "待补文字", deleted: "已删除", indexing: "正在建索引",
  outdated: "索引配置已变更",
  lexical_ready: "可文字检索",
};
const stageLabels = {
  query_embedding: "问题向量化", vector_search: "向量召回",
  asset_hydrate: "资产与权限核验", description_lexical_search: "描述文字检索",
  description_embedding: "描述向量化", vector_upsert: "写入向量索引",
  search_error: "检索执行失败",
};

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

async function api(path, options = {}) {
  const response = await pageOwner.request(path, {...options,
    headers: {"Content-Type": "application/json", ...options.headers}});
  if (response.status === 429) throw Error("请求过于频繁，请稍后重试。");
  let value = null;
  try { value = response.status === 204 ? null : await response.json(); }
  catch { if (response.ok) throw Error(`服务返回格式错误（HTTP ${response.status}），请稍后重试。`); }
  if (!response.ok) throw Error(value?.error?.message || `请求失败 (${response.status})`);
  return value;
}

function notice(message, isError = false) {
  const node = byId("image-notice");
  node.textContent = message;
  node.hidden = !message;
  node.classList.toggle("error", isError);
}

const assetPath = asset => `/assets/${encodeURIComponent(asset.asset_id)}`;
// Native image loads and direct links cannot carry the displayed owner's headers.
async function imageData(path, signal) {
  const res = await pageOwner.request(path, {signal});
  if (!res.ok) throw Error(`无法读取图片（HTTP ${res.status}）。`);
  const blob = await res.blob();
  if (!["image/png", "image/jpeg", "image/webp"].includes(blob.type)) throw Error("图片格式不正确。");
  const data = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(Error("无法读取图片。"));
    reader.readAsDataURL(blob);
  });
  pageOwner.assertActive();
  signal.throwIfAborted();
  return data;
}

let thumbnails, original;
const visible = new IntersectionObserver(entries => {
  for (const {target, isIntersecting} of entries) if (isIntersecting && target.isConnected) {
    visible.unobserve(target);
    imageData(target.dataset.thumbnail, thumbnails.signal).then(data => { target.src = data; })
      .catch(() => { target.alt = "缩略图暂不可读取"; });
  }
});
const originalDialog = byId("image-original");
byId("image-original-close").addEventListener("click", () => originalDialog.close());
originalDialog.addEventListener("close", () => {
  original?.abort();
  byId("image-original-content").removeAttribute("src");
});
async function showOriginal(asset) {
  original?.abort();
  original = new AbortController();
  const img = byId("image-original-content");
  img.src = await imageData(`${assetPath(asset)}/content`, original.signal);
  img.alt = asset.title || "原图";
  originalDialog.showModal();
}

function action(label, callback) {
  const button = element("button", label);
  button.type = "button";
  button.addEventListener("click", async () => {
    button.disabled = true;
    try { await callback(); }
    catch (error) { notice(error.message, true); }
    finally { button.disabled = false; }
  });
  return button;
}

function renderAssets(items) {
  visible.disconnect();
  thumbnails?.abort();
  thumbnails = new AbortController();
  const root = byId("image-results");
  root.replaceChildren();
  byId("image-count").textContent = `${items.length} 张图片`;
  if (!items.length) {
    root.append(element("div", "暂无匹配图片。可调整检索文字，或由管理员添加图片资料。", "plain-empty"));
    return;
  }
  for (const item of items) {
    const asset = item.asset || item;
    const card = element("article", undefined, "image-card");
    const link = action("", () => showOriginal(asset));
    link.className = "image-preview";
    link.setAttribute("aria-label", `查看原图：${asset.title || asset.asset_id}`);
    const img = element("img");
    img.dataset.thumbnail = `${assetPath(asset)}/thumbnail`;
    img.alt = asset.title || "图片资料";
    visible.observe(img);
    link.append(img);
    card.append(link);
    const body = element("div", undefined, "image-card-body");
    body.append(element("h3", asset.title || asset.asset_id));
    body.append(element("p", `数据分类：${RagDomains.text(asset.data_domains)}`, "image-meta"));
    const state = asset.indexing_status || asset.index_status || "pending";
    const stateLabel = !imageCloud && state === "pending" ? "待语义索引 · 可文字检索"
      : !imageCloud && state === "ready" ? "可文字检索 · 语义模式未启用" : statusLabels[state] || state;
    body.append(element("span", stateLabel, `image-status ${state === "failed" ? "failed" : ""}`));
    body.append(element("p", item.matched_text || asset.description || "仅有标题；补充说明可改善检索。"));
    if (asset.tags?.length) body.append(element("p", asset.tags.join(" · "), "image-meta"));
    if (item.score !== undefined) {
      body.append(element("p", `检索分数 ${Number(item.score).toFixed(4)} · 分数用于排序`, "image-meta"));
    }
    if (asset.index_error) {
      body.append(element("p", asset.index_error.message || "索引未完成，可重试后重新处理。", "image-index-error"));
    }
    if (asset.source_document_id) {
      body.append(element("p", `来源：${asset.source_document_id}${asset.source_page ? ` · 第 ${asset.source_page} 页` : ""}`, "image-meta"));
    }
    const actions = element("div", undefined, "image-card-actions");
    actions.append(action("查看原图", () => showOriginal(asset)));
    if (imageAdmin) {
      actions.append(action("编辑说明", () => openEditor(asset)));
      actions.append(action("更新／重试索引", async () => {
        await api(`${assetPath(asset)}/reindex`, {method: "POST"});
        await refresh();
        notice("已登记当前配置的索引任务。点击“处理待建索引”执行。已缓存的同版本向量会复用。");
      }));
      actions.append(action("删除", async () => {
        if (!confirm(`删除“${asset.title}”？`)) return;
        await api(`${assetPath(asset)}?expected_revision=${encodeURIComponent(asset.revision_id)}`, {method: "DELETE"});
        await refresh();
        notice("图片资料已删除。");
      }));
    }
    body.append(actions);
    card.append(body);
    root.append(card);
  }
}

function renderTrace(value) {
  const trace = value.trace || value;
  byId("image-trace-id").textContent = trace.trace_id || value.trace_id || "—";
  byId("image-trace-summary").textContent = `状态 ${trace.status || "—"} · 总耗时 ${Number(trace.duration_ms || 0).toFixed(1)} ms · 模型调用 ${trace.model_calls || 0}`;
  const root = byId("image-stages");
  root.replaceChildren();
  for (const stage of trace.stages || []) {
    const details = element("details", undefined, "image-stage");
    const summary = element("summary");
    summary.append(
      element("span", stageLabels[stage.name] || stage.name),
      element("span", `${stage.status || "ok"} · ${Number(stage.duration_ms || 0).toFixed(1)} ms`),
    );
    details.append(summary, element("pre", JSON.stringify(stage.output ?? stage.result ?? {}, null, 2)));
    root.append(details);
  }
  if (!root.children.length) root.append(element("div", "此记录没有步骤详情。", "plain-empty"));
}

async function refresh() {
  byId("image-results-heading").textContent = "图片资料";
  const value = await api("/assets");
  renderAssets(value.assets);
}

function openEditor(asset = null) {
  editingAsset = asset;
  byId("image-edit-form").reset();
  byId("image-edit-error").textContent = "";
  byId("image-editor-heading").textContent = asset ? "编辑图片说明" : "添加图片资料";
  byId("image-file-label").hidden = !!asset;
  byId("image-file").required = !asset;
  RagDomains.render(byId("image-data-domains"), asset?.data_domains || []);
  if (asset) {
    byId("image-title").value = asset.title;
    byId("image-description").value = asset.description || "";
    byId("image-tags").value = (asset.tags || []).join(", ");
    byId("image-ocr").value = asset.ocr_text || "";
  }
  byId("image-editor").showModal();
}

async function fileBase64(file) {
  if (!file || file.size > 10_000_000) throw Error("请选择不超过 10MB 的 PNG、JPEG 或 WebP 图片。");
  return await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.onerror = () => reject(Error("图片读取失败，请重新选择。"));
    reader.readAsDataURL(file);
  });
}

byId("image-edit-form").addEventListener("submit", async event => {
  event.preventDefault();
  byId("image-save").disabled = true;
  try {
    const dataDomains = RagDomains.values(byId("image-data-domains"));
    if (!dataDomains.length) throw Error("请选择图片的数据分类，再保存资料。");
    const value = {
      title: byId("image-title").value.trim(),
      description: byId("image-description").value.trim(),
      ocr_text: byId("image-ocr").value.trim(),
      tags: byId("image-tags").value.split(/[,，]/).map(v => v.trim()).filter(Boolean),
      allowed_subjects: editingAsset?.allowed_subjects || [],
      data_domains: dataDomains,
    };
    if (editingAsset) {
      value.expected_revision = editingAsset.revision_id;
      await api(assetPath(editingAsset), {method: "PATCH", body: JSON.stringify(value)});
    } else {
      value.asset_id = `img-${crypto.randomUUID()}`;
      value.content_base64 = await fileBase64(byId("image-file").files[0]);
      await api("/assets", {method: "POST", body: JSON.stringify(value)});
    }
    byId("image-editor").close();
    await refresh();
    notice(imageCloud ? "图片资料已保存。点击“处理待建索引”更新语义索引。"
      : "图片资料已保存，可直接按标题、说明、标签和 OCR 进行文字检索。未调用模型。");
  } catch (error) {
    byId("image-edit-error").textContent = error.message;
  } finally {
    byId("image-save").disabled = false;
  }
});

byId("image-upload-open").addEventListener("click", () => openEditor());
byId("image-editor-close").addEventListener("click", () => byId("image-editor").close());
byId("image-refresh").addEventListener("click", () => refresh().catch(error => notice(error.message, true)));
byId("image-search-form").addEventListener("submit", async event => {
  event.preventDefault();
  byId("image-search-submit").disabled = true;
  notice("正在检索图片描述…");
  try {
    const value = await api("/assets/search", {
      method: "POST", body: JSON.stringify({question: byId("image-question").value, top_k: 20}),
    });
    byId("image-results-heading").textContent = "检索结果";
    renderAssets(value.hits || []);
    renderTrace(value);
    if (value.status === "error") {
      notice(value.error?.message || "检索执行失败，请查看右侧步骤与 Trace ID。", true);
      return;
    }
    const limited = (value.stages || []).some(stage => stage.output?.candidate_limit_reached);
    notice(limited ? "向量候选已达到本次上限，当前结果可能不完整；详情见资产与权限核验步骤。"
      : value.search_mode === "lexical" ? "本次使用文字检索，未调用模型。" : "已根据图片的文字描述完成检索。");
  } catch (error) {
    notice(error.message, true);
  } finally {
    byId("image-search-submit").disabled = false;
  }
});

byId("image-index").addEventListener("click", async () => {
  byId("image-index").disabled = true;
  notice("正在处理待建索引任务…");
  try {
    const value = await api("/assets/index", {method: "POST", body: JSON.stringify({limit: 10})});
    await refresh();
    notice(`已处理 ${value.processed} 项：就绪 ${value.ready}、删除清理 ${value.deleted}、失败 ${value.failed}、取消 ${value.cancelled}。`, value.failed > 0);
  } catch (error) {
    notice(error.message, true);
  } finally {
    byId("image-index").disabled = false;
  }
});

byId("image-trace-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const value = await api(`/assets/traces/${encodeURIComponent(byId("image-trace-input").value.trim())}`);
    renderTrace(value);
    renderAssets(value.hits || []);
    byId("image-results-heading").textContent = "历史检索结果";
    notice(value.status === "error" ? "已读取失败记录，请展开步骤查看原因。" : "已读取图片检索记录。", value.status === "error");
  } catch (error) {
    notice(error.message, true);
  }
});

(async () => {
  try {
    const session = await pageOwner.session();
    // The page tabs do not depend on the domain catalogue, so reveal them before it loads.
    byId("knowledge-open").hidden = false;
    byId("evaluation-open").hidden = !session.admin;
    await RagDomains.load(pageOwner.request);
    imageAdmin = session.admin;
    imageCloud = session.capabilities.image_semantic_search === true;
    byId("image-account").textContent = session.username;
    byId("image-domains").textContent = `数据权限：${RagDomains.text(session.data_domains)}`;
    byId("image-admin").hidden = !imageAdmin;
    byId("image-mode").textContent = imageCloud
      ? "语义检索使用当前文本 Embedding；图片说明保存后需完成索引。"
      : "图片库按文字描述检索，不调用模型。";
    await refresh();
  } catch (error) {
    notice(error.message, true);
  }
})();
