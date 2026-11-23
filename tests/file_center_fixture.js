// Browser-only API fixture. Loaded before the real page scripts by test_file_center_ui.py.
const adminFixture = location.search.includes("admin");
const writes = [], reads = [];
const documentFile = {id: "guide", kind: "document", title: "Guide", owner_id: "sub1", owner_name: "Alice",
  updated_by: "sub2", updated_by_name: "Editor", updated_at: 1700000000, revision_id: "r2", version: "v2",
  status: "staged", data_domains: ["public"], staged_jobs: [{version: "v2", job_id: "job2"}],
  versions: [{version: "v1", revision_id: "r1", created_at: "2024-01-01T00:00:00Z"}],
  events: [{action: "updated", actor_name: "Editor", at: 1700000000}]};
const imageFile = {...documentFile, kind: "image_asset", id: "pic", title: "Photo", description: "Caption", allowed_subjects: ["existing-acl"]};
const binaryTypes = {
  pdf: "application/pdf", doc: "application/msword", docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  xls: "application/vnd.ms-excel", xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ppt: "application/vnd.ms-powerpoint", pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
};
const binaryFile = {...documentFile, id: "report", title: "Report", source_extension: "xlsx", revision_id: "rev-xlsx",
  versions: Object.keys(binaryTypes).map(ext => ({version: ext, revision_id: "rev-" + ext, source_extension: ext}))};
let parseReply;
let trashFixture = false;
let imageTrash = ["first", "second"].map(key => ({...imageFile, trash_id:key, title:"Archived " + key,
  status:"trashed", can_restore:true, deleted_by_name:"Remover", deleted_at:1700000000}));
window.fetch = async (url, options = {}) => {
  const owner = {"X-RAG-Principal": "sub1", "X-RAG-Tenant": "home"};
  if (url !== "/console/session" && Object.entries(owner).some(([k, v]) => new Headers(options.headers).get(k) !== v))
    throw Error("Missing displayed owner: " + url);
  const obj = value => Response.json(value, {headers: owner});
  const body = options.body && JSON.parse(options.body);
  if (body) writes.push({url, method: options.method, body}); else reads.push(url);
  if (url === "/console/session") return obj({subject_id: "sub1", tenant_id: "home", admin: adminFixture, data_domains: ["public"]});
  if (url === "/access/domains") return obj({domains: [{id:"public",label:"Public"},{id:"finance",label:"Finance"}]});
  if (url.startsWith("/files?view=")) {
    const trash = {...documentFile, trash_id:"trash1", deleted_by_name:"Remover", deleted_at:1700000000, can_restore:true};
    return obj({generation:"g1", files:url.endsWith("trash") ? [...imageTrash, ...(trashFixture ? [trash] : [])] : [imageFile, binaryFile, ...(trashFixture ? [] : [documentFile])]});
  }
  if (url.endsWith("/trash")) trashFixture = true;
  if (url === "/files/document/guide/restore") trashFixture = false;
  if (url === "/files/image/pic/restore") imageTrash = imageTrash.filter(row => row.trash_id !== body.trash_id);
  if (url === "/files/parse" && parseReply) return parseReply(owner);
  if (body) return obj({});
  if (url === "/files/document/report") return obj(binaryFile);
  if (url.startsWith("/files/document/report/content?original=true&revision=rev-")) {
    return new Response("original fixture", {headers:{...owner, "Content-Type":binaryTypes[url.split("rev-").pop()]}});
  }
  if (url.includes("/content")) return new Response("<b>plain text</b>", {headers:{...owner, "Content-Type":"text/plain; charset=utf-8"}});
  if (url === "/files/image/pic") return obj(imageFile);
  if (url === "/files/document/guide") return obj(documentFile);
  throw Error("Unexpected request: " + url);
};
