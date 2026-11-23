// Interact with the shipped HTML and scripts; observe the emitted HTTP requests.
(async () => {
  const id = name => document.getElementById(name);
  const one = s => document.querySelector(s);
  const btn = (root, text) => [...root.querySelectorAll("button")].find(b => b.textContent === text);
  const assert = (value, label) => { if (!value) throw Error(label); };
  const until = async (fn, label) => { for (let i=0;i<150;i++) { if (fn()) return; await new Promise(r=>setTimeout(r,10)); } throw Error(label); };
  const clickRow = (title, label) => btn(btn(id("file-list"), title).closest("article"), label).click();
  const close = modal => modal.querySelector("[data-close]").click();
  const detail = async () => { btn(id("file-list"), "Guide").click(); await until(()=>one("#detail-body pre"), "preview"); };
  const select = (name, value) => { id(name).value = value; id(name).dispatchEvent(new Event("change")); };
  function chooseFile(name, content) {
    const transfer = new DataTransfer();
    transfer.items.add(new File([content], name));
    id("editor-upload").files = transfer.files;
    id("editor-upload").dispatchEvent(new Event("change"));
  }
  // Use the browser's resolved colors, including alpha blending on the paper.
  function assertReadable(dialog, selectors) {
    const ctx = document.createElement("canvas").getContext("2d");
    const paint = color => { ctx.fillStyle = color; ctx.fillRect(0, 0, 1, 1); };
    const luminance = () => {
      const rgb = Array.from(ctx.getImageData(0, 0, 1, 1).data).slice(0, 3).map(byte => {
        const v = byte / 255;
        return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
      });
      return .2126 * rgb[0] + .7152 * rgb[1] + .0722 * rgb[2];
    };
    for (const selector of selectors) {
      const node = dialog.querySelector(selector);
      const layers = [];
      for (let el = node; el !== dialog.parentElement; el = el.parentElement) layers.unshift(getComputedStyle(el).backgroundColor);
      layers.forEach(paint);
      const bg = luminance();
      paint(getComputedStyle(node).color);
      const fg = luminance(), ratio = (Math.max(bg, fg) + .05) / (Math.min(bg, fg) + .05);
      assert(ratio >= 4.5, `${dialog.id} ${selector} text contrast: ${ratio.toFixed(2)}`);
    }
    assert(getComputedStyle(dialog).colorScheme === "light", "native controls on paper");
    assert(getComputedStyle(document.body).colorScheme === "dark", "desk palette stays dark");
  }
  async function submit(url) {
    const n = writes.length;
    id("editor-form").requestSubmit();
    await until(()=>writes.length > n && !id("file-editor").open && !fileCenter.state.busy, "save: " + url + " " + id("editor-error").textContent);
    assert(writes[n].url === url, "wrong endpoint: " + writes[n].url);
    return writes[n].body;
  }
  try {
    await until(()=>btn(id("file-list"), "Guide"), "load list");
    assert(reads.includes("/files?view=" + (adminFixture ? "all" : "mine")), "role list");
    assert(id("evaluation-open").hidden === !adminFixture, "evaluation permission");
    assert(id("file-list").textContent.includes("Alice") && id("file-list").textContent.includes("Editor"), "display names");
    assert(fileCenter.time(1700000000) === fileCenter.time("2023-11-14T22:13:20Z"), "epoch / ISO");
    id("file-kind").value = "image"; id("file-kind").dispatchEvent(new Event("input"));
    assert(!btn(id("file-list"), "Guide") && btn(id("file-list"), "Photo"), "image alias filter");
    id("file-kind").value = ""; id("file-kind").dispatchEvent(new Event("input"));
    btn(id("file-list"), "Photo").click();
    await until(()=>one("#detail-body h3"), "image history");
    assert(id("detail-body").textContent.includes("v1") && !one("#detail-body select"), "historical images have no preview selector");
    assert(reads.includes("/files/image/pic/content?revision=r2") && !reads.includes("/files/image/pic/content?revision=r1"), "current image revision only");
    close(id("file-detail"));
    await detail();
    assertReadable(id("file-detail"), ["h2", "pre", "select"]);
    assert(one("#detail-body pre").textContent === "<b>plain text</b>" && !one("#detail-body pre b"), "inert text preview");
    const rev = one("#detail-body select"); rev.value = "r1"; rev.dispatchEvent(new Event("change"));
    // Capture the final download handoff without writing into the browser user's Downloads.
    const click = HTMLAnchorElement.prototype.click;
    let download;
    HTMLAnchorElement.prototype.click = function () { download = {url: this.href, name: this.download}; };
    try {
      btn(id("detail-body"), "下载原文件").click();
      await until(() => download, "download handoff");
      assert(download.url.startsWith("blob:") && download.name === "guide.txt" &&
        reads.includes("/files/document/guide/content?original=true&revision=r1"), "owner-checked revision original");
    } finally { HTMLAnchorElement.prototype.click = click; }
    assert(id("detail-body").textContent.includes("updated"), "events");
    if (adminFixture) {
      btn(id("detail-body"), "发布版本 v2").click(); await until(()=>!id("file-detail").open && !fileCenter.state.busy, "publish");
      assert(writes.at(-1).url === "/publish" && writes.at(-1).body.expected_generation === "g1", "publish contract");
      await detail(); btn(id("detail-body"), "保存分类").click(); await until(()=>!id("file-detail").open && !fileCenter.state.busy, "access");
      assert(writes.at(-1).url === "/documents/access" && !('allowed_subjects' in writes.at(-1).body), "preserve ACL");
    } else {
      assert(!btn(id("detail-body"), "保存分类"), "member access controls"); close(id("file-detail"));
      btn(id("file-list"), "Report").click();
      await until(() => one("#detail-body pre"), "parsed text preview");
      HTMLAnchorElement.prototype.click = function () { download = {url:this.href, name:this.download}; };
      try {
        for (const ext of Object.keys(binaryTypes)) {
          const selector = one("#detail-body select");
          selector.value = "rev-" + ext;
          selector.dispatchEvent(new Event("change"));
          download = null;
          btn(id("detail-body"), "下载原文件").click();
          await until(() => download, "original download: " + ext);
          assert(download.url.startsWith("blob:") && download.name === "report." + ext, "original MIME: " + ext);
        }
      } finally { HTMLAnchorElement.prototype.click = click; }
      close(id("file-detail"));
      id("file-add").click();
      assertReadable(id("file-editor"), ["h2", "label", "#editor-hint", "#editor-title", "#editor-kind", "#editor-content", "legend", "#editor-save"]);
      assert(id("editor-domains").querySelectorAll("input").length === 1 && !one("#editor-domains input:checked"), "member classifications");
      id("editor-title").value = "New"; id("editor-content").value = "Text";
      const doc = await submit("/files/documents"); assert(!("data_domains" in doc || "allowed_subjects" in doc), "default unclassified");
      id("file-add").click(); chooseFile("notes.md", "# Local text");
      await until(() => id("editor-content").value === "# Local text", "local text import");
      const notes = await submit("/files/documents");
      assert(notes.media_type === "text/markdown" && notes.content === "# Local text" && !("cloud_consent" in notes), "local text endpoint");
      for (const ext of Object.keys(binaryTypes)) {
        id("file-add").click(); select("editor-kind", "parse");
        const count = writes.length;
        assert(!id("editor-notice").hidden && id("editor-disclosure").innerText.includes("外部 MinerU"), "visible disclosure");
        assert(id("editor-upload").accept.split(",").sort().join() === Object.keys(binaryTypes).map(e => "." + e).sort().join(), "binary picker");
        chooseFile("sample." + ext.toUpperCase(), "binary fixture");
        assert(!id("editor-consent").checked && !id("editor-form").checkValidity(), "fresh consent required");
        id("editor-form").requestSubmit();
        assert(writes.length === count && !fileCenter.state.busy, "no upload without consent");
        id("editor-consent").checked = true;
        one("#editor-domains input").checked = true;
        const body = await submit("/files/parse");
        assert(writes.at(-1).method === "POST" && body.extension === ext && body.cloud_consent === true &&
          atob(body.content_base64) === "binary fixture" && body.data_domains.join() === "public", "parse contract: " + ext);
        assert(Object.keys(body).sort().join() === "cloud_consent,content_base64,data_domains,document_id,extension,title,version", "parse fields only");
      }
      const before = reads.length;
      clickRow("Report", "更新"); await until(()=>id("file-editor").open && !id("editor-save").disabled, "edit binary document");
      assert(id("editor-kind").value === "parse" && !reads.slice(before).some(url => url.includes("/content")), "select binary mode from source metadata");
      id("editor-version").value = "v3";
      chooseFile("replacement.exe", "private bytes"); id("editor-consent").checked = true;
      const countBeforeError = writes.length;
      id("editor-form").requestSubmit(); await until(()=>id("editor-error").textContent, "unsupported extension error");
      assert(writes.length === countBeforeError, "unsupported extension not sent");
      chooseFile("replacement.XLSX", "private bytes"); assert(!id("editor-consent").checked, "replacement invalidates consent");
      id("editor-consent").checked = true;
      let fail;
      parseReply = owner => new Promise(resolve => { fail = () => resolve(Response.json({detail:"private upstream error"}, {status:503, headers:owner})); });
      id("editor-form").requestSubmit(); await until(()=>fail, "pending parse");
      assert(id("editor-status").innerText.includes("MinerU") && id("editor-save").disabled, "visible wait state");
      id("editor-form").requestSubmit(); assert(writes.length === countBeforeError + 1, "no duplicate parse");
      fail(); await until(()=>!fileCenter.state.busy, "parse failure"); parseReply = null;
      assert(id("file-editor").open && !id("editor-save").disabled && !id("editor-status").textContent && !id("editor-consent").checked &&
        id("editor-error").innerText && !document.body.innerText.includes("private upstream error"), "recoverable redacted error");
      id("editor-consent").checked = true;
      const next = await submit("/files/parse");
      assert(next.document_id === "report" && next.version === "v3" && next.extension === "xlsx" && !("data_domains" in next), "binary version update preserves access");
      id("file-add").click(); select("editor-kind", "image"); assert(id("editor-domain-fields").hidden, "private image form");
      const picture = new DataTransfer(); picture.items.add(new File(["image"], "p.png", {type:"image/png"}));
      id("editor-upload").files = picture.files; id("editor-title").value = "Image";
      assert(id("editor-notice").hidden && !id("editor-consent").required, "image upload needs no parser consent");
      const img = await submit("/files/images"); assert(!("data_domains" in img || "allowed_subjects" in img || "cloud_consent" in img || "extension" in img), "private image body");
      clickRow("Photo", "更新"); await until(()=>id("file-editor").open && !id("editor-save").disabled, "edit image");
      assert(id("editor-description").value === "Caption", "image prefill");
      const edit = await submit("/files/images/pic"); assert(writes.at(-1).method === "PATCH" && edit.expected_revision === "r2" && !("allowed_subjects" in edit || "data_domains" in edit), "image update");
      clickRow("Guide", "更新"); await until(()=>id("file-editor").open && !id("editor-save").disabled, "edit document");
      assert(id("editor-content").value === "<b>plain text</b>" && id("editor-domain-fields").hidden, "document prefill");
      id("editor-version").value = "v3"; const updated = await submit("/files/documents");
      assert(updated.document_id === "guide" && updated.version === "v3" && !("data_domains" in updated), "document update");
      const count = writes.length; window.confirm = () => false; clickRow("Guide", "移入回收站"); assert(writes.length === count, "cancel delete");
      await Promise.resolve(); window.confirm = () => true; clickRow("Guide", "移入回收站"); await until(()=>!btn(id("file-list"), "Guide"), "trash");
      assert(writes.at(-1).body.expected_revision === "r2" && writes.at(-1).body.expected_generation === "g1", "trash contract");
      one('[data-view="trash"]').click(); await until(()=>btn(id("file-list"), "还原"), "trash list");
      assert(id("file-list").textContent.includes("Remover"), "deleted by name");
      const row = title => [...id("file-list").querySelectorAll("article")].find(r => r.querySelector("h3").textContent === title);
      assert(!one("#file-list h3 button, #file-list a"), "trash rows have no ambiguous detail links");
      assert(row("Archived first") && row("Archived second"), "two tombstones with same asset id");
      btn(row("Archived second"), "还原").click();
      await until(()=>!fileCenter.state.busy && !row("Archived second"), "restore selected image");
      assert(row("Archived first") && writes.at(-1).url === "/files/image/pic/restore" && writes.at(-1).body.trash_id === "second", "restore exact tombstone");
      btn(row("Guide"), "还原").click(); await until(()=>!fileCenter.state.busy && !row("Guide"), "restore");
      assert(writes.at(-1).body.trash_id === "trash1" && writes.at(-1).body.expected_generation === "g1", "restore contract");
    }
    document.body.dataset.result = "pass";
  } catch (e) { document.body.dataset.result = "FAIL: " + e.message; }
})();
