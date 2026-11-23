"use strict";

const fileCenter = (() => {
  const byId = id => document.getElementById(id);
  const state = {session: null, generation: null, files: [], view: "all", busy: false};
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function notice(message, error = false) {
    const node = byId("knowledge-notice");
    node.textContent = message;
    node.hidden = !message;
    node.classList.toggle("error", error);
  }
  async function response(path, options = {}) {
    const res = await pageOwner.request(path, options);
    if (res.ok) return res;
    const json = await res.json().catch(() => null);
    const message = json?.error?.message || (typeof json?.detail === "string" && json.detail);
    if (res.status === 409) throw Error((message || "文件已变更。") + " 请刷新并核对最新版本后重试。");
    throw Error(message || ({403: "当前账号没有此操作权限。", 429: "请求过于频繁，请稍后重试。"}[res.status]) || `请求失败（HTTP ${res.status}）`);
  }
  async function json(path, method = "GET", body, signal) {
    const res = await response(path, {method, signal, ...(body === undefined ? {} : {
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    })});
    return res.status === 204 ? null : res.json();
  }
  function normalize(item) {
    const alias = String(item.kind || (item.asset_id ? "image" : "document")).toLowerCase();
    const kind = ["image", "images", "asset", "assets", "image_asset"].includes(alias) ? "image" : "document";
    return {...item, kind, id: item.id || item.asset_id || item.document_id, status: item.status || item.indexing_status || item.state};
  }
  const path = file => `/files/${file.kind}/${encodeURIComponent(file.id)}`;
  const revision = file => file.revision_id || file.revision || file.version;
  function contentPath(file, rev, original = false) {
    const params = new URLSearchParams();
    if (original) params.set("original", "true");
    if (rev) params.set("revision", rev);
    return path(file) + "/content" + (params.size ? "?" + params : "");
  }
  const mayManage = file => !file.trash_id && (state.session.admin || (state.session.subject_id && file.owner_id === state.session.subject_id));
  function time(value) {
    if (value == null || value === "") return "—";
    const numeric = typeof value === "number" || /^-?\d+(\.\d+)?$/.test(value);
    const date = new Date(numeric ? Number(value) * 1000 : value);
    return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", {hour12: false});
  }
  const kinds = {document: "文档", image: "图片"};
  const statuses = {staged: "待发布", pending: "待处理", published: "已发布", ready: "就绪", revoked: "访问已撤销", trashed: "已移入回收站", deleted: "已删除", active: "正常", failed: "失败"};
  function action(label, run) {
    const button = element("button", label);
    button.type = "button";
    button.addEventListener("click", async () => {
      button.disabled = true;
      try { await run(); } catch (error) { notice(error.message, true); }
      finally { button.disabled = false; }
    });
    return button;
  }
  return {byId, state, normalize, element, notice, response, json, path, revision, contentPath, mayManage, time, kinds, statuses, action};
})();
