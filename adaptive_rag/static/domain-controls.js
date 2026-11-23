"use strict";

window.RagDomains = (() => {
  let catalog = [];
  let loading;
  async function load(request = fetch) {
    if (!loading) loading = (async () => {
      let response;
      try { response = await request("/access/domains", {credentials: "same-origin", cache: "no-store"}); }
      catch { throw Error("无法读取数据分类，请检查网络后重试。"); }
      if (response.status === 429) throw Error("请求过于频繁，请稍后重试。");
      if (!response.ok) throw Error("无法读取数据分类，请刷新页面或重新登录。");
      const value = await response.json();
      catalog = value.domains || [];
      return catalog;
    })().catch(error => { loading = null; throw error; });
    return loading;
  }
  function text(values) {
    if (values === null) return "全部分类";
    if (!values?.length) return "未分类 · 仅管理员可见";
    return values.map(value => catalog.find(item => item.id === value)?.label || value).join("、");
  }
  function render(root, selected = [], {publicRequired = false, allowed = null} = {}) {
    root.replaceChildren();
    root.classList.add("domain-options");
    for (const item of catalog) {
      if (allowed !== null && !allowed.includes(item.id)) continue;
      const label = document.createElement("label");
      label.className = "domain-option";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = item.id;
      input.checked = selected.includes(item.id) || (publicRequired && item.id === "public");
      input.disabled = publicRequired && item.id === "public";
      label.append(input, document.createTextNode(item.label));
      root.append(label);
    }
  }
  function values(root) {
    return [...root.querySelectorAll("input:checked")].map(input => input.value);
  }
  return {load, text, render, values};
})();
