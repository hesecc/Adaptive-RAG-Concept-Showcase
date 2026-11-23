"use strict";

// These pages have one displayed owner for their lifetime; never rebind a live editor.
const pageOwner = (() => {
  const fields = [["tenant_id", "X-RAG-Tenant"], ["subject_id", "X-RAG-Principal"]];
  const changed = "登录身份已变更，请重新打开页面。";
  let expected, closed = false;
  function invalidate() {
    if (closed) return;
    closed = true;
    document.documentElement.hidden = true;
    location.replace("/");
  }
  function assertActive() {
    if (closed || !expected) throw Error(changed);
  }
  function check(headers, owner) {
    if (!fields.every(([, header]) => headers.get(header) === owner.get(header))) {
      invalidate();
      throw Error(changed);
    }
  }
  async function send(path, options) {
    let res;
    try { res = await fetch(path, {...options, credentials: "same-origin", mode: "same-origin", redirect: "error", cache: "no-store"}); }
    catch (error) {
      if (error.name === "AbortError") throw error;
      throw Error("无法连接服务，请检查网络后重试。");
    }
    if (res.status === 401) invalidate();
    if (closed) throw Error(changed);
    return res;
  }
  async function session() {
    if (closed || expected) throw Error(changed);
    const res = await send("/console/session");
    if (!res.ok) throw Error(res.status === 429 ? "请求过于频繁，请稍后重试。" : `无法读取账号（HTTP ${res.status}）。`);
    const user = await res.json();
    if (closed || expected) throw Error(changed);
    const owner = new Headers();
    for (const [field, header] of fields) {
      if (typeof user[field] !== "string" || !user[field]) throw Error("服务未返回完整的登录身份。");
      owner.set(header, encodeURIComponent(user[field]).replace(/[!'()*]/g, c => "%" + c.charCodeAt(0).toString(16).toUpperCase()));
    }
    check(res.headers, owner);
    expected = owner;
    return user;
  }
  async function request(path, options = {}) {
    assertActive();
    const headers = new Headers(options.headers);
    expected.forEach((value, header) => headers.set(header, value));
    const res = await send(path, {...options, headers});
    if (res.ok || fields.some(([, header]) => res.headers.has(header))) check(res.headers, expected);
    else throw Error(res.status === 429 ? "请求过于频繁，请稍后重试。" : `请求失败（HTTP ${res.status}）`);
    return res;
  }
  window.addEventListener("storage", e => { if (e.key === "rag.session.changed") invalidate(); });
  window.addEventListener("pageshow", e => { if (e.persisted) invalidate(); });
  return {session, request, assertActive};
})();
