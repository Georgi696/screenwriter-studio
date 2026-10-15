const GLYPHS = {
  sheet: '<svg viewBox="0 0 8 8" aria-hidden="true"></svg>',
  page: '<svg viewBox="0 0 8 8" aria-hidden="true"></svg>',
  shot: '<svg viewBox="0 0 8 8" aria-hidden="true"></svg>',
  frame: '<svg viewBox="0 0 8 8" aria-hidden="true"></svg>'
};
const PIPS = ["brief", "development", "writer", "editor", "approval", "art", "images"];
const CREW = ["development", "writer", "editor", "art"];
let last = null;
let revision = -1;
let shown = 0;
let streamOK = false;
let openPath = null;
let readerSource = "";
let readerFormat = "";
let readerMode = "source";
let follow = true;
let studioSettings = null;
let settingsSchema = null;
let stillFilter = "all";
let toastTimer = 0;
let viewChosen = false;
let readerOpen = false;
const localFiles = new Map();
const LOCAL_DOCS = new Set(["json", "md", "fountain", "txt"]);
const LOCAL_IMAGES = new Set(["png", "jpg", "jpeg", "webp", "gif"]);
const REFERENCE_EXT = new Set(["png", "jpg", "jpeg", "webp"]);
const MAX_REFERENCE_BYTES = 8000000;

let libraryProjects = [];
let libraryLoaded = false;
let deskFolder = "";
let referenceKey = "";
// Path -> reference role, scope and optional direction.
const selectedRefs = new Map();
const refInfo = new Map();
let uploadedRefs = [];
let showEarlierRefs = false;
// Roles whose name must match the script's character, location or object.
const SUBJECT_ROLES = new Set(["character", "location", "object"]);
const REFERENCE_STORE = "studio.references";

function plain(text) {
  return String(text || "")
    .replace(/\*\*/g, "")
    .replace(/`/g, "")
    .replace(/(?:\/[\w.-]+)+\/productions\//g, "productions/");
}

const MODEL_NAMES = {
  "kling-3.0/video": "Kling 3.0",
  "bytedance/seedance-2-5": "Seedance 2.5",
  "nano-banana-2-1": "Nano Banana",
  "nano-banana-pro": "Nano Banana Pro",
  "gpt-image-2-5-sunburst-text-to-image": "Sunburst",
  "gpt-image-2-5-sunburst-image-to-image": "Sunburst edit",
  "seedream/5-pro-text-to-image": "Seedream"
};

function modelTitle(model) {
  const raw = String(model || "Image");
  return MODEL_NAMES[raw] || raw.replace(/[-_/]+/g, " ");
}

function noteText(note) {
  const text = note || "Waiting for a brief.";
  if (text === "Stills are in images/.") return "Stills are ready.";
  return text;
}

function setText(el, text) {
  if (el && el.textContent !== text) el.textContent = text;
}

function setMode(el, mode) {
  if (!el || el.dataset.mode === mode) return;
  el.dataset.mode = mode;
  el.classList.remove("is-working", "is-done", "is-idle", "is-skipped", "is-blocked", "is-stopped", "is-awaiting");
  const klass = {
    working: "is-working",
    done: "is-done",
    skipped: "is-skipped",
    blocked: "is-blocked",
    stopped: "is-stopped",
    awaiting: "is-awaiting"
  }[mode] || "is-idle";
  el.classList.add(klass);
}

function stationEl(id) {
  return document.querySelector('[data-station="' + id + '"]');
}

function toast(message, ms) {
  const el = document.getElementById("toast");
  el.hidden = false;
  el.textContent = message;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ms || 1600);
}

function copyText(text, message) {
  const value = String(text || "");
  if (!value.trim()) {
    toast("Nothing to copy");
    return;
  }
  const done = () => toast(message);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(value).then(done).catch(() => {
      fallbackCopy(value);
      done();
    });
    return;
  }
  fallbackCopy(value);
  done();
}

function fallbackCopy(value) {
  const area = document.createElement("textarea");
  area.value = value;
  document.body.appendChild(area);
  area.select();
  document.execCommand("copy");
  area.remove();
}

function moveGlider() {
  const bar = document.querySelector(".tablist");
  const on = bar.querySelector(".tab.is-on");
  const glider = document.getElementById("tab-glider");
  if (!on || !glider) return;
  glider.style.width = on.offsetWidth + "px";
  glider.style.transform = "translateX(" + on.offsetLeft + "px)";
}

function setView(name, fromUser) {
  if (fromUser) {
    follow = false;
    viewChosen = true;
    document.getElementById("follow").classList.remove("is-on");
  }
  document.body.dataset.view = name;
  document.querySelectorAll(".tab").forEach((tab) => {
    const on = tab.dataset.tab === name;
    tab.classList.toggle("is-on", on);
    tab.setAttribute("aria-selected", on ? "true" : "false");
    if (on) tab.classList.remove("has-news");
  });
  if (last) syncNextStep(last);
  document.body.classList.remove("is-entering");
  void document.body.offsetWidth;
  document.body.classList.add("is-entering");
  requestAnimationFrame(() => {
    moveGlider();
    placeRail();
    if (last) moveToken(last.station || "brief", false);
  });
}

function viewFor(state) {
  if (state.active === "video") return "video";
  if ((state.images || []).some((image) => image.state === "running") || state.station === "images") return "stills";
  if (state.station && state.station !== "brief") return "pipeline";
  return "brief";
}

function applyStillFilter() {
  const bay = document.getElementById("image-bay");
  let shownCols = 0;
  bay.querySelectorAll(".model-col").forEach((col) => {
    let visible = 0;
    col.querySelectorAll(".frame").forEach((frame) => {
      const ghost = frame.classList.contains("ghost");
      const kind = (frame.dataset.kind || "").toLowerCase();
      const state = frame.dataset.state || "";
      const waitingSlot = ghost && !col.classList.contains("has-jobs");
      let show = !ghost;
      if (stillFilter === "waiting") show = waitingSlot || ["running", "review", "blocked", "inspecting"].includes(state);
      else if (stillFilter !== "all") show = !ghost && kind.includes(stillFilter);
      frame.hidden = !show;
      if (show) visible += 1;
    });
    col.hidden = visible === 0;
    if (!col.hidden) shownCols += 1;
  });
  bay.dataset.filterEmpty = bay.children.length && shownCols === 0 ? "1" : "";
}

const folderPreferences = new Map();
try {
  const saved = JSON.parse(localStorage.getItem("studio.library.collapsed") || "[]");
  if (Array.isArray(saved)) {
    for (const key of saved) if (typeof key === "string") folderPreferences.set(key, false);
  }
} catch (_) { /* Folder toggles still work when browser storage is unavailable. */ }
let folderSequence = 0;

function setFolderExpanded(section, expanded) {
  section.querySelector(".folder-toggle").setAttribute("aria-expanded", String(expanded));
  section.querySelector(".folder-content").hidden = !expanded;
}

function libraryFolder(group, source) {
  const section = document.createElement("section");
  section.className = "library-folder";
  section.dataset.folder = source + ":" + group.slug;
  const head = document.createElement("div");
  head.className = "folder-head";
  const heading = document.createElement("h3");
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "folder-toggle";
  toggle.textContent = group.slug;
  const content = document.createElement("div");
  content.className = "folder-content";
  content.id = "library-folder-" + (++folderSequence);
  toggle.setAttribute("aria-controls", content.id);
  heading.appendChild(toggle);
  head.appendChild(heading);
  section.append(head, content);
  setFolderExpanded(section, folderPreferences.get(section.dataset.folder) !== false);
  toggle.addEventListener("click", () => {
    const expanded = toggle.getAttribute("aria-expanded") !== "true";
    folderPreferences.set(section.dataset.folder, expanded);
    setFolderExpanded(section, expanded);
    try {
      localStorage.setItem("studio.library.collapsed", JSON.stringify(
        [...folderPreferences].filter(([, open]) => !open).map(([key]) => key)
      ));
    } catch (_) { /* Keep the in-memory preference. */ }
  });
  return { section, head, content };
}

function searchWords(value) {
  return String(value || "").normalize("NFKD").replace(/\p{M}/gu, "")
    .toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}

function filterLibrary() {
  const query = document.getElementById("library-search").value.trim();
  const terms = searchWords(query).split(/\s+/).filter(Boolean);
  const searching = terms.length > 0;
  let files = 0;
  let folders = 0;
  document.querySelectorAll(".library-folder").forEach((section) => {
    const folderName = searchWords(section.querySelector(".folder-toggle").textContent);
    const folderMatches = terms.every((term) => folderName.includes(term));
    let matches = 0;
    section.querySelectorAll(".file").forEach((button) => {
      const name = searchWords(folderName + " " + button.textContent + " " + button.dataset.path);
      button.hidden = searching && !terms.every((term) => name.includes(term));
      if (!button.hidden) matches += 1;
    });
    section.querySelectorAll(".library-group").forEach((group) => {
      group.hidden = searching && !folderMatches && ![...group.querySelectorAll(".file")].some((file) => !file.hidden);
    });
    section.hidden = searching && !folderMatches && matches === 0;
    if (!section.hidden) { folders += 1; files += matches; }
    // Searching reveals matches without changing saved collapse preferences.
    setFolderExpanded(section, searching ? !section.hidden : folderPreferences.get(section.dataset.folder) !== false);
  });
  const status = document.getElementById("library-search-status");
  status.hidden = !searching;
  status.textContent = !searching ? "" : folders
    ? `${files} ${files === 1 ? "file" : "files"} in ${folders} ${folders === 1 ? "folder" : "folders"}`
    : `No files or folders match “${query}”.`;
  document.getElementById("clear-library-search").hidden = !query;
}

function bindLibrarySearch() {
  const input = document.getElementById("library-search");
  input.addEventListener("input", filterLibrary);
  // Safari's built-in search-field clear control also emits this event.
  input.addEventListener("search", filterLibrary);
  document.getElementById("clear-library-search").addEventListener("click", () => {
    input.value = "";
    filterLibrary();
    input.focus();
  });
}

function fileButton(item) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "file";
  if (item.path === openPath) button.classList.add("is-open");
  button.textContent = item.name;
  button.dataset.path = item.path;
  return button;
}

function renderOpenedFolder(groups) {
  const host = document.getElementById("opened-folder");
  host.replaceChildren();
  if (!groups.length) {
    return;
  }
  for (const group of groups) {
    const { section, head, content } = libraryFolder(group, "local");
    const close = document.createElement("button");
    close.type = "button";
    close.className = "linkish";
    close.dataset.closeFolder = "true";
    close.textContent = "Close";
    head.appendChild(close);
    for (const [label, items] of [["Documents", group.documents], ["Images", group.images]]) {
      if (!items.length) continue;
      const groupContent = document.createElement("div");
      groupContent.className = "library-group";
      content.appendChild(groupContent);
      const name = document.createElement("p");
      name.className = "label";
      name.textContent = label;
      groupContent.appendChild(name);
      for (const item of items) groupContent.appendChild(fileButton(item));
    }
    host.appendChild(section);
  }
  filterLibrary();
}

function takeFolder(fileList) {
  localFiles.clear();
  const grouped = new Map();
  for (const file of fileList) {
    const relative = file.webkitRelativePath || file.name;
    const parts = relative.split("/").filter(Boolean);
    const slug = parts.length > 1 ? parts[0] : file.name;
    const name = parts.length > 1 ? parts.slice(1).join("/") : file.name;
    const suffix = name.includes(".") ? name.split(".").pop().toLowerCase() : "";
    const kind = LOCAL_IMAGES.has(suffix) ? "images" : LOCAL_DOCS.has(suffix) ? "documents" : "";
    if (!kind) continue;
    const path = "local:" + relative;
    localFiles.set(path, file);
    if (!grouped.has(slug)) grouped.set(slug, { slug, documents: [], images: [] });
    grouped.get(slug)[kind].push({ path, name });
  }
  const groups = [...grouped.values()].filter((group) => group.documents.length || group.images.length);
  if (!groups.length) {
    toast("That folder has no scripts or images");
    return;
  }
  renderOpenedFolder(groups);
  toast(groups.length === 1 ? groups[0].slug + " opened" : groups.length + " folders opened");
}

function paintCrew(state) {
  const done = new Set(state.done || []);
  const overridden = state.overridden || [];
  const editorBlocked = !!state.can_proceed && !state.running && !done.has("editor") && !overridden.includes("editor");
  for (const id of CREW) {
    const el = stationEl(id);
    const crew = (state.crew || []).find((item) => item.id === id);
    if (crew) {
      setText(el.querySelector("strong"), crew.name);
      setText(el.querySelector(".model"), crew.model || "");
    }
    let mode = "idle";
    let label = "Waiting";
    if (state.active === id && state.running) {
      mode = "working";
      label = "Working";
    } else if (state.active === id) {
      mode = "stopped";
      label = "Stopped";
    } else if (overridden.includes(id)) {
      mode = "skipped";
      label = "Overridden";
    } else if (id === "editor" && editorBlocked) {
      mode = "blocked";
      label = "Blocked";
    } else if (done.has(id)) {
      mode = "done";
      label = "Done";
    }
    setMode(el, mode);
    setText(el.querySelector(".status"), label);
  }
  const approval = stationEl("approval");
  let approvalMode = "idle";
  let approvalLabel = "Waiting";
  const artStarted = done.has("art") || state.active === "art" || state.station === "images" || (state.images || []).length > 0;
  if (done.has("approval")) {
    approvalMode = "done";
    approvalLabel = "Approved";
  } else if (state.can_proceed && !state.running) {
    approvalMode = "awaiting";
    approvalLabel = "Your turn";
  } else if (artStarted) {
    approvalMode = "skipped";
    approvalLabel = "Not required";
  }
  setMode(approval, approvalMode);
  setText(approval.querySelector(".status"), approvalLabel);
  const brief = stationEl("brief");
  const handedOff = state.active === "development" || done.has("development") || ["writer", "editor", "approval", "art", "images"].includes(state.station);
  if (state.station === "brief" && state.running) {
    setMode(brief, "working");
    setText(brief.querySelector(".status"), "Working");
  } else if (handedOff) {
    setMode(brief, "done");
    setText(brief.querySelector(".status"), "Done");
  } else {
    setMode(brief, "idle");
    setText(brief.querySelector(".status"), "Waiting");
  }
}

function moveToken(station, animate) {
  const token = document.getElementById("token");
  const stage = document.getElementById("stage");
  const el = stationEl(station) || stationEl("brief");
  const a = el.getBoundingClientRect();
  const b = stage.getBoundingClientRect();
  const x = a.left - b.left + a.width / 2 - token.offsetWidth / 2;
  let y = a.top - b.top - token.offsetHeight - 6;
  if (station === "images") y = a.top - b.top + 4;
  if (y < 8) y = 8;
  token.classList.toggle("no-anim", !animate);
  token.style.transform = "translate(" + Math.round(x) + "px, " + Math.round(y) + "px)";
}

function stationRow() {
  return [...document.querySelectorAll("#track .station")];
}

function stationsStacked() {
  const row = stationRow();
  if (row.length < 2) return false;
  const first = row[0].getBoundingClientRect();
  const last = row[row.length - 1].getBoundingClientRect();
  return last.top > first.bottom - 8;
}

function placeRail() {
  const slot = document.getElementById("return-slot");
  const rail = document.getElementById("return-rail");
  const row = stationRow();
  if (!slot || !rail || row.length < 2) return;
  const slotBox = slot.getBoundingClientRect();
  const first = row[0].getBoundingClientRect();
  const last = row[row.length - 1].getBoundingClientRect();
  const width = Math.max(0, last.right - first.left);
  if (width < 20 || first.width < 20) return;
  rail.style.marginLeft = Math.max(0, first.left - slotBox.left) + "px";
  rail.style.width = width + "px";
}

function pointOnRail(id) {
  const rail = document.getElementById("return-rail").getBoundingClientRect();
  if (rail.width < 20) return 0;
  if (stationsStacked()) return id === "writer" ? 0 : Math.max(0, rail.width - 8);
  const box = stationEl(id).getBoundingClientRect();
  const x = box.left + box.width / 2 - rail.left - 4;
  return Math.max(0, Math.min(rail.width - 8, x));
}

function playReturn(instant) {
  const rail = document.getElementById("return-rail");
  const token = document.getElementById("return-token");
  rail.classList.add("is-live");
  token.classList.add("no-anim");
  token.style.left = (instant ? pointOnRail("writer") : pointOnRail("editor")) + "px";
  if (instant) return;
  void token.offsetWidth;
  token.classList.remove("no-anim");
  token.style.left = pointOnRail("writer") + "px";
}

function parkReturn() {
  const rail = document.getElementById("return-rail");
  const token = document.getElementById("return-token");
  rail.classList.remove("is-live");
  token.classList.add("no-anim");
  token.style.left = pointOnRail("editor") + "px";
}

function paintProgress(state) {
  const rail = document.getElementById("return-rail");
  if (!rail) return;
  rail.classList.toggle("is-returning", !!state.returning);
}

function columnFor(bay, model) {
  for (const col of bay.querySelectorAll(".model-col")) {
    if (col.dataset.model === model) return col;
  }
  return null;
}

function ensureColumn(bay, model, kinds) {
  let col = columnFor(bay, model);
  if (col) {
    if (kinds) setText(col.querySelector(".kinds"), kinds);
    return col;
  }
  col = document.createElement("section");
  col.className = "model-col";
  col.dataset.model = model;
  const title = document.createElement("h3");
  title.textContent = modelTitle(model);
  title.title = model;
  const kindEl = document.createElement("p");
  kindEl.className = "kinds";
  kindEl.textContent = kinds || "";
  const slots = document.createElement("div");
  slots.className = "slots";
  const ghost = document.createElement("div");
  ghost.className = "frame ghost";
  ghost.textContent = "Waiting";
  slots.appendChild(ghost);
  col.append(title, kindEl, slots);
  bay.appendChild(col);
  return col;
}

function ensureFrame(col, id) {
  const domId = "frame-" + String(id).replace(/[^A-Za-z0-9_-]/g, "_");
  const found = document.getElementById(domId);
  if (found) { col.querySelector(".slots").appendChild(found); return found; }
  const frame = document.createElement("figure");
  frame.id = domId;
  frame.className = "frame";
  frame.dataset.job = id;
  frame.innerHTML = '<div class="flash"></div><div class="cell"><img alt="" hidden><span class="hold">Working</span></div><figcaption></figcaption>';
  col.querySelector(".slots").appendChild(frame);
  return frame;
}

function showDone(frame, image) {
  const img = frame.querySelector("img");
  const hold = frame.querySelector(".hold");
  if (image.src) {
    if (img.getAttribute("src") !== image.src) img.src = image.src;
    img.hidden = false;
    hold.hidden = true;
  } else {
    img.hidden = true;
    hold.hidden = false;
    hold.textContent = "Not on this machine";
  }
}

function applyFrameState(frame, image) {
  frame.dataset.state = image.state || "";
  frame.classList.remove("is-running", "is-done", "is-failed", "exposing", "printed");
  const img = frame.querySelector("img");
  const hold = frame.querySelector(".hold");
  if (image.state === "running") {
    frame.classList.add("is-running", "exposing");
    img.removeAttribute("src");
    img.hidden = true;
    hold.hidden = false;
    hold.textContent = "Working";
  } else if (["done", "review", "inspecting"].includes(image.state)) {
    frame.classList.add("is-done", "printed");
    showDone(frame, image);
  } else if (image.state === "planned") {
    img.removeAttribute("src");
    img.hidden = true;
    hold.hidden = false;
    hold.textContent = "Planned — no image generated";
  } else if (image.state === "blocked") {
    img.hidden = true;
    hold.hidden = false;
    hold.textContent = "Waiting for approved references: " + (image.blocked_by || []).join(", ");
  } else if (image.state === "submitting" && image.uncertain) {
    frame.classList.add("is-failed");
    img.hidden = true;
    hold.hidden = false;
    hold.textContent = "Needs checking";
  } else if (image.state === "failed") {
    frame.classList.add("is-failed");
    img.hidden = true;
    hold.hidden = false;
    hold.textContent = "Failed";
  }
}

const SETTLED_IMAGE = new Set(["done", "review", "failed", "blocked", "planned"]);

async function imageAction(path, body, done) {
  try {
    const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const reply = await res.json();
    if (!res.ok) throw new Error(reply.error || "Could not change this image.");
    toast(done);
  } catch (error) { toast(error.message, 6000); }
}

// Edit the prompt and make a new take, go back to an earlier version, or recover an uncertain submission.
function renderImageTools(frame, image, running) {
  let tools = frame.querySelector(".image-tools");
  const signature = JSON.stringify([image.state, image.prompt, image.versions, image.uncertain, image.model, Boolean(running)]);
  if (tools && tools.dataset.signature === signature) return;
  const previous = tools ? tools.querySelector("textarea") : null;
  const draft = previous && previous.value !== previous.defaultValue ? previous.value : null;
  const wasOpen = Boolean(tools && tools.querySelector("details[open]"));
  if (tools) tools.remove();
  tools = make("div", "image-tools");
  tools.dataset.signature = signature;
  if (image.uncertain) {
    tools.appendChild(make("p", "field-note", "KIE may or may not have created this image's task. Check your KIE task history first."));
    const attach = make("button", "ghost", "Attach task ID");
    attach.type = "button";
    attach.addEventListener("click", () => {
      const task = window.prompt("KIE task ID for " + image.id + " (from your KIE task history):");
      if (task && task.trim()) imageAction("/api/images/attach", { id: image.id, task_id: task.trim() }, "Task attached. Resume saved image work to fetch it.");
    });
    const forget = make("button", "ghost", "Not on KIE? Send it again");
    forget.type = "button";
    forget.addEventListener("click", () => {
      if (!window.confirm("Only do this if KIE's task history has no task for " + image.id + ". Resume then sends it again (paid); if KIE did create one, both are billed. Continue?")) return;
      imageAction("/api/images/forget", { id: image.id }, "Submission forgotten. Resume saved image work to send it again.");
    });
    attach.disabled = forget.disabled = Boolean(running);
    tools.append(attach, forget);
  }
  if (SETTLED_IMAGE.has(image.state) && image.prompt) {
    const edit = make("details", "image-edit");
    edit.open = wasOpen;
    edit.appendChild(make("summary", "", image.edited ? "Prompt (edited) · regenerate" : "Edit prompt and regenerate"));
    const box = document.createElement("textarea");
    box.rows = 6;
    box.value = image.prompt;
    box.defaultValue = image.prompt;
    if (draft !== null) box.value = draft;
    box.setAttribute("aria-label", "Prompt for " + image.id);
    const price = imagePrice(image.model, (last && last.image_resolution) || "1K");
    const go = make("button", "go", "Regenerate" + (price !== null ? " · about " + Math.round(price) + " credits" : ""));
    go.type = "button";
    go.disabled = Boolean(running);
    go.addEventListener("click", () => {
      const prompt = box.value.trim();
      if (!prompt) { toast("Write a prompt first."); return; }
      const cost = price !== null ? " About " + credits(price) + ", plus its inspection." : "";
      if (!window.confirm("Make a new take of " + image.id + " with this prompt?" + cost + " The current version is kept and can be chosen again. Images that use this one are not regenerated.")) return;
      reviewImage({ id: image.id, action: "regenerate", prompt });
    });
    edit.append(box, go);
    tools.appendChild(edit);
  }
  if ((image.versions || []).length) {
    const strip = make("div", "image-versions");
    strip.appendChild(make("p", "field-note", "Earlier versions · switching spends no credits"));
    for (const version of image.versions) {
      const item = make("div", "image-version");
      const thumb = make("img");
      thumb.src = version.src;
      thumb.alt = version.label + " of " + image.id;
      thumb.loading = "lazy";
      const use = make("button", "ghost", "Use " + version.label.toLowerCase());
      use.type = "button";
      use.disabled = Boolean(running) || !SETTLED_IMAGE.has(image.state);
      use.addEventListener("click", () => imageAction("/api/images/version", { id: image.id, version: version.name }, image.id + " now uses " + version.label.toLowerCase() + "."));
      item.append(thumb, use);
      strip.appendChild(item);
    }
    tools.appendChild(strip);
  }
  if (tools.childNodes.length) frame.appendChild(tools);
}

async function reviewImage(data) {
  try {
    const res = await fetch("/api/images/review", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)});
    const reply = await res.json();
    if (!res.ok) throw new Error(reply.error || "Could not continue image work");
    toast("Image work queued");
  } catch (error) { toast(error.message); }
}

function renderInspection(frame, image, running) {
  let panel = frame.querySelector(".image-review");
  const signature = JSON.stringify([image.inspection, image.review_id, image.repair_prompt, image.state, running]);
  if (panel && panel.dataset.signature === signature) return;
  if (panel) panel.remove();
  if (!image.inspection || !image.inspection.summary) return;
  panel = document.createElement("div");
  panel.className = "image-review";
  panel.dataset.signature = signature;
  const heading = document.createElement("strong");
  heading.textContent = image.state === "done" ? (image.decision === "human accepted" ? "Kept by you" : "Accepted") : "Needs your review";
  panel.appendChild(heading);
  if (image.src) {
    const view = document.createElement("button"); view.type = "button"; view.className = "ghost"; view.textContent = "View full image";
    view.addEventListener("click", () => { showReaderImage(image.id, image.src); setView("reader", true); });
    panel.appendChild(view);
  }
  const line = (text) => { const p = document.createElement("p"); p.textContent = text; panel.appendChild(p); };
  const rawFailure = /traceback|errors\.pydantic\.dev|pydantic|validation error/i;
  const summary = String(image.inspection.summary || "");
  const uncertainty = String(image.inspection.uncertainty || "");
  const unreadable = rawFailure.test(summary) || rawFailure.test(uncertainty);
  if (unreadable) line("Inspection could not read this image. You can keep it or inspect it again.");
  else line(summary);
  for (const [label, items] of [["Finding", image.inspection.findings], ["Proposed change", image.inspection.changes], ["Preserve", image.inspection.preserve]]) {
    for (const text of items || []) {
      if (unreadable && rawFailure.test(String(text))) continue;
      line(label + ": " + text);
    }
  }
  if (!unreadable && image.inspection.uncertainty) line("Uncertain: " + image.inspection.uncertainty);
  line("Repairs used: " + (image.repairs || 0) + " / 2. Previous versions remain in the production folder.");
  if (image.repair_prompt && image.state === "review") {
    const details = document.createElement("details");
    const title = document.createElement("summary"); title.textContent = "Exact repair instructions";
    const text = document.createElement("pre"); text.textContent = image.repair_prompt;
    details.append(title, text); panel.appendChild(details);
    const repairPrice = imagePrice(image.repair_model, (last && last.image_resolution) || "1K");
    line("Repair model: " + image.repair_model + ". Approval spends credits on one repair" +
      (repairPrice !== null ? " (about " + credits(repairPrice) + ")" : "") + " and its inspection.");
  }
  if (image.repair_unavailable) line(image.repair_unavailable);
  if (image.state === "review") {
    const actions = document.createElement("div");
    actions.className = "review-actions";
    const addChoice = (parent, action, label) => {
      const button = document.createElement("button"); button.type = "button";
      button.className = action === "repair" ? "go" : "ghost";
      button.dataset.action = action;
      button.textContent = label;
      button.disabled = Boolean(running) && action !== "keep";
      button.addEventListener("click", async () => {
        panel.querySelectorAll("button").forEach(b => { b.disabled = true; });
        await reviewImage({id: image.id, review_id: image.review_id, action});
        panel.querySelectorAll("button").forEach(b => {
          const spends = b.dataset.action === "repair" || b.dataset.action === "inspect";
          b.disabled = spends ? Boolean(last && last.running) : false;
        });
        panel.dataset.signature = "";
        fetch("/api/status").then(r => r.json()).then(apply).catch(() => {});
      });
      parent.appendChild(button);
    };
    if (image.repair_prompt) addChoice(actions, "repair", "Approve these changes (uses credits)");
    addChoice(actions, "keep", "Abort");
    panel.appendChild(actions);
    line("Abort keeps the current image and spends no credits.");
    addChoice(panel, "inspect", "Inspect again (uses credits)");
  }
  frame.appendChild(panel);
}

function savedImageWorkCanResume(state) {
  const images = state.images || [];
  if (!images.length) return false;
  // An image waiting for review only holds back the images that use it; the rest can continue.
  const open = new Set(["failed", "planned", "queued", "polling", "submitting", "downloading", "running", "inspecting"]);
  return images.some((image) => open.has(image.state));
}

function reviewWaiting(state) {
  return (state.images || []).some((image) => image.state === "review");
}

function stillsPaused(state) {
  if (!state || state.running) return false;
  const images = state.images || [];
  if (!images.length) return false;
  return images.some((image) => image.state !== "done");
}

function openingView(state) {
  if (state.video && (state.video.saved || state.active === "video")) return "video";
  const images = state.images || [];
  if ((state.folder && images.length) || reviewWaiting(state) || stillsPaused(state)) return "stills";
  const crewStarted = Boolean(
    (state.done || []).length ||
    (state.overridden || []).length ||
    state.folder ||
    (state.station && state.station !== "brief") ||
    state.active
  );
  if (!images.length && crewStarted) return "pipeline";
  return "";
}

// Mark each numbered step once its work is finished, so the tab bar reads as a checklist.
function syncTabProgress(state) {
  const images = state.images || [];
  const finished = {
    brief: Boolean(state.folder),
    pipeline: (state.done || []).includes("art") || images.length > 0,
    stills: images.length > 0 && images.every((image) => image.state === "done"),
    video: Boolean(state.video && state.video.complete)
  };
  for (const [name, done] of Object.entries(finished)) {
    document.querySelector('.tab[data-tab="' + name + '"]').classList.toggle("is-complete", done);
  }
}

function shotOf(state, id) {
  return (state.shots || []).find((shot) => shot.id === id) || null;
}

function imageOf(state, id) {
  return (state.images || []).find((image) => image.id === id) || null;
}

function clipOf(state, id) {
  return ((state.video && state.video.clips) || []).find((clip) => clip.id === id) || null;
}

function nextStep(state) {
  const images = state.images || [];
  const video = state.video || {};
  const clips = video.clips || [];
  if (state.running) return { text: state.note || "The crew is working." };
  if (state.can_proceed) {
    return state.pages_passed
      ? { text: "The script editor passed the script. Read it, then approve to write the image prompts.", label: "Review the script", view: "pipeline" }
      : { text: "The script editor did not pass this draft. Continue with it or abort.", label: "Review the draft", view: "pipeline" };
  }
  if (/^The run stopped:/.test(state.note || "") && !state.can_proceed) {
    return { text: state.note + " Saved files are kept.", label: "Back to Idea", view: "brief" };
  }
  const waiting = images.filter((image) => image.state === "review").length;
  if (waiting) return { text: waiting + (waiting === 1 ? " image needs" : " images need") + " your decision.", label: "Review images", action: openStillsReview };
  if (stillsPaused(state)) return { text: "Image work is unfinished. Resume it from the Images tab.", label: "Open Images", view: "stills" };
  if (images.length && !video.saved) return { text: "All images are approved. Next, turn each one into a video clip.", label: "Go to Video clips", view: "video" };
  if (video.saved && !video.complete) return { text: "Some video clips are unfinished.", label: "Go to Video clips", view: "video" };
  if (video.complete) return { text: "All " + clips.length + " clips are ready. Download them and combine them in your editor.", label: "Download all clips", href: "/video-zip" };
  if (!state.folder) return { text: "Describe your film to begin.", label: "Go to Idea", view: "brief" };
  return { text: "Open the Story tab to follow the crew.", label: "Go to Story", view: "pipeline" };
}

let nextStepGoal = null;
function syncNextStep(state) {
  nextStepGoal = nextStep(state);
  setText(document.getElementById("next-step-text"), nextStepGoal.text);
  const button = document.getElementById("next-step-action");
  // No point offering a jump to the tab the user is already on.
  button.hidden = !nextStepGoal.label || nextStepGoal.view === document.body.dataset.view;
  setText(button, nextStepGoal.label || "");
  document.getElementById("next-step").classList.toggle("is-busy", Boolean(state.running));
}

function credits(amount) {
  const pricing = settingsSchema && settingsSchema.pricing;
  const rate = pricing ? pricing.usd_per_credit : 0.005;
  return Math.round(amount).toLocaleString() + " credits (about $" + (amount * rate).toFixed(2) + ")";
}

function priceSource(estimate) {
  return estimate && estimate.source ? "KIE prices from " + estimate.source + ", checked " + estimate.checked + ". KIE bills what its price page says on the day." : "";
}

function imagePrice(model, resolution) {
  const pricing = settingsSchema && settingsSchema.pricing;
  const rates = pricing && pricing.images ? pricing.images[model] : null;
  if (!rates) return null;
  return rates[resolution] !== undefined ? rates[resolution] : rates["1K"];
}

function shotLabel(state, id) {
  const shot = shotOf(state, id);
  return shot ? "Shot " + shot.number + " · " + shot.duration_seconds + "s" : id;
}

function shotChip(text, mode, view) {
  const chip = make("button", "shot-chip is-" + mode, text);
  chip.type = "button";
  chip.addEventListener("click", () => setView(view, true));
  return chip;
}

let shotBoardKey = "";
function syncShotBoard(state) {
  const board = document.getElementById("shot-board");
  const shots = state.shots || [];
  board.hidden = !shots.length;
  if (!shots.length) { shotBoardKey = ""; return; }
  const key = JSON.stringify([shots, (state.images || []).map((i) => [i.id, i.state, i.src]), ((state.video || {}).clips || []).map((c) => [c.id, c.state])]);
  if (key === shotBoardKey) return;
  shotBoardKey = key;
  const total = shots.reduce((sum, shot) => sum + shot.duration_seconds, 0);
  const runtime = state.runtime_seconds || total;
  setText(document.getElementById("shot-board-summary"),
    shots.length + (shots.length === 1 ? " shot · " : " shots · ") + total + "s" +
    (total !== runtime ? " (the film is locked at " + runtime + "s)" : ""));
  const meter = document.getElementById("shot-meter");
  const cards = document.getElementById("shot-cards");
  meter.replaceChildren();
  cards.replaceChildren();
  for (const shot of shots) {
    const image = imageOf(state, shot.id);
    const clip = clipOf(state, shot.id);
    const imageReady = Boolean(image && image.state === "done");
    const segment = make("span", "seg" + (clip && clip.state === "done" ? " is-clip" : imageReady ? " is-image" : ""));
    segment.style.flexGrow = String(shot.duration_seconds);
    segment.title = "Shot " + shot.number + " · " + shot.duration_seconds + "s";
    meter.appendChild(segment);
    const card = make("li", "shot-card");
    const head = make("div", "shot-head");
    head.append(make("strong", "", "Shot " + shot.number), make("span", "detail-chip", shot.duration_seconds + "s"));
    card.appendChild(head);
    if (image && image.src) {
      const thumb = make("img", "shot-thumb");
      thumb.src = image.src;
      thumb.alt = "Image for shot " + shot.number;
      thumb.loading = "lazy";
      card.appendChild(thumb);
    }
    const cast = (shot.characters || []).join(", ");
    if (cast || shot.location) card.appendChild(make("p", "shot-meta shot-who", [cast, shot.location].filter(Boolean).join(" · ")));
    card.appendChild(make("p", "shot-action", shot.action));
    for (const line of shot.dialogue || []) card.appendChild(make("p", "shot-line", line.speaker + ": \u201c" + line.text + "\u201d"));
    if (shot.framing || shot.camera_move || shot.audio || shot.notes) {
      const more = make("details", "shot-more");
      more.appendChild(make("summary", "", "Camera, audio and notes"));
      if (shot.framing) more.appendChild(make("p", "shot-meta", "Camera · " + shot.framing));
      if (shot.camera_move) more.appendChild(make("p", "shot-meta", "Move · " + shot.camera_move));
      if (shot.audio) more.appendChild(make("p", "shot-meta", "Audio · " + shot.audio));
      if (shot.notes) more.appendChild(make("p", "shot-meta", "Note · " + shot.notes));
      card.appendChild(more);
    }
    const status = make("div", "shot-status");
    status.appendChild(shotChip(imageReady ? "Image ready" : image ? "Image " + (image.state || "pending") : "No image yet", imageReady ? "done" : "wait", "stills"));
    status.appendChild(shotChip(clip && clip.state === "done" ? "Clip ready" : clip ? "Clip " + clip.state : "No clip yet", clip && clip.state === "done" ? "done" : "wait", "video"));
    card.appendChild(status);
    cards.appendChild(card);
  }
}

function syncImageExtras(state) {
  const images = state.images || [];
  const bay = document.getElementById("image-bay");
  const ratio = String(state.aspect_ratio || "16:9").replace(":", " / ");
  bay.style.setProperty("--shot-ratio", ratio);
  const counts = { done: 0, review: 0, failed: 0, running: 0 };
  for (const image of images) {
    if (image.state === "done") counts.done += 1;
    else if (image.state === "review") counts.review += 1;
    else if (image.state === "failed") counts.failed += 1;
    else counts.running += 1;
  }
  const parts = [counts.done + " of " + images.length + " images ready"];
  if (counts.review) parts.push(counts.review + " need your review");
  if (counts.running) parts.push(counts.running + " in progress or waiting");
  if (counts.failed) parts.push(counts.failed + " failed");
  const summary = document.getElementById("image-summary");
  summary.hidden = !images.length;
  summary.dataset.alert = counts.review || counts.failed ? "1" : "";
  setText(summary, parts.join(" · "));
  const empty = document.getElementById("image-empty");
  empty.hidden = images.length > 0;
  if (!images.length) {
    const action = document.getElementById("image-empty-action");
    if (state.can_proceed) {
      setText(document.getElementById("image-empty-text"), "Approve the script first. The image prompts are written after that, then one image is generated per shot.");
      setText(action, "Review the script");
    } else if (state.running) {
      setText(document.getElementById("image-empty-text"), "The crew is still working. Images appear here as they are generated.");
      setText(action, "Follow progress");
    } else {
      setText(document.getElementById("image-empty-text"), "Images appear here once a script is approved. Start a production in the Idea tab, or open one from the library.");
      setText(action, "Go to Idea");
    }
    action.dataset.view = state.can_proceed || state.running ? "pipeline" : "brief";
  }
  // Captions name the shot an image belongs to, and finished images can be saved.
  for (const image of images) {
    const frame = document.getElementById("frame-" + String(image.id).replace(/[^A-Za-z0-9_-]/g, "_"));
    if (!frame) continue;
    const shot = shotOf(state, image.id);
    const caption = frame.querySelector("figcaption");
    if (shot && !image.error) setText(caption, "Shot " + shot.number + " · " + shot.duration_seconds + "s · " + (shot.framing || image.kind));
    let link = frame.querySelector("a.frame-download");
    if (image.src && !link) {
      link = make("a", "frame-download", "Download");
      link.setAttribute("download", image.id);
      frame.appendChild(link);
    }
    if (link) { link.hidden = !image.src; if (image.src) link.href = image.src; }
  }
}

// Clips you ticked to animate next. Only shots with an approved image and no finished clip can be picked.
const clipPick = new Set();
const CLIP_BUSY = new Set(["submitting", "polling", "downloading", "uncertain"]);

function clipPickable(state, shot) {
  const video = state.video || {};
  const clip = clipOf(state, shot.id);
  return Boolean(video.ready) && !video.complete && (imageOf(state, shot.id) || {}).state === "done" &&
    !(clip && (clip.state === "done" || CLIP_BUSY.has(clip.state)));
}

function clipPrice(state, id) {
  const planned = (((state.video || {}).shots) || []).find((shot) => shot.id === id) || {};
  return typeof planned.credits === "number" ? planned.credits : null;
}

function syncClipPick(state) {
  const shots = state.shots || [];
  const running = Boolean(state.running);
  for (const id of [...clipPick]) {
    const shot = shots.find((item) => item.id === id);
    if (!shot || !clipPickable(state, shot)) clipPick.delete(id);
  }
  const pickable = shots.filter((shot) => clipPickable(state, shot));
  document.getElementById("clip-pick").hidden = !pickable.length;
  const chosen = [...clipPick];
  const total = chosen.reduce((sum, id) => sum + (clipPrice(state, id) || 0), 0);
  const button = document.getElementById("animate-selected");
  setText(button, chosen.length
    ? "Animate selected (" + chosen.length + ")" + (total ? " · about " + Math.round(total).toLocaleString() + " credits" : "")
    : "Tick the clips to animate");
  button.disabled = running || !chosen.length;
  document.getElementById("select-remaining").disabled = running || pickable.every((shot) => clipPick.has(shot.id));
  document.getElementById("clear-selection").disabled = running || !chosen.length;
  document.querySelectorAll("#video-ready-list input[type=checkbox]").forEach((box) => {
    box.checked = clipPick.has(box.value);
    box.disabled = running || !pickable.some((shot) => shot.id === box.value);
  });
}

function syncVideoExtras(state) {
  const shots = state.shots || [];
  const video = state.video || {};
  const clips = video.clips || [];
  const details = document.getElementById("video-ready");
  details.hidden = !shots.length;
  if (shots.length) {
    const readyCount = shots.filter((shot) => (imageOf(state, shot.id) || {}).state === "done").length;
    const remaining = shots.filter((shot) => clipPickable(state, shot)).length;
    setText(document.getElementById("video-ready-title"), video.ready && remaining
      ? "Choose clips to animate · " + remaining + " not animated yet"
      : "Shots and images · " + readyCount + " of " + shots.length + " ready");
    const key = JSON.stringify([shots.map((shot) => [shot.id, (imageOf(state, shot.id) || {}).state, (imageOf(state, shot.id) || {}).src,
      (clipOf(state, shot.id) || {}).state, clipPrice(state, shot.id)]), Boolean(video.ready), Boolean(video.complete)]);
    const list = document.getElementById("video-ready-list");
    if (list.dataset.key !== key) {
      list.dataset.key = key;
      list.replaceChildren();
      const clipText = { done: "Clip ready", pending: "Not animated yet", failed: "Clip failed: animate it again", submitting: "Clip sending", polling: "Clip rendering", downloading: "Clip downloading", uncertain: "Clip needs checking" };
      for (const shot of shots) {
        const image = imageOf(state, shot.id);
        const clip = clipOf(state, shot.id);
        const ok = Boolean(image && image.state === "done");
        const row = make("li", "ready-row" + (ok ? " is-ok" : ""));
        const pick = document.createElement("input");
        pick.type = "checkbox";
        pick.value = shot.id;
        pick.id = "pick-" + shot.id;
        pick.setAttribute("aria-label", "Animate shot " + shot.number);
        pick.addEventListener("change", () => {
          if (pick.checked) clipPick.add(shot.id); else clipPick.delete(shot.id);
          syncClipPick(last || state);
        });
        row.appendChild(pick);
        if (image && image.src) { const thumb = make("img", "ready-thumb"); thumb.src = image.src; thumb.alt = ""; thumb.loading = "lazy"; row.appendChild(thumb); }
        else row.appendChild(make("span", "ready-thumb is-empty"));
        const label = make("label", "", "Shot " + shot.number + " · " + shot.duration_seconds + "s");
        label.htmlFor = pick.id;
        const price = clipPrice(state, shot.id);
        const status = !ok
          ? (image ? "Image " + (image.state || "pending") + ": finish it in the Images tab" : "No image yet: generate it in the Images tab")
          : (clip ? clipText[clip.state] || "Clip " + clip.state : "Image approved") +
            (price !== null && !(clip && clip.state === "done") ? " · about " + Math.round(price).toLocaleString() + " credits" : "");
        row.append(label, make("span", "ready-text", status));
        list.appendChild(row);
      }
      if (!details.dataset.touched) details.open = readyCount < shots.length || (Boolean(video.ready) && remaining > 0);
    }
    syncClipPick(state);
  }
  const planned = video.shots || [];
  const seconds = (planned.length ? planned : shots).reduce((sum, shot) => sum + shot.duration_seconds, 0);
  const models = {};
  for (const shot of planned) models[shot.model_name || "Kling 3.0"] = (models[shot.model_name || "Kling 3.0"] || 0) + 1;
  const summary = [];
  if (shots.length) summary.push(shots.length + (shots.length === 1 ? " clip" : " clips") + " · " + seconds + "s of video");
  const mix = Object.entries(models).map(([name, count]) => name + " × " + count).join(", ");
  if (mix) summary.push(mix);
  if (video.mode) summary.push({ std: "720p", pro: "1080p", "4K": "4K" }[video.mode] + (video.sound === false ? " · silent" : " · with sound"));
  setText(document.getElementById("video-summary"), summary.join(" · "));
  const cost = document.getElementById("video-cost");
  const estimate = video.estimate;
  cost.hidden = !estimate;
  if (estimate) {
    setText(cost, (estimate.preliminary ? "Estimated with the current video settings: " : "Estimated: ") + credits(estimate.credits) + " for every clip" +
      (estimate.remaining > 0 && estimate.remaining !== estimate.credits ? " · " + credits(estimate.remaining) + " for the clips not finished yet" : "") +
      (estimate.unpriced && estimate.unpriced.length ? " · not priced: " + estimate.unpriced.join(", ") : "") + ".");
    cost.title = priceSource(estimate) + " " + (estimate.extras || "");
  }
  document.getElementById("download-all").hidden = !clips.some((clip) => clip.state === "done");
}

function syncExtras(state) {
  syncNextStep(state);
  syncShotBoard(state);
  syncImageExtras(state);
  syncVideoExtras(state);
}

function openStillsReview() {
  setView("stills", true);
  requestAnimationFrame(() => {
    const bay = document.getElementById("image-bay");
    if (!bay) return;
    let card = bay.querySelector('.frame[data-state="review"]');
    if (!card) return;
    const column = card.closest(".model-col");
    if (card.hidden || (column && column.hidden)) {
      stillFilter = "all";
      document.querySelectorAll("#still-tools .chip").forEach((button) => {
        button.classList.toggle("is-on", button.dataset.filter === "all");
      });
      applyStillFilter();
      card = bay.querySelector('.frame[data-state="review"]');
    }
    if (card) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
}

function placeCostChoices(besideProceed) {
  const inspection = document.getElementById("inspection-field");
  const dry = document.getElementById("dry-option");
  const slot = document.getElementById("cost-choices");
  const inspectionHome = document.getElementById("inspection-home");
  const dryHome = document.getElementById("dry-home");
  if (!inspection || !dry || !slot || !inspectionHome || !dryHome) return;
  if (besideProceed) {
    if (inspection.parentNode !== slot) slot.appendChild(inspection);
    if (dry.parentNode !== slot) slot.appendChild(dry);
  } else {
    if (inspection.parentNode !== inspectionHome) inspectionHome.appendChild(inspection);
    if (dry.parentNode !== dryHome) dryHome.appendChild(dry);
  }
}

function syncProceed(state) {
  const running = !!state.running;
  const showProceed = !!state.can_proceed && !running;
  const showReview = !showProceed && !running && (reviewWaiting(state) || stillsPaused(state));
  const showResume = showReview && savedImageWorkCanResume(state);
  placeCostChoices(showProceed);
  const button = document.getElementById("proceed");
  const abort = document.getElementById("abort-draft");
  const note = document.getElementById("proceed-note");
  const row = document.getElementById("proceed-row");
  const review = document.getElementById("review-stills");
  const resume = document.getElementById("resume-images");
  const jump = document.getElementById("activity-jump");
  if (row) row.hidden = !showProceed;
  button.hidden = !showProceed;
  note.hidden = !showProceed;
  button.disabled = !showProceed;
  const approving = state.pages_passed === true;
  setText(button, approving ? "Approve pages" : "Proceed with this draft");
  setText(note, approving
    ? "The editor passed these pages. Select a step below to read them. Approving starts the image prompts and image generation. Plan images and image inspection beside this button set the cost."
    : "Sends this script to the image prompts and image generation. The editor's verdict stays on file. Plan images and image inspection beside this button set the cost.");
  const cost = document.getElementById("proceed-cost");
  const estimate = state.estimate;
  if (cost) {
    cost.hidden = !showProceed || !estimate;
    if (estimate) {
      const dry = document.getElementById("dry").checked;
      const images = estimate.images;
      const videos = estimate.videos;
      const seconds = videos.shots.reduce((sum, shot) => sum + shot.seconds, 0);
      setText(cost, (dry ? "Plan images only: no image credits now. " : "Images now: about " + images.count + " at " + images.resolution + ", " + credits(images.credits) + ". ") +
        "Video clips later: " + seconds + "s, " + credits(videos.credits) +
        (videos.unpriced.length ? " (not priced: " + videos.unpriced.join(", ") + ")" : "") + ". Chat calls, inspections and repairs are extra.");
      cost.title = [images.assumption, priceSource(estimate)].filter(Boolean).join(" ");
    }
  }
  if (abort) {
    abort.hidden = !showProceed;
    abort.disabled = !showProceed;
  }
  if (review) {
    review.hidden = !showReview;
    review.disabled = !showReview;
  }
  if (resume) {
    resume.hidden = !showResume;
    resume.disabled = !showResume;
    const left = state.images_estimate;
    setText(resume, left && left.credits ? "Resume saved image work · about " + credits(left.credits) : "Resume saved image work");
    resume.title = left ? left.count + (left.count === 1 ? " image is" : " images are") + " not finished. Inspections and repairs are extra." : "";
    resume.classList.remove("go");
    resume.classList.add("ghost");
  }
  if (jump) jump.hidden = !reviewWaiting(state);
}

function syncImages(state) {
  const bay = document.getElementById("image-bay");
  const before = bay.querySelectorAll(".frame[data-job]").length;
  for (const agent of state.image_agents || []) ensureColumn(bay, agent.model, agent.kinds);
  const live = new Set((state.images || []).map((image) => image.id));
  bay.querySelectorAll(".frame[data-job]").forEach((frame) => {
    if (!live.has(frame.dataset.job)) frame.remove();
  });
  for (const image of state.images || []) {
    const col = ensureColumn(bay, image.model || "still", image.kind || "");
    const frame = ensureFrame(col, image.id);
    frame.dataset.kind = image.kind || "";
    const caption = image.error ? image.id + " · " + image.kind + " — " + image.error : image.id + " · " + image.kind;
    setText(frame.querySelector("figcaption"), caption);
    const shown = (image.state || "") + (image.uncertain ? ":uncertain" : "");
    if (frame.dataset.shown !== shown) { applyFrameState(frame, image); frame.dataset.shown = shown; }
    else if (["done", "review", "inspecting"].includes(image.state)) showDone(frame, image);
    renderInspection(frame, image, state.running);
    renderImageTools(frame, image, state.running);
  }
  bay.querySelectorAll(".model-col").forEach((col) => {
    const jobs = col.querySelectorAll(".frame[data-job]");
    col.classList.toggle("has-jobs", jobs.length > 0);
    col.classList.toggle("is-working", [...jobs].some((frame) => frame.dataset.state === "running"));
  });
  applyStillFilter();
  if ((state.images || []).length > before && document.body.dataset.view !== "stills") {
    document.querySelector('.tab[data-tab="stills"]').classList.add("has-news");
  }
}

function syncLog(lines) {
  const box = document.getElementById("log");
  const head = lines[0] || "";
  if (lines.length < shown || box.dataset.head !== head) {
    box.replaceChildren();
    shown = 0;
  }
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 12;
  for (const line of lines.slice(shown)) {
    const p = document.createElement("p");
    p.textContent = plain(line);
    box.appendChild(p);
  }
  shown = lines.length;
  box.dataset.head = head;
  if (atBottom) box.scrollTop = box.scrollHeight;
}

const STEP_INFO = {
  brief: { step: "Brief", empty: "Write a brief and start production." },
  development: { step: "Step 1", empty: "Development starts when the idea is sent." },
  writer: { step: "Step 2", empty: "The screenwriter starts after development locks the story." },
  editor: { step: "Step 3", empty: "The script editor reads each draft after it is written." },
  approval: { step: "Step 4", empty: "Your approval comes after the script editor passes." },
  art: { step: "Step 5", empty: "The image prompts are written after the script is approved." }
};
let pickedStation = "";
let detailKey = "";
let detailTimer = 0;
let detailSerial = 0;

function autoStation(state) {
  const station = (state && state.station) || "brief";
  return station === "images" ? "art" : (STEP_INFO[station] ? station : "brief");
}

function shownStation() {
  return pickedStation || autoStation(last);
}

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = plain(text);
  return node;
}

// Sessions saved before lines carried a station are sorted by their opening words.
const LINE_STATIONS = [
  [/^\*\*Development\*\*/, "development"],
  [/^\*\*Screenwriter\*\*|^Pages:/, "writer"],
  [/^\*\*Script editor\*\*|^The editor (sent|passed)/, "editor"],
  [/^Proceeding with the saved draft|^You approved/, "approval"],
  [/^\*\*Art director\*\*/, "art"],
  [/^\*\*Stills?\*\*|^- |^Image |^Stills /, "images"]
];

function stationLines(state, id) {
  const tags = state.log_stations || [];
  let current = "";
  return (state.log || []).filter((line, index) => {
    if (tags[index]) current = tags[index];
    else {
      const match = LINE_STATIONS.find(([pattern]) => pattern.test(line));
      if (match) current = match[1];
    }
    return current === id;
  });
}

function detailField(host, label, value) {
  if (!value) return;
  const row = make("div", "detail-field");
  row.append(make("span", "", label), make("p", "", value));
  host.appendChild(row);
}

function detailList(host, label, items, ordered) {
  if (!items || !items.length) return;
  host.appendChild(make("h4", "", label));
  const list = make(ordered ? "ol" : "ul", "detail-list");
  for (const item of items) list.appendChild(make("li", "", item));
  host.appendChild(list);
}

function detailChips(host, items) {
  const row = make("div", "detail-chips");
  for (const item of items.filter(Boolean)) row.appendChild(make("span", "detail-chip", item));
  if (row.children.length) host.appendChild(row);
}

function modeChip(id, mode) {
  const chip = make("span", "detail-chip" + (mode ? " is-" + mode : ""), id);
  if (mode) chip.title = ({ style: "Style: colour, light and texture", character: "Character: selected face and wardrobe attributes",
    location: "Location: setting and architecture", object: "Object / Product: shape, materials and details",
    composition: "Composition: framing and subject placement", lock: "Locked reference: same subject" })[mode] || mode;
  return chip;
}

const STAGE_OUTPUT = {
  brief(host, d, state) {
    const text = d.brief || state.brief || "";
    if (!text) return false;
    host.appendChild(make("p", "detail-lede", text));
    if (d.image_count) detailField(host, "Image limit", d.image_count + " new images");
    const refs = d.references || [];
    if (refs.length) {
      host.appendChild(make("h4", "", "Reference images"));
      const row = make("div", "detail-chips");
      for (const ref of refs) row.appendChild(modeChip((ref.name || ref.id) + " · " + (ref.mode || "lock"), ref.mode));
      host.appendChild(row);
    }
    return true;
  },
  development(host, d) {
    if (!d.title && !d.logline) return false;
    host.appendChild(make("h4", "detail-title", d.title || "Untitled"));
    detailChips(host, [d.job_type, d.runtime_seconds ? d.runtime_seconds + "s" : "", d.aspect_ratio, d.tone]);
    if (d.logline) host.appendChild(make("p", "detail-lede", d.logline));
    detailField(host, "Look", d.look);
    detailField(host, "Palette", d.palette);
    detailField(host, "Lighting", d.lighting);
    detailList(host, "Characters", (d.characters || []).map((c) => c.name + " — " + c.description));
    detailList(host, "Locations", d.locations || []);
    detailList(host, "Beats", (d.beats || []).map((b) => b.timecode + "  " + b.name + ": " + b.action), true);
    detailList(host, "Invented defaults", d.invented || []);
    return true;
  },
  writer(host, d) {
    const shots = d.shots || [];
    if (!d.drafts && !shots.length) return false;
    const drafts = d.drafts === 1 ? "1 draft written" : d.drafts + " drafts written";
    host.appendChild(make("p", "detail-meta", drafts + (d.final ? " · final pages saved" : "")));
    if (shots.length) {
      const wrap = make("div", "detail-table");
      const table = document.createElement("table");
      const head = document.createElement("tr");
      for (const label of ["#", "Dur", "Framing", "Action"]) head.appendChild(make("th", "", label));
      table.appendChild(head);
      for (const shot of shots) {
        const row = document.createElement("tr");
        for (const value of [shot.number, shot.duration_seconds + "s", shot.framing, shot.action]) row.appendChild(make("td", "", value));
        table.appendChild(row);
      }
      wrap.appendChild(table);
      host.appendChild(wrap);
    }
    detailList(host, "Writer's notes", d.notes || []);
    return true;
  },
  editor(host, d) {
    const verdicts = d.verdicts || [];
    if (!verdicts.length) return false;
    for (const verdict of verdicts) {
      const card = make("div", "verdict" + (verdict.passed ? " is-pass" : " is-fail"));
      const head = make("p", "verdict-head");
      head.append(make("strong", "", "Draft " + verdict.draft), make("span", "", verdict.passed ? "Passed" : "Sent back"));
      card.appendChild(head);
      if (verdict.issues.length) {
        const list = make("ul", "detail-list");
        for (const issue of verdict.issues) list.appendChild(make("li", "", issue));
        card.appendChild(list);
      }
      host.appendChild(card);
    }
    return true;
  },
  approval(host, d, state) {
    let text = "";
    if (d.approved) text = "You approved this script. The image prompts and image generation continued from it.";
    else if (d.aborted) text = "This draft was aborted. The pages stay in the production folder.";
    else if (d.waiting && d.passed) text = "The editor passed these pages. Read the script and the shot list, then choose Approve pages above. Abort draft stops here and keeps every file.";
    else if (d.waiting) text = "The editor did not pass these pages after two rewrites. You can proceed with them anyway, or abort the draft.";
    else if ((state.done || []).includes("art") || (state.images || []).length) text = "This run continued to images without waiting for approval.";
    if (!text) return false;
    host.appendChild(make("p", "detail-lede", text));
    return true;
  },
  art(host, d) {
    const jobs = d.jobs || [];
    if (!jobs.length) return false;
    host.appendChild(make("p", "detail-meta", jobs.length + " image jobs · " + (d.submitted ? "sent to generation" : "planned")));
    const list = make("ol", "job-list");
    for (const job of jobs) {
      const item = document.createElement("li");
      const head = make("div", "job-head");
      head.append(make("strong", "", job.id), make("span", "detail-chip", job.kind));
      for (const ref of job.references || []) head.appendChild(modeChip(ref.id, ref.mode));
      item.appendChild(head);
      const details = document.createElement("details");
      details.dataset.key = "job:" + job.id;
      details.append(make("summary", "", "Prompt"), make("p", "job-prompt", job.prompt || ""));
      item.appendChild(details);
      list.appendChild(item);
    }
    host.appendChild(list);
    return true;
  }
};

function renderStageOutput(id, data) {
  const host = document.getElementById("detail-output");
  const files = document.getElementById("detail-files");
  const open = new Set([...host.querySelectorAll("details[open]")].map((node) => node.dataset.key));
  host.replaceChildren();
  files.replaceChildren();
  const filled = STAGE_OUTPUT[id](host, (data && data.data) || {}, last || {});
  if (!filled) host.appendChild(make("p", "detail-empty", STEP_INFO[id].empty));
  host.querySelectorAll("details").forEach((node) => { if (open.has(node.dataset.key)) node.open = true; });
  for (const file of (data && data.files) || []) {
    const button = make("button", "ghost", "Open " + file.name);
    button.type = "button";
    button.addEventListener("click", () => openFile(file.path));
    files.appendChild(button);
  }
  files.hidden = !files.children.length;
}

async function loadStageDetail(id) {
  const serial = ++detailSerial;
  let data = null;
  try {
    const res = await fetch("/api/stage?station=" + encodeURIComponent(id));
    if (res.ok) data = await res.json();
  } catch (error) { /* The empty state explains the step. */ }
  if (serial !== detailSerial || id !== shownStation()) return;
  renderStageOutput(id, data);
}

function paintStageDetail(state) {
  const id = shownStation();
  document.querySelectorAll("#track .station").forEach((station) => {
    const on = station.dataset.station === id;
    station.classList.toggle("is-selected", on);
    station.querySelector(".piece").setAttribute("aria-pressed", on ? "true" : "false");
  });
  const station = stationEl(id);
  const model = station.querySelector(".model").textContent;
  setText(document.getElementById("detail-kicker"), STEP_INFO[id].step + (model && id !== "brief" ? " · " + model : ""));
  setText(document.getElementById("detail-title"), station.querySelector("strong").textContent);
  const status = document.getElementById("detail-status");
  setText(status, station.querySelector(".status").textContent);
  status.dataset.mode = station.dataset.mode || "idle";
  document.getElementById("detail-follow").hidden = !pickedStation;
  const lines = stationLines(state, id);
  const log = document.getElementById("detail-log");
  const signature = id + ":" + lines.length + ":" + (lines[0] || "");
  if (log.dataset.signature !== signature) {
    log.dataset.signature = signature;
    log.replaceChildren();
    for (const line of lines) log.appendChild(make("li", "", line));
    if (!lines.length) log.appendChild(make("li", "detail-empty", "No activity yet."));
    log.scrollTop = log.scrollHeight;
  }
  const key = [id, state.folder, (state.done || []).join(","), state.active, state.running, lines.length, state.can_proceed, state.pages_passed, (state.brief || "").length].join("|");
  if (key !== detailKey) {
    detailKey = key;
    clearTimeout(detailTimer);
    detailTimer = setTimeout(() => loadStageDetail(id), 200);
  }
}

function pickStation(id) {
  pickedStation = id;
  detailKey = "";
  if (last) paintStageDetail(last);
}

let videoPromptKey = "";
// Model, quality and sound come from the Idea tab's settings. The server applies them
// to the saved production, so this tab only reports what will be used.
function syncVideo(state) {
  const video = state.video || {};
  const running = Boolean(state.running);
  setText(document.getElementById("video-message"),
    state.active === "video" && running ? state.note : video.message || "Approve the images to continue.");
  const names = {auto: "Auto-chosen models", kling: "Kling 3.0", seedance: "Seedance 2.5"};
  const quality = {std: "Standard 720p", pro: "Pro 1080p", "4K": "4K"};
  setText(document.getElementById("video-settings-note"),
    (names[video.selection] || names.auto) + " · " + (quality[video.mode] || quality.pro) + " · " +
    (video.sound === false ? "silent" : "with sound") +
    (video.saved ? " · locked for this production once clips are generated." : " · change these in Video on the Idea tab."));
  document.getElementById("plan-video").disabled = running || !video.can_plan;
  const generate = document.getElementById("generate-video");
  generate.disabled = running || !video.ready || Boolean(video.complete);
  setText(generate, video.complete ? "All clips ready" : video.saved ? "Animate all remaining clips" : "Animate all clips");
  const host = document.getElementById("video-clips");
  const clips = video.clips || [];
  const ids = new Set(clips.map((clip) => clip.id));
  for (const node of [...host.children]) if (!ids.has(node.dataset.clip)) node.remove();
  for (const clip of clips) {
    let card = [...host.children].find((node) => node.dataset.clip === clip.id);
    if (!card) {
      card = make("article", "video-clip");
      card.dataset.clip = clip.id;
      card.append(make("h3"), make("img", "clip-thumb"), make("div", "clip-meta"), make("p"), make("video"), make("a", "video-download", "Download clip"), make("div", "takes"), make("button", "ghost"));
      card.querySelector("button").type = "button";
      card.querySelector("button").addEventListener("click", () => {
        const done = card.dataset.state === "done";
        startVideo(false, done ? { redo: [clip.id] } : { shots: [clip.id] });
      });
      const preview = card.querySelector("video");
      preview.controls = true;
      preview.playsInline = true;
      preview.preload = "none";
      host.appendChild(card);
    }
    const clipStates = { done: "ready", pending: "not started", submitting: "sending", polling: "rendering", downloading: "downloading", failed: "failed", uncertain: "needs checking" };
    setText(card.querySelector("h3"), shotLabel(state, clip.id) + " · " + modelTitle(clip.model || "Video") + " · " + (clipStates[clip.state] || clip.state));
    setText(card.querySelector("p"), clip.error || clip.audio_note || "");
    card.dataset.state = clip.state;
    const source = imageOf(state, clip.id);
    const planned = (video.shots || []).find((shot) => shot.id === clip.id) || {};
    const thumb = card.querySelector(".clip-thumb");
    thumb.hidden = Boolean(clip.src) || !(source && source.src);
    if (source && source.src && thumb.getAttribute("src") !== source.src) { thumb.src = source.src; thumb.alt = "First frame of " + clip.id; }
    setText(card.querySelector(".clip-meta"), [planned.duration_seconds ? planned.duration_seconds + "s" : "", shotOf(state, clip.id) ? shotOf(state, clip.id).action : ""].filter(Boolean).join(" · "));
    card.querySelector("video").poster = source && source.src ? source.src : "";
    const action = card.querySelector("button");
    const price = typeof planned.credits === "number" ? " · about " + Math.round(planned.credits).toLocaleString() + " credits" : " · paid";
    setText(action, (clip.state === "done" ? "Redo this shot" : "Render this shot") + price);
    action.disabled = running || !video.ready || !video.saved;
    let forget = card.querySelector(".forget-clip");
    if (clip.state === "uncertain" || clip.state === "submitting") {
      if (!forget) {
        forget = make("button", "ghost forget-clip", "Not on KIE? Send it again");
        forget.type = "button";
        forget.title = "Use this only after checking KIE's task history shows no task for this shot.";
        forget.addEventListener("click", () => forgetClip(clip.id));
        card.appendChild(forget);
      }
      forget.disabled = running;
    } else if (forget) {
      forget.remove();
    }
    action.hidden = Boolean(video.saved) === false;
    const preview = card.querySelector("video");
    preview.hidden = !clip.src;
    if (clip.src && preview.getAttribute("src") !== clip.src) preview.src = clip.src;
    const download = card.querySelector("a.video-download");
    download.hidden = !clip.src;
    if (clip.src) { download.href = clip.src + "&download=1"; download.setAttribute("download", clip.id + ".mp4"); }
    const takes = card.querySelector(".takes");
    const takeKey = JSON.stringify([clip.takes || [], running, clip.state]);
    if (takes.dataset.key !== takeKey) {
      takes.dataset.key = takeKey;
      takes.replaceChildren();
      for (const take of clip.takes || []) {
        const row = make("div", "take-row");
        const link = make("a", "", "Earlier take · " + take.name);
        link.href = take.src;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        const use = make("button", "ghost", "Use this take");
        use.type = "button";
        use.disabled = running || !["done", "failed", "pending"].includes(clip.state);
        use.addEventListener("click", () => useTake(clip.id, take.name));
        row.append(link, use);
        takes.appendChild(row);
      }
    }
    let edit = card.querySelector(".clip-edit");
    const canEdit = Boolean(video.saved) && Boolean(video.ready) && planned.prompt;
    if (canEdit && !edit) {
      edit = make("details", "clip-edit");
      edit.append(make("summary", "", "Edit prompt"), document.createElement("textarea"), make("button", "go"));
      const box = edit.querySelector("textarea");
      box.rows = 7;
      box.setAttribute("aria-label", "Video prompt for " + clip.id);
      edit.querySelector("button").type = "button";
      edit.querySelector("button").addEventListener("click", () => {
        const text = box.value.trim();
        if (!text) { toast("Write a prompt first."); return; }
        const done = card.dataset.state === "done";
        startVideo(false, done ? { redo: [clip.id], prompts: { [clip.id]: text } } : { shots: [clip.id], prompts: { [clip.id]: text } });
      });
      card.insertBefore(edit, takes);
    }
    if (edit) {
      edit.hidden = !canEdit;
      const box = edit.querySelector("textarea");
      if (canEdit && box.dataset.planned !== planned.prompt) {
        // Refresh the text only when the saved prompt changed, never while you are typing.
        if (box.value === (box.dataset.planned || "") || !box.value) box.value = planned.prompt;
        box.dataset.planned = planned.prompt;
      }
      setText(edit.querySelector("summary"), planned.original_prompt ? "Prompt (edited)" : "Edit prompt");
      const price = typeof planned.credits === "number" ? " · about " + Math.round(planned.credits).toLocaleString() + " credits" : "";
      setText(edit.querySelector("button"), (clip.state === "done" ? "Redo with this prompt" : "Render with this prompt") + price);
      edit.querySelector("button").disabled = running;
    }
  }
  const shots = video.shots || [];
  const promptKey = JSON.stringify(shots);
  if (videoPromptKey !== promptKey) {
    videoPromptKey = promptKey;
    const prompts = document.getElementById("video-prompts");
    prompts.replaceChildren();
    for (const shot of shots) {
      prompts.append(make("h3", "", shot.id + " · " + shot.duration_seconds + "s · " + (shot.model_name || "Kling 3.0") +
        (shot.original_prompt ? " · prompt edited" : "") +
        (typeof shot.credits === "number" ? " · about " + credits(shot.credits) : "")),
        make("p", "field-note", shot.selection_reason || "Original saved Kling plan."), make("pre", "", shot.prompt));
      const sources = make("p", "field-note");
      for (const [index, source] of (shot.sources || []).entries()) {
        const link = make("a", "", "Official documentation " + (index + 1));
        link.href = safeUrl(source);
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        sources.append(link, document.createTextNode(" "));
      }
      prompts.append(sources);
    }
  }
  document.getElementById("video-plan").hidden = !shots.length;
}

function paidVideoQuestion(only) {
  const video = (last && last.video) || {};
  const quality = { std: "720p", pro: "1080p", "4K": "4K" }[video.mode] || "1080p";
  const sound = video.sound === false ? "silent" : "with sound";
  const picked = only.redo || only.shots || [];
  if (picked.length > 1) {
    const sum = picked.reduce((total, id) => total + (clipPrice(last || {}, id) || 0), 0);
    return "Animate " + picked.length + " clips (" + picked.map((id) => shotLabel(last || {}, id)).join(", ") + ") at " + quality + ", " + sound +
      "? Each is a paid KIE job." + (sum ? " Estimated cost: " + credits(sum) + "." : "") + " The other shots are left for later.";
  }
  const one = picked[0];
  const planned = (video.shots || []).find((shot) => shot.id === one) || {};
  const each = typeof planned.credits === "number" ? ", about " + credits(planned.credits) : "";
  if (only.redo) return "Regenerate " + shotLabel(last || {}, one) + "? This is a paid KIE job (" + quality + ", " + sound + each + "). The current take is kept as an earlier take.";
  if (only.shots) return "Render " + shotLabel(last || {}, one) + "? This is a paid KIE job (" + quality + ", " + sound + each + ").";
  const shots = video.shots || (last && last.shots) || [];
  const seconds = shots.reduce((sum, shot) => sum + (shot.duration_seconds || 0), 0);
  const pending = (video.clips || []).filter((clip) => clip.state !== "done").length || shots.length;
  const estimate = video.estimate;
  const total = estimate ? " Estimated cost: " + credits(video.saved ? estimate.remaining : estimate.credits) + "." : "";
  return "Generate " + pending + (pending === 1 ? " clip" : " clips") + (seconds ? " (" + seconds + "s of video in total)" : "") + " at " + quality + ", " + sound + "? Each clip is a paid KIE job." + total;
}

async function startVideo(dryRun, only = {}) {
  const single = Boolean(only.shots || only.redo);
  if (!dryRun && !window.confirm(paidVideoQuestion(only))) return;
  const button = single ? null : document.getElementById(dryRun ? "plan-video" : "generate-video");
  if (button) button.disabled = true;
  try {
    const response = await fetch("/api/video", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...only, dry_run: dryRun })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not start video generation.");
    if (document.body.dataset.view !== "video") setView("video");
    toast(dryRun ? "Saving the clip plan" : "Video generation started");
    await fetch("/api/status").then((res) => res.json()).then(apply);
  } catch (error) {
    toast(error.message, 6000);
    if (button) button.disabled = false;
  }
}

document.getElementById("next-step-action").addEventListener("click", () => {
  const goal = nextStepGoal || {};
  if (goal.action) goal.action();
  else if (goal.href) window.location.href = goal.href;
  else if (goal.view) setView(goal.view, true);
});
document.getElementById("image-empty-action").addEventListener("click", (event) => setView(event.currentTarget.dataset.view || "brief", true));
document.getElementById("video-ready").addEventListener("toggle", (event) => { event.currentTarget.dataset.touched = "1"; });
document.getElementById("plan-video").addEventListener("click", () => startVideo(true));
document.getElementById("generate-video").addEventListener("click", () => startVideo(false));
document.getElementById("animate-selected").addEventListener("click", () => {
  const order = (last && last.shots ? last.shots : []).map((shot) => shot.id);
  const chosen = order.filter((id) => clipPick.has(id));
  if (chosen.length) startVideo(false, { shots: chosen });
});
document.getElementById("select-remaining").addEventListener("click", () => {
  for (const shot of (last && last.shots) || []) if (clipPickable(last, shot)) clipPick.add(shot.id);
  syncClipPick(last);
});
document.getElementById("clear-selection").addEventListener("click", () => { clipPick.clear(); syncClipPick(last); });

function paint(state, prev) {
  const stage = document.getElementById("stage");
  const note = document.getElementById("note");
  if (note.dataset.sticky) {
    setText(note, note.dataset.sticky);
    note.classList.add("is-bad");
  } else {
    setText(note, noteText(state.note));
    note.classList.toggle("is-bad", /not set|failed|stopped|cancelled|error/i.test(state.note || ""));
  }
  const folder = document.getElementById("folder");
  setText(folder, state.folder || "");
  folder.hidden = !state.folder;
  if ((state.folder || "") !== deskFolder) {
    deskFolder = state.folder || "";
    renderReferences();
  }
  const plaque = document.getElementById("plaque");
  setText(plaque, state.brief || "");
  plaque.hidden = !state.brief;
  document.getElementById("live").hidden = !state.running;
  const stop = document.getElementById("stop");
  stop.hidden = !state.running;
  if (!state.running) stop.disabled = false;
  stage.classList.toggle("is-running", !!state.running);
  const run = document.getElementById("run");
  run.disabled = !!state.running;
  setText(run, state.running ? "Working…" : "Start production");
  paintCrew(state);
  if (state.can_proceed && !state.running && !(prev && prev.can_proceed && !prev.running)) pickedStation = "";
  paintStageDetail(state);
  const token = document.getElementById("token");
  const glyph = state.glyph || "sheet";
  if (token.dataset.glyph !== glyph) {
    token.dataset.glyph = glyph;
    token.innerHTML = GLYPHS[glyph] || GLYPHS.sheet;
  }
  if (!document.getElementById("return-token").dataset.ready) {
    document.getElementById("return-token").innerHTML = GLYPHS.page;
    document.getElementById("return-token").dataset.ready = "1";
  }
  syncImages(state);
  syncVideo(state);
  syncProceed(state);
  placeRail();
  paintProgress(state);
  const first = !prev;
  moveToken(state.station || "brief", !first);
  token.classList.toggle("is-hidden", !!state.returning);
  const label = document.getElementById("return-label");
  setText(label, state.returning ? "Script sent back" : "Editor loop · up to two rewrites");
  label.classList.toggle("is-live", !!state.returning);
  if (state.returning && !(prev && prev.returning)) {
    playReturn(first);
    if (!first) {
      stage.classList.remove("shake");
      void stage.offsetWidth;
      stage.classList.add("shake");
    }
  } else if (!state.returning) {
    parkReturn();
  }
  const here = Math.max(0, PIPS.indexOf(state.station || "brief"));
  document.querySelectorAll(".pip").forEach((pip, index) => {
    pip.classList.toggle("is-on", index <= here);
    pip.classList.toggle("is-here", index === here);
  });
  syncLog(state.log || []);
  const stepsDone = document.querySelectorAll(".station.is-done").length;
  setText(document.getElementById("count-pipeline"), String(stepsDone));
  setText(document.getElementById("count-stills"), String((state.images || []).length));
  setText(document.getElementById("count-video"), String(((state.video && state.video.clips) || []).filter((clip) => clip.state === "done").length));
  setText(document.getElementById("count-activity"), String((state.log || []).length));
  syncTabProgress(state);
  syncExtras(state);
  if (!viewChosen) {
    const nextView = openingView(state);
    if (nextView) setView(nextView);
    viewChosen = true;
  } else if (follow && state.running) {
    const next = viewFor(state);
    if (document.body.dataset.view !== next) setView(next);
  }
}

function apply(state) {
  if (!state || typeof state.revision !== "number") return;
  // A status fetch can arrive after a newer pushed state; only a new server or a newer revision wins.
  const sameServer = !state.boot || state.boot === bootSeen;
  if (sameServer && state.revision <= revision) return;
  bootSeen = state.boot || bootSeen;
  if (last && last.folder !== state.folder) forgetProduction();
  const prev = last;
  last = state;
  revision = state.revision;
  paint(state, prev);
}

let bootSeen = "";

// Per-production caches and choices must not leak into the next production.
function forgetProduction() {
  pickedStation = "";
  shotBoardKey = "";
  videoPromptKey = "";
  const ready = document.getElementById("video-ready");
  if (ready) { delete ready.dataset.touched; }
  const list = document.getElementById("video-ready-list");
  if (list) { delete list.dataset.key; }
  document.getElementById("video-clips").replaceChildren();
}

function startDust() {}

function ideaCount() {
  const idea = document.getElementById("idea").value;
  const chars = idea.length;
  setText(document.getElementById("idea-count"), idea.trim() ? chars.toLocaleString() + (chars === 1 ? " character" : " characters") : "Empty");
}

function suggestLabel() {
  ideaCount();
  const button = document.getElementById("suggest");
  if (!button || button.dataset.busy === "1") return;
  const empty = !document.getElementById("idea").value.trim();
  const count = selectedRefs.size;
  const from = count ? " from " + count + (count === 1 ? " reference" : " references") : "";
  button.textContent = empty ? "Suggest a story" + from : "Strengthen this" + (count ? " with the references" : "");
  button.title = count
    ? "The story is built from the prompts the selected images were generated from and their roles: characters, locations, objects, style and composition. The pictures themselves are not sent."
    : "Select reference images below and the story will be built around them.";
}

function holdNote(message) {
  const note = document.getElementById("note");
  note.dataset.sticky = message;
  note.textContent = message;
  note.classList.add("is-bad");
}

function releaseNote() {
  const note = document.getElementById("note");
  delete note.dataset.sticky;
  if (!last) return;
  setText(note, noteText(last.note));
  note.classList.toggle("is-bad", /not set|failed|stopped|cancelled|error/i.test(last.note || ""));
}

async function suggestBrief() {
  const button = document.getElementById("suggest");
  const idea = document.getElementById("idea");
  if (button.dataset.busy === "1") return;
  button.dataset.busy = "1";
  button.disabled = true;
  button.textContent = selectedRefs.size ? "Reading the references…" : "Writing…";
  // Only the role, name and direction matter here; shot ranges are checked when the run starts.
  const references = [...selectedRefs].map(([path, ref]) => {
    const item = { path, mode: ref.mode };
    if (ref.name) item.name = ref.name;
    if (ref.instruction) item.instruction = ref.instruction;
    if (ref.mode === "character") { item.face = ref.face !== false; item.wardrobe = ref.wardrobe !== false; if (!item.face && !item.wardrobe) item.face = item.wardrobe = true; }
    return item;
  });
  try {
    const res = await fetch("/api/brief/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        idea: idea.value,
        runtime_seconds: document.getElementById("runtime").value ? Number(document.getElementById("runtime").value) : null,
        aspect_ratio: document.getElementById("aspect").value || null,
        max_shots: document.getElementById("max-shots").value ? Number(document.getElementById("max-shots").value) : null,
        references
      })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.brief) {
      const message = data.error || "Could not write a brief.";
      toast(message, 4000);
      if (/API key is not set/.test(message)) holdNote(message);
      return;
    }
    releaseNote();
    idea.value = data.brief;
    idea.focus();
    toast(references.length ? "Brief built around your references. Edit it freely." : "Brief ready to edit");
  } catch (error) {
    toast("Could not write a brief.", 4000);
  } finally {
    button.disabled = false;
    delete button.dataset.busy;
    suggestLabel();
  }
}

function productionSlug(folder) {
  const parts = String(folder || "").replace(/\\/g, "/").replace(/\/+$/, "").split("/").filter(Boolean);
  const at = parts.lastIndexOf("productions");
  if (at >= 0 && parts[at + 1]) return parts[at + 1];
  return parts[parts.length - 1] || "";
}

function referenceExtension(name) {
  const parts = String(name || "").toLowerCase().split(".");
  return parts.length > 1 ? parts.pop() : "";
}

function referenceCatalogKey() {
  const parts = [deskFolder, showEarlierRefs];
  for (const project of libraryProjects) {
    parts.push(project.slug + "=" + (project.images || []).map((item) => [item.path, item.label, item.earlier].join(":")).join(","));
  }
  parts.push(uploadedRefs.map((item) => item.id).join(","));
  return parts.join("\n");
}

const REFERENCE_ROLES = [
  ["style", "Style"], ["character", "Character"], ["location", "Location"],
  ["object", "Object / Product"], ["composition", "Composition"]
];
let referenceTraySignature = "";
// A library image the studio already knows (a character sheet, a saved reference) starts in that role.
function newReference(hint) {
  const known = Boolean(hint) && REFERENCE_ROLES.some(([mode]) => mode === hint.role);
  return { mode: known ? hint.role : "style", name: known ? hint.subject || "" : "", scope: "auto", shotsText: "",
    instruction: "", face: !(hint && hint.face === false), wardrobe: !(hint && hint.wardrobe === false) };
}
function unnamedReference(ref) {
  return SUBJECT_ROLES.has(ref.mode) && !ref.name.trim();
}
function saveReferences() {
  try {
    localStorage.setItem(REFERENCE_STORE, JSON.stringify({ selected: [...selectedRefs], uploads: uploadedRefs }));
  } catch (_) { /* The selection still works for this page. */ }
}
function loadReferences() {
  try {
    const saved = JSON.parse(localStorage.getItem(REFERENCE_STORE) || "{}");
    for (const [path, ref] of saved.selected || []) {
      if (typeof path === "string" && ref && REFERENCE_ROLES.some(([mode]) => mode === ref.mode)) {
        selectedRefs.set(path, { ...newReference(), ...ref });
      }
    }
    uploadedRefs = (saved.uploads || []).filter((item) => item && typeof item.id === "string");
  } catch (_) { /* Start with an empty selection. */ }
}
function savedReferencesHere() {
  const slug = productionSlug(deskFolder);
  const project = libraryProjects.find((item) => item.slug === slug);
  return slug && project ? project.references || [] : [];
}
function restoreButton() {
  const button = document.getElementById("reference-restore");
  const saved = savedReferencesHere();
  button.hidden = !saved.some((ref) => (selectedRefs.get(ref.path) || {}).mode !== ref.mode);
  button.textContent = "Use the " + (saved.length === 1 ? "reference" : saved.length + " references") +
    " from " + productionSlug(deskFolder);
}
function restoreReferences() {
  for (const ref of savedReferencesHere()) {
    selectedRefs.set(ref.path, { ...newReference(), mode: ref.mode, name: ref.name || "", instruction: ref.instruction || "",
      scope: ref.scope === "shots" ? "shots" : "auto", shotsText: (ref.shots || []).join(", "),
      face: ref.face !== false, wardrobe: ref.wardrobe !== false });
  }
  markReferenceSelection();
}
function referenceSignature() {
  return JSON.stringify([...selectedRefs]);
}
function referenceSummary() {
  if (!selectedRefs.size) return "None selected";
  const roles = REFERENCE_ROLES.map(([mode, label]) => {
    const count = [...selectedRefs.values()].filter((ref) => ref.mode === mode).length;
    return count ? count + " " + label.toLowerCase() : "";
  }).filter(Boolean).join(" · ");
  const unnamed = [...selectedRefs.values()].filter(unnamedReference).length;
  return unnamed ? roles + " · " + unnamed + " to name" : roles;
}
function referencePayload() {
  return [...selectedRefs].map(([path, ref]) => {
    const shots = new Set();
    if (ref.scope === "shots") {
      for (const part of ref.shotsText.split(",")) {
        const match = part.trim().match(/^(\d{1,3})(?:\s*-\s*(\d{1,3}))?$/);
        if (!match) throw new Error("Enter reference shots as numbers or ranges, for example 1, 3-5.");
        const first = Number(match[1]), last = Number(match[2] || match[1]);
        if (first < 1 || last > 240 || last < first) throw new Error("Reference shots must be between 1 and 240, in ascending ranges.");
        for (let n = first; n <= last; n++) shots.add(n);
      }
    }
    if (ref.mode === "character" && !ref.face && !ref.wardrobe) throw new Error("Choose Face, Wardrobe, or both for each character reference.");
    return { path, mode: ref.mode, name: ref.name, scope: ref.scope, shots: [...shots].sort((a, b) => a - b),
      instruction: ref.instruction, ...(ref.mode === "character" ? { face: ref.face, wardrobe: ref.wardrobe } : {}) };
  });
}

function renderReferenceTray() {
  const tray = document.getElementById("reference-selected");
  const signature = referenceSignature();
  if (signature === referenceTraySignature) return;
  const expanded = new Set([...tray.querySelectorAll("details[open]")].map((node) => node.dataset.path));
  referenceTraySignature = signature;
  tray.replaceChildren();
  for (const [path, ref] of selectedRefs) {
    const info = refInfo.get(path) || { name: path, src: "/library-media?path=" + encodeURIComponent(path) };
    const card = make("div", "ref-chosen");
    card.dataset.path = path;
    const img = document.createElement("img");
    img.alt = "";
    img.src = info.src;
    const body = make("div", "ref-chosen-body");
    const name = make("span", "ref-chosen-name", info.label || info.name);
    name.title = info.name;
    const choices = (key, label, options) => {
      const group = make("div", "segmented ref-options");
      group.setAttribute("role", "group");
      group.setAttribute("aria-label", label);
      for (const [value, text] of options) {
        const button = make("button", ref[key] === value ? "is-on" : "", text);
        button.type = "button";
        button.dataset.setting = key;
        button.dataset.value = value;
        button.setAttribute("aria-pressed", String(ref[key] === value));
        button.addEventListener("click", () => {
          ref[key] = value;
          markReferenceSelection();
          const updated = [...tray.children].find((item) => item.dataset.path === path);
          const selected = [...updated.querySelectorAll("button[data-setting]")]
            .find((item) => item.dataset.setting === key && item.dataset.value === value);
          selected?.focus();
        });
        group.appendChild(button);
      }
      return group;
    };
    const role = choices("mode", "Role for " + info.name, REFERENCE_ROLES);
    const settings = document.createElement("details");
    settings.className = "ref-settings";
    settings.dataset.path = path;
    settings.open = expanded.has(path);
    settings.appendChild(make("summary", "", "Reference settings"));
    const fields = make("div", "ref-settings-fields");
    settings.appendChild(fields);
    // Updating the signature while typing keeps library polling from replacing focused controls.
    const changed = () => { referenceTraySignature = referenceSignature(); };
    const field = (title, control) => {
      const label = make(control.tagName === "DIV" ? "div" : "label", "ref-setting", title);
      label.appendChild(control);
      fields.appendChild(label);
    };
    const named = document.createElement("input");
    named.type = "text"; named.maxLength = 120; named.value = ref.name;
    named.placeholder = { character: "e.g. Maya", location: "e.g. station hall", object: "e.g. red watch" }[ref.mode]
      || "e.g. Maya, station hall, red watch";
    const subject = SUBJECT_ROLES.has(ref.mode);
    const warning = make("p", "ref-warning", "Name it exactly as the script does, so it is matched to the right shots.");
    warning.hidden = !unnamedReference(ref);
    named.addEventListener("input", () => {
      ref.name = named.value;
      changed();
      warning.hidden = !unnamedReference(ref);
      markReferenceSelection();
    });
    if (!subject) field("Name / subject", named);
    const scope = choices("scope", "Applies to", [
      ["auto", ref.mode === "style" ? "All shots" : "Relevant shots"], ["shots", "Specific shots"]
    ]);
    field("Applies to", scope);
    if (ref.scope === "shots") {
      const shots = document.createElement("input");
      shots.type = "text"; shots.value = ref.shotsText; shots.placeholder = "1, 3-5"; shots.maxLength = 1000;
      shots.addEventListener("input", () => { ref.shotsText = shots.value; changed(); });
      field("Shot numbers", shots);
      fields.appendChild(make("p", "field-note", "Use the final script’s shot numbers, for example 1, 3–5."));
    }
    if (ref.mode === "character") {
      const checks = make("div", "ref-character-options");
      for (const [key, label] of [["face", "Face"], ["wardrobe", "Wardrobe"]]) {
        const wrapper = make("label", "");
        const input = document.createElement("input");
        input.type = "checkbox"; input.checked = ref[key];
        input.addEventListener("change", () => { ref[key] = input.checked; changed(); });
        wrapper.append(input, document.createTextNode(label));
        checks.appendChild(wrapper);
      }
      fields.appendChild(checks);
    }
    const instruction = document.createElement("textarea");
    instruction.rows = 2; instruction.maxLength = 1000; instruction.value = ref.instruction;
    instruction.className = "ref-instruction";
    instruction.placeholder = "e.g. Keep the coat, use the scripted hairstyle";
    instruction.addEventListener("input", () => { ref.instruction = instruction.value; changed(); });
    field("Additional instruction (optional)", instruction);
    body.append(name, role);
    if (subject) {
      const inline = make("label", "ref-name-inline", "Name in the script");
      inline.appendChild(named);
      body.append(inline, warning);
    }
    const remove = make("button", "ref-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", "Remove " + info.name);
    remove.addEventListener("click", () => { selectedRefs.delete(path); markReferenceSelection(); });
    card.append(img, body, remove, settings);
    tray.appendChild(card);
  }
  tray.hidden = !selectedRefs.size;
}

function markReferenceSelection() {
  document.querySelectorAll("#reference-picker .ref-thumb").forEach((button) => {
    const ref = selectedRefs.get(button.dataset.ref);
    button.classList.toggle("is-selected", !!ref);
    button.dataset.badge = ref ? REFERENCE_ROLES.find(([mode]) => mode === ref.mode)[1] : "";
    button.setAttribute("aria-pressed", ref ? "true" : "false");
  });
  const count = document.getElementById("reference-count");
  count.textContent = referenceSummary();
  count.classList.toggle("is-warning", [...selectedRefs.values()].some(unnamedReference));
  suggestLabel();
  document.getElementById("reference-clear").hidden = !selectedRefs.size;
  renderReferenceTray();
  briefSummary();
  restoreButton();
  saveReferences();
}

function referenceThumb(item) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "ref-thumb";
  button.dataset.ref = item.path;
  button.title = item.label ? item.label + " (" + item.name + ")" : item.name || item.path;
  button.classList.toggle("is-earlier", Boolean(item.earlier));
  const img = document.createElement("img");
  img.alt = "";
  img.src = item.src || ("/library-media?path=" + encodeURIComponent(item.path));
  refInfo.set(item.path, { name: item.name || item.path, label: item.label || "", src: img.src });
  const caption = document.createElement("span");
  caption.textContent = item.label || item.name || item.path;
  button.append(img, caption);
  button.addEventListener("click", () => {
    if (selectedRefs.has(item.path)) selectedRefs.delete(item.path);
    else selectedRefs.set(item.path, newReference(item));
    markReferenceSelection();
  });
  return button;
}

function fillReferenceGrid(container, items, emptyText) {
  container.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "ref-empty";
    empty.textContent = emptyText;
    container.appendChild(empty);
    return;
  }
  for (const item of items) container.appendChild(referenceThumb(item));
}

function renderReferences() {
  const current = document.getElementById("reference-current");
  if (!current) return;
  const key = referenceCatalogKey();
  if (key === referenceKey) {
    markReferenceSelection();
    return;
  }
  referenceKey = key;
  const slug = productionSlug(deskFolder);
  const label = document.getElementById("reference-current-label");
  label.textContent = slug ? "This production · " + slug : "This production";
  const mine = [];
  const others = [];
  const known = new Set(uploadedRefs.map((item) => item.id));
  let earlier = 0;
  for (const project of libraryProjects) {
    const images = (project.images || []).filter((item) => REFERENCE_EXT.has(referenceExtension(item.path || item.name)));
    images.forEach((item) => known.add(item.path));
    earlier += images.filter((item) => item.earlier).length;
    // Earlier takes and repairs stay out of the way unless asked for or already chosen.
    const shown = images.filter((item) => !item.earlier || showEarlierRefs || selectedRefs.has(item.path));
    if (slug && project.slug === slug) mine.push(...shown);
    else others.push(...shown.map((item) => ({ ...item, name: project.slug + "/" + item.name,
      label: item.label ? project.slug + " · " + item.label : "" })));
  }
  if (libraryLoaded) {
    // A file deleted since the selection was saved cannot be sent.
    for (const path of [...selectedRefs.keys()]) if (!known.has(path)) selectedRefs.delete(path);
  }
  setText(document.querySelector("#reference-browser > summary"), "Browse images (" + (mine.length + others.length) + ")");
  const toggle = document.getElementById("reference-earlier");
  toggle.hidden = !earlier;
  toggle.textContent = showEarlierRefs ? "Hide earlier versions" : "Show " + earlier + " earlier " + (earlier === 1 ? "version" : "versions");
  fillReferenceGrid(current, mine, slug ? "No images in this production yet." : "No production is on the desk yet.");
  fillReferenceGrid(document.getElementById("reference-other"), others, "No other images in the library.");
  fillReferenceGrid(
    document.getElementById("reference-uploads"),
    uploadedRefs.map((item) => ({ path: item.id, name: item.name, src: item.src })),
    ""
  );
  const uploads = document.getElementById("reference-uploads");
  if (!uploadedRefs.length) uploads.replaceChildren();
  // New labels reach the chosen cards too, unless someone is typing in one.
  if (!document.getElementById("reference-selected").contains(document.activeElement)) referenceTraySignature = "";
  markReferenceSelection();
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const text = String(reader.result || "");
      const comma = text.indexOf(",");
      resolve(comma >= 0 ? text.slice(comma + 1) : text);
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

async function uploadReference(file) {
  const ext = referenceExtension(file.name);
  if (!REFERENCE_EXT.has(ext)) {
    toast("Upload a PNG, JPEG, or WebP.", 4000);
    return;
  }
  if (!file.size || file.size > MAX_REFERENCE_BYTES) {
    toast("That image is too large.", 4000);
    return;
  }
  const encoded = await fileToBase64(file);
  const res = await fetch("/api/references", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename: file.name.split(/[/\\]/).pop(), data: encoded })
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.id) {
    toast(data.error || "Could not save that image.", 4000);
    return;
  }
  uploadedRefs.push({ id: data.id, name: file.name.split(/[/\\]/).pop(), src: data.src });
  selectedRefs.set(data.id, newReference());
  referenceKey = "";
  renderReferences();
}

function gateChoice() {
  const picked = document.querySelector('input[name="gate"]:checked');
  return picked ? picked.value : "approve";
}

function briefSummary() {
  const out = document.getElementById("brief-summary");
  if (!out) return;
  const gate = { approve: "Waits for your approval", auto: "Continues to images automatically", pages: "Stops after the script" }[gateChoice()];
  const parts = [gate];
  if (gateChoice() !== "pages") {
    parts.push(document.getElementById("dry").checked ? "Writes image prompts, no image credits" : (document.getElementById("inspection-mode").value === "auto" ? "Auto inspection" : "Manual inspection"));
  }
  const shots = document.getElementById("max-shots").value;
  const images = document.getElementById("image-count").value;
  const runtime = document.getElementById("runtime").value;
  const aspect = document.getElementById("aspect").value;
  if (runtime) parts.push(runtime + "s");
  if (aspect) parts.push(aspect);
  if (shots) parts.push("Up to " + shots + " shots");
  if (images) parts.push("Up to " + images + " images");
  if (selectedRefs.size) parts.push("References: " + referenceSummary().replace(" · ", ", "));
  if (last && last.folder && !last.running) parts.push("Starts a new production; saved files stay");
  out.textContent = parts.join(" · ");
  briefEstimate();
}

// Shot counts follow budget.py. The longest clip is the selected video model's limit.
function clipBounds() {
  const models = settingsSchema && settingsSchema.video ? settingsSchema.video.models : [];
  const id = studioSettings && studioSettings.video ? studioSettings.video.selection : "auto";
  const model = models.find((item) => item.id === id);
  const kling = models.find((item) => item.id === "kling");
  if (!model || !Number.isInteger(model.duration_min) || !Number.isInteger(model.duration_max)) {
    return { id: "auto", min: 3, max: 30, note: " A clip longer than 15 seconds uses Seedance 2.5." };
  }
  const note = id === "auto" && kling ? " A clip longer than " + kling.duration_max + " seconds uses Seedance 2.5." : "";
  return { id, min: model.duration_min, max: model.duration_max, note };
}

function briefEstimate() {
  const note = document.getElementById("format-estimate");
  const run = document.getElementById("run");
  const runtime = Number(document.getElementById("runtime").value);
  const maxShots = Number(document.getElementById("max-shots").value);
  const bounds = clipBounds();
  let problem = "";
  if (runtime && (!Number.isInteger(runtime) || runtime < 6 || runtime > 720)) problem = "Length must be a whole number between 6 and 720 seconds.";
  else if (maxShots && (!Number.isInteger(maxShots) || maxShots < 1 || maxShots > 240)) problem = "Maximum shots must be between 1 and 240.";
  const low = runtime ? Math.max(1, Math.ceil(runtime / bounds.max)) : 0;
  const high = runtime ? Math.min(240, Math.max(1, Math.floor(runtime / bounds.min))) : 0;
  if (!problem && runtime && maxShots && (maxShots < low || maxShots > high)) problem = "For " + runtime + " seconds, maximum shots must be between " + low + " and " + high + ".";
  note.dataset.bad = problem ? "1" : "";
  run.disabled = Boolean(problem) || Boolean(last && last.running);
  const range = bounds.min + "–" + bounds.max;
  if (problem) { setText(note, problem); return; }
  if (!runtime) { setText(note, "Leave these empty and the idea decides. Shots last " + range + " seconds and together fill the length exactly." + bounds.note); return; }
  const shots = maxShots || Math.max(low, Math.floor(runtime / 8));
  const shotWord = shots === 1 ? "shot" : "shots";
  const imageWord = shots === 1 ? "image" : "images";
  const clipWord = shots === 1 ? "clip" : "clips";
  setText(note, runtime + "s film · up to " + shots + " " + shotWord + " → " + shots + " shot " + imageWord + " and up to " + shots + " video " + clipWord + ", plus character and location images. Each clip is " + range + " seconds." + bounds.note);
  ideaCost(runtime, shots);
}

// A rough figure from the saved model choices. The exact estimate appears beside Approve pages.
function ideaCost(runtime, shots) {
  const out = document.getElementById("idea-cost");
  const pricing = settingsSchema && settingsSchema.pricing;
  if (!out) return;
  out.hidden = !pricing || !studioSettings;
  if (out.hidden) return;
  const video = studioSettings.video;
  const key = video.mode + "/" + (video.sound ? "sound" : "silent");
  const names = video.selection === "auto" ? ["kling", "seedance"] : [video.selection];
  const rates = names.map((name) => [name, (pricing.video[name] || {})[key]]).filter(([, rate]) => typeof rate === "number");
  const resolution = studioSettings.images.resolution === "auto" ? "1K" : studioSettings.images.resolution;
  const perImage = imagePrice(studioSettings.images.cinematic, resolution);
  const perSecond = rates.map(([name, rate]) => pricing.video_names[name] + " " + rate).join(", ");
  out.title = "KIE prices from " + pricing.source + ", checked " + pricing.checked + ". " + pricing.extras + " " +
    Object.values(pricing.assumptions || {}).join(" ");
  if (!runtime) {
    setText(out, "Cost guide · video " + perSecond + " credits per second · images about " + (perImage || "?") + " credits each at " + resolution);
    return;
  }
  const low = Math.min(...rates.map(([, rate]) => rate)) * runtime;
  const high = Math.max(...rates.map(([, rate]) => rate)) * runtime;
  const dollars = (amount) => "$" + (amount * pricing.usd_per_credit).toFixed(2);
  const videoText = low === high ? credits(low)
    : Math.round(low).toLocaleString() + "–" + Math.round(high).toLocaleString() + " credits (" + dollars(low) + "–" + dollars(high) + "; per second: " + perSecond + ")";
  setText(out, "Rough cost · video " + videoText + " · shot images about " + credits(shots * (perImage || 0)) +
    ", plus character and location images");
}

async function startRun() {
  const run = document.getElementById("run");
  if (run.disabled) return;
  if (!document.getElementById("idea").value.trim()) {
    toast("Write an idea first, or use Suggest a story.", 4000);
    document.getElementById("idea").focus();
    return;
  }
  let references;
  try { references = referencePayload(); }
  catch (error) { toast(error.message, 6000); return; }
  releaseNote();
  run.disabled = true;
  follow = true;
  document.getElementById("follow").classList.add("is-on");
  setView("pipeline");
  try {
    const res = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        idea: document.getElementById("idea").value,
        pages_only: gateChoice() === "pages",
        approval: gateChoice() === "approve",
        dry_run: document.getElementById("dry").checked,
        inspection_mode: document.getElementById("inspection-mode").value,
        max_shots: document.getElementById("max-shots").value ? Number(document.getElementById("max-shots").value) : null,
        runtime_seconds: document.getElementById("runtime").value ? Number(document.getElementById("runtime").value) : null,
        aspect_ratio: document.getElementById("aspect").value || null,
        image_count: document.getElementById("image-count").value === "" ? null : Number(document.getElementById("image-count").value),
        references
      })
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      const note = document.getElementById("note");
      note.textContent = data.error || "Could not start the crew.";
      note.classList.add("is-bad");
      run.disabled = false;
    }
  } catch (error) {
    const note = document.getElementById("note");
    note.textContent = "Could not start the crew.";
    note.classList.add("is-bad");
    run.disabled = false;
  }
}

let librarySig = null;
async function pollLibrary() {
  try {
    const res = await fetch("/api/library");
    const data = await res.json();
    if (data.signature === librarySig) return;
    librarySig = data.signature;
    libraryProjects = data.projects || [];
    libraryLoaded = true;
    renderReferences();
    const nav = document.getElementById("library");
    nav.replaceChildren();
    if (!data.projects || !data.projects.length) {
      if (localFiles.size) { filterLibrary(); return; }
      const p = document.createElement("p");
      p.className = "empty";
      p.textContent = "No productions yet.";
      nav.appendChild(p);
      filterLibrary();
      return;
    }
    for (const project of data.projects) {
      const { section, head, content } = libraryFolder(project, "production");
      const open = make("button", "linkish open-production", "Open");
      open.type = "button";
      open.title = "Continue " + project.slug + " in the desk";
      open.setAttribute("aria-label", "Open " + project.slug + " in the desk");
      open.addEventListener("click", () => openProduction(project.slug));
      head.appendChild(open);
      for (const [label, items] of [["Documents", project.documents], ["Images", project.images], ["Videos", project.videos || []]]) {
        const groupContent = document.createElement("div");
        groupContent.className = "library-group";
        content.appendChild(groupContent);
        const name = document.createElement("p");
        name.className = "label";
        name.textContent = label;
        groupContent.appendChild(name);
        if (!items.length) {
          const empty = document.createElement("p");
          empty.className = "empty";
          empty.textContent = "None yet.";
          groupContent.appendChild(empty);
          continue;
        }
        for (const item of items) {
          groupContent.appendChild(fileButton(item));
        }
      }
      nav.appendChild(section);
    }
    filterLibrary();
  } catch (error) {
    /* the board still shows the log */
  }
}

function escapeText(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function escapeAttr(text) {
  return escapeText(text).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function safeUrl(url) {
  const value = String(url || "").trim();
  if (!value || /[\u0000-\u001F\u007F]/.test(value) || value.startsWith("//")) return "";
  if (/^[a-z][a-z0-9+.-]*:/i.test(value) && !/^(https?:|mailto:)/i.test(value)) return "";
  return value;
}

function readerFormatFor(name) {
  const base = String(name || "").split(/[/\\]/).pop();
  const dot = base.lastIndexOf(".");
  const suffix = dot > 0 ? base.slice(dot + 1).toLowerCase() : "";
  if (suffix === "md" || suffix === "markdown") return "md";
  if (suffix === "json") return "json";
  return "";
}

function renderInline(raw) {
  const codes = [];
  let work = String(raw).replace(/`([^`\n]+)`/g, (_, code) => {
    codes.push("<code>" + escapeText(code) + "</code>");
    return "\u0000C" + (codes.length - 1) + "\u0000";
  });
  const links = [];
  work = work.replace(/(^|[^!])\[([^\]\n]+)\]\(([^)\s]+)\)/g, (_, lead, label, url) => {
    links.push({ label, url });
    return lead + "\u0000L" + (links.length - 1) + "\u0000";
  });
  work = work.split(/(\u0000[CL]\d+\u0000)/).map((part) => {
    if (/^\u0000[CL]\d+\u0000$/.test(part)) return part;
    return escapeText(part);
  }).join("");
  work = work.replace(/\*\*\*([^*\n]+)\*\*\*/g, "<strong><em>$1</em></strong>");
  work = work.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  work = work.replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
  work = work.replace(/(^|[^*])\*([^*\n]+)\*/g, (_, lead, body) => lead + "<em>" + body + "</em>");
  work = work.replace(/(^|[^_\w])_([^_\n]+)_(?![_\w])/g, (_, lead, body) => lead + "<em>" + body + "</em>");
  work = work.replace(/\u0000L(\d+)\u0000/g, (_, id) => {
    const link = links[Number(id)];
    const href = safeUrl(link.url);
    if (!href) return escapeText("[" + link.label + "](" + link.url + ")");
    return '<a href="' + escapeAttr(href) + '" rel="noopener noreferrer">' + renderInline(link.label) + "</a>";
  });
  work = work.replace(/\u0000C(\d+)\u0000/g, (_, id) => codes[Number(id)]);
  return work;
}

function endsWithDelimiter(text) {
  if (!text.endsWith("|")) return false;
  let slashes = 0;
  for (let i = text.length - 2; i >= 0 && text[i] === "\\"; i -= 1) slashes += 1;
  return slashes % 2 === 0;
}

function splitTableCells(line) {
  let text = String(line).trim();
  if (text.startsWith("|")) text = text.slice(1);
  if (endsWithDelimiter(text)) text = text.slice(0, -1);
  const cells = [];
  let current = "";
  for (let i = 0; i < text.length; i += 1) {
    if (text[i] === "\\" && text[i + 1] === "|") {
      current += "|";
      i += 1;
      continue;
    }
    if (text[i] === "|") {
      cells.push(current.trim());
      current = "";
      continue;
    }
    current += text[i];
  }
  cells.push(current.trim());
  return cells;
}

function tableAlignments(line) {
  if (!String(line).includes("|")) return null;
  const cells = splitTableCells(line);
  if (!cells.length || cells.some((cell) => !/^:?-+:?$/.test(cell))) return null;
  return cells.map((cell) => {
    const left = cell.startsWith(":");
    const right = cell.endsWith(":");
    if (left && right) return "center";
    if (right) return "right";
    return "left";
  });
}

function looksLikeTableRow(line) {
  const text = String(line).trim();
  if (!text.includes("|")) return false;
  if (/^#{1,6}(?:[ \t]|$)/.test(text)) return false;
  if (/^[-*+][ \t]+/.test(text)) return false;
  if (/^\d+[.)][ \t]+/.test(text)) return false;
  if (/^\u0000F\d+\u0000$/.test(text)) return false;
  return true;
}

function renderTable(header, aligns, body) {
  const cols = Math.max(header.length, aligns.length, body.reduce((n, row) => Math.max(n, row.length), 0));
  const alignFor = (index) => (aligns[index] === "center" || aligns[index] === "right" ? aligns[index] : "left");
  let html = '<div class="md-table-wrap"><table><thead><tr>';
  for (let i = 0; i < cols; i += 1) {
    html += '<th style="text-align:' + alignFor(i) + '">' + renderInline(header[i] || "") + "</th>";
  }
  html += "</tr></thead><tbody>";
  for (const row of body) {
    html += "<tr>";
    for (let i = 0; i < cols; i += 1) {
      html += '<td style="text-align:' + alignFor(i) + '">' + renderInline(row[i] || "") + "</td>";
    }
    html += "</tr>";
  }
  html += "</tbody></table></div>";
  return html;
}

function renderMarkdown(source) {
  const text = String(source || "").replace(/\r\n?/g, "\n");
  const fences = [];
  const stripped = text.replace(/^[ ]{0,3}```[^\n]*\n([\s\S]*?)^[ ]{0,3}```[ \t]*$/gm, (_, code) => {
    fences.push('<pre class="md-code"><code>' + escapeText(code.replace(/\n$/, "")) + "</code></pre>");
    return "\u0000F" + (fences.length - 1) + "\u0000";
  });
  const lines = stripped.split("\n");
  let html = "";
  let list = "";
  const para = [];
  function flushPara() {
    if (!para.length) return;
    html += "<p>" + renderInline(para.join(" ")) + "</p>";
    para.length = 0;
  }
  function closeList() {
    if (!list) return;
    html += "</" + list + ">";
    list = "";
  }
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const fence = /^\u0000F(\d+)\u0000$/.exec(line);
    if (fence) {
      flushPara();
      closeList();
      html += fences[Number(fence[1])];
      continue;
    }
    const heading = /^(#{1,6})[ \t]+(.*)$/.exec(line);
    if (heading) {
      flushPara();
      closeList();
      const level = heading[1].length;
      html += "<h" + level + ">" + renderInline(heading[2].replace(/[ \t]+#+\s*$/, "")) + "</h" + level + ">";
      continue;
    }
    if (looksLikeTableRow(line) && i + 1 < lines.length) {
      const aligns = tableAlignments(lines[i + 1]);
      if (aligns) {
        flushPara();
        closeList();
        const header = splitTableCells(line);
        const body = [];
        i += 2;
        while (i < lines.length && looksLikeTableRow(lines[i]) && !tableAlignments(lines[i])) {
          body.push(splitTableCells(lines[i]));
          i += 1;
        }
        i -= 1;
        html += renderTable(header, aligns, body);
        continue;
      }
    }
    const bullet = /^[ \t]*[-*][ \t]+(.*)$/.exec(line);
    const ordered = /^[ \t]*\d+[.)][ \t]+(.*)$/.exec(line);
    if (bullet || ordered) {
      flushPara();
      const kind = bullet ? "ul" : "ol";
      if (list !== kind) {
        closeList();
        html += "<" + kind + ">";
        list = kind;
      }
      html += "<li>" + renderInline((bullet || ordered)[1]) + "</li>";
      continue;
    }
    if (!line.trim()) {
      flushPara();
      closeList();
      continue;
    }
    closeList();
    para.push(line.trim());
  }
  flushPara();
  closeList();
  return html;
}

function highlightJson(pretty) {
  return escapeText(pretty).replace(
    /("(?:\\.|[^"\\])*")(\s*:)|("(?:\\.|[^"\\])*")|\b(true|false)\b|\bnull\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g,
    (match, key, colon, str, bool) => {
      if (key) return '<span class="json-key">' + key + "</span>" + colon;
      if (str) return '<span class="json-string">' + str + "</span>";
      if (bool) return '<span class="json-bool">' + bool + "</span>";
      if (match === "null") return '<span class="json-null">null</span>';
      return '<span class="json-number">' + match + "</span>";
    }
  );
}

function renderJsonPreview(source) {
  try {
    return '<pre class="json-view">' + highlightJson(JSON.stringify(JSON.parse(source), null, 2)) + "</pre>";
  } catch (error) {
    return '<p class="parse-note">This file could not be parsed as JSON.</p><pre class="json-view">' + escapeText(source) + "</pre>";
  }
}

function syncPreviewButton() {
  const button = document.getElementById("preview-reader");
  if (!button) return;
  const available = readerFormat === "md" || readerFormat === "json";
  button.hidden = !available;
  button.disabled = !available;
  const showing = available && readerMode === "preview";
  button.textContent = showing ? "Source" : "Preview";
  button.classList.toggle("is-on", showing);
  button.setAttribute("aria-pressed", showing ? "true" : "false");
}

function paintReader() {
  const pre = document.getElementById("reader-text");
  const preview = document.getElementById("reader-preview");
  pre.textContent = readerSource;
  if ((readerFormat === "md" || readerFormat === "json") && readerMode === "preview") {
    pre.hidden = true;
    preview.hidden = false;
    preview.innerHTML = readerFormat === "json" ? renderJsonPreview(readerSource) : renderMarkdown(readerSource);
  } else {
    preview.hidden = true;
    preview.replaceChildren();
    pre.hidden = false;
  }
  syncPreviewButton();
}

function releaseReaderImage() {
  const video = document.getElementById("reader-video");
  if (video) {
    video.pause();
    video.removeAttribute("src");
    video.hidden = true;
  }
  const img = document.getElementById("reader-image");
  const src = img.getAttribute("src") || "";
  if (src.startsWith("blob:")) URL.revokeObjectURL(src);
  img.removeAttribute("src");
  img.alt = "";
  img.hidden = true;
}

function syncReaderEmpty() {
  const empty = document.getElementById("reader-empty");
  const showLibrary = document.getElementById("reader-show-library");
  if (empty) empty.hidden = readerOpen;
  if (showLibrary) showLibrary.hidden = readerOpen || !document.body.classList.contains("library-hidden");
}

function setLibraryHidden(hidden) {
  document.body.classList.toggle("library-hidden", hidden);
  const toggle = document.getElementById("toggle-library");
  if (toggle) toggle.textContent = hidden ? "Show library" : "Hide library";
  syncReaderEmpty();
}

function toggleLibrary() {
  setLibraryHidden(!document.body.classList.contains("library-hidden"));
}

function showReaderDocument(name, text) {
  readerOpen = true;
  readerSource = String(text == null ? "" : text);
  readerFormat = readerFormatFor(name);
  readerMode = readerFormat ? "preview" : "source";
  document.getElementById("reader-title").textContent = name || "Viewer";
  releaseReaderImage();
  paintReader();
  syncReaderEmpty();
}

function showReaderImage(name, src) {
  releaseReaderImage();
  readerOpen = true;
  readerSource = "";
  readerFormat = "";
  readerMode = "source";
  document.getElementById("reader-title").textContent = name || "Image";
  const pre = document.getElementById("reader-text");
  pre.hidden = true;
  pre.textContent = "";
  const preview = document.getElementById("reader-preview");
  preview.hidden = true;
  preview.replaceChildren();
  syncPreviewButton();
  const img = document.getElementById("reader-image");
  const previous = img.getAttribute("src") || "";
  if (previous.startsWith("blob:") && previous !== src) URL.revokeObjectURL(previous);
  img.hidden = false;
  img.alt = name || "Image";
  img.src = src;
  syncReaderEmpty();
}

function resetReader() {
  readerOpen = false;
  readerSource = "";
  readerFormat = "";
  readerMode = "source";
  const pre = document.getElementById("reader-text");
  pre.hidden = true;
  pre.textContent = "";
  const preview = document.getElementById("reader-preview");
  preview.hidden = true;
  preview.replaceChildren();
  releaseReaderImage();
  document.getElementById("reader-title").textContent = "Viewer";
  syncPreviewButton();
  syncReaderEmpty();
}

function toggleReaderPreview() {
  if (readerFormat !== "md" && readerFormat !== "json") return;
  readerMode = readerMode === "preview" ? "source" : "preview";
  paintReader();
}

async function openLocalFile(path) {
  const file = localFiles.get(path);
  if (!file) return;
  const suffix = file.name.includes(".") ? file.name.split(".").pop().toLowerCase() : "";
  if (LOCAL_IMAGES.has(suffix)) {
    showReaderImage(file.name, URL.createObjectURL(file));
    return;
  }
  showReaderDocument(file.name, await file.text());
}

async function openFile(path) {
  setView("reader", true);
  if (/^[^/]+\/videos\/(final|s\d+)\.mp4$/.test(path)) {
    resetReader();
    readerOpen = true;
    document.getElementById("reader-title").textContent = path;
    const video = document.getElementById("reader-video");
    video.src = "/video-media?path=" + encodeURIComponent(path);
    video.hidden = false;
    syncReaderEmpty();
    return;
  }
  if (String(path).startsWith("local:")) {
    await openLocalFile(path);
    return;
  }
  try {
    const res = await fetch("/api/file?path=" + encodeURIComponent(path));
    const data = await res.json();
    if (data.kind === "image" && data.src) {
      showReaderImage(data.name || "Image", data.src);
      return;
    }
    showReaderDocument(data.name || "Viewer", data.text || data.error || "That file is not in a production folder.");
  } catch (error) {
    showReaderDocument("Viewer", "That file is not in a production folder.");
  }
}

document.getElementById("brief-form").addEventListener("submit", (event) => {
  event.preventDefault();
  startRun();
});
document.getElementById("idea").addEventListener("input", suggestLabel);
document.getElementById("idea").addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    startRun();
  }
});
document.getElementById("brief-form").addEventListener("change", briefSummary);
// Plan images only can sit beside Approve pages, outside the form; the cost line follows it.
document.getElementById("dry").addEventListener("change", () => { if (last) syncProceed(last); });
document.getElementById("brief-form").addEventListener("input", briefSummary);
document.getElementById("track").addEventListener("click", (event) => {
  const station = event.target.closest(".station");
  if (station) pickStation(station.dataset.station);
});
document.getElementById("detail-follow").addEventListener("click", () => pickStation(""));
for (const name of ["idea", "film", "gate", "images", "inspection", "models", "references", "video", "writing", "dry"]) {
  document.getElementById("help-" + name).addEventListener("click", () => {
    const copy = document.getElementById("help-" + name + "-copy");
    const button = document.getElementById("help-" + name);
    copy.hidden = !copy.hidden;
    button.setAttribute("aria-expanded", copy.hidden ? "false" : "true");
  });
}
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => setView(tab.dataset.tab, true));
});
document.getElementById("follow").addEventListener("click", () => {
  follow = !follow;
  document.getElementById("follow").classList.toggle("is-on", follow);
  if (follow && last) setView(viewFor(last));
  toast(follow ? "Following the crew" : "Tabs stay where you put them");
});
document.getElementById("toggle-library").addEventListener("click", toggleLibrary);
document.getElementById("suggest").addEventListener("click", suggestBrief);
document.getElementById("clear-brief").addEventListener("click", () => {
  document.getElementById("idea").value = "";
  document.getElementById("idea").focus();
  suggestLabel();
});
document.getElementById("copy-brief").addEventListener("click", () => {
  copyText(document.getElementById("idea").value, "Brief copied");
});
document.getElementById("copy-log").addEventListener("click", () => {
  const lines = [...document.querySelectorAll("#log p")].map((p) => p.textContent).join("\n");
  copyText(lines, "Log copied");
});
document.getElementById("log-end").addEventListener("click", () => {
  const box = document.getElementById("log");
  box.scrollTop = box.scrollHeight;
});
document.getElementById("preview-reader").addEventListener("click", toggleReaderPreview);
document.getElementById("copy-reader").addEventListener("click", () => {
  copyText(readerSource, "Text copied");
});
document.getElementById("still-tools").addEventListener("click", (event) => {
  const chip = event.target.closest("[data-filter]");
  if (!chip) return;
  stillFilter = chip.dataset.filter;
  document.querySelectorAll("#still-tools .chip").forEach((button) => {
    button.classList.toggle("is-on", button === chip);
  });
  applyStillFilter();
});
bindLibrarySearch();
document.querySelector("aside").addEventListener("click", (event) => {
  if (event.target.closest("[data-close-folder]")) {
    localFiles.clear();
    document.getElementById("opened-folder").replaceChildren();
    if (String(openPath || "").startsWith("local:")) openPath = null;
    const nav = document.getElementById("library");
    if (!nav.querySelector(".file, .empty")) {
      const p = document.createElement("p");
      p.className = "empty";
      p.textContent = "No productions yet.";
      nav.appendChild(p);
    }
    filterLibrary();
    return;
  }
  const button = event.target.closest("button[data-path]");
  if (!button) return;
  openPath = button.dataset.path;
  document.querySelectorAll("aside button.file.is-open").forEach((el) => el.classList.remove("is-open"));
  button.classList.add("is-open");
  openFile(button.dataset.path);
});
document.getElementById("open-folder").addEventListener("click", () => {
  document.getElementById("folder-picker").click();
});
document.getElementById("folder-picker").addEventListener("change", (event) => {
  const files = event.target.files;
  if (files && files.length) takeFolder(files);
  event.target.value = "";
});
document.getElementById("reference-restore").addEventListener("click", restoreReferences);
document.getElementById("reference-earlier").addEventListener("click", () => {
  showEarlierRefs = !showEarlierRefs;
  renderReferences();
});
document.getElementById("reference-clear").addEventListener("click", () => {
  if (!selectedRefs.size) return;
  selectedRefs.clear();
  markReferenceSelection();
});
document.getElementById("reference-upload").addEventListener("change", async (event) => {
  const files = [...(event.target.files || [])];
  event.target.value = "";
  for (const file of files) {
    try {
      await uploadReference(file);
    } catch (error) {
      toast("Could not save that image.", 4000);
    }
  }
});
document.getElementById("clear-log").addEventListener("click", async () => {
  try {
    const res = await fetch("/api/log/clear", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    if (!res.ok) throw new Error("clear");
    toast("Log cleared");
  } catch (error) {
    toast("Could not clear the activity");
  }
});
document.getElementById("new-session").addEventListener("click", async () => {
  const question = last && last.running
    ? "A run is in progress. Starting a new production stops it now (tasks already sent to KIE are still billed). Saved files stay on disk. Continue?"
    : "Start a new production from a blank idea? The current production's files stay on disk and can be opened again from the library.";
  if (!window.confirm(question)) return;
  try {
    const res = await fetch("/api/session/clear", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    if (!res.ok) throw new Error("clear");
    document.getElementById("idea").value = "";
    suggestLabel();
    releaseNote();
    document.querySelector('input[name="gate"][value="approve"]').checked = true;
    document.getElementById("dry").checked = false;
    pickedStation = "";
    document.getElementById("max-shots").value = "";
    document.getElementById("image-count").value = "";
    document.getElementById("runtime").value = "";
    document.getElementById("aspect").value = "";
    document.getElementById("inspection-mode").value = "manual";
    selectedRefs.clear();
    uploadedRefs = [];
    referenceKey = "";
    renderReferences();
    briefSummary();
    resetReader();
    openPath = null;
    setView("brief");
    toast("New production");
  } catch (error) {
    toast("Could not start a new session");
  }
});
document.addEventListener("keydown", (event) => {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement && document.activeElement.tagName);
  if (typing) return;
  const views = ["brief", "pipeline", "stills", "video", "activity", "reader"];
  const index = "123456".indexOf(event.key);
  if (index >= 0) setView(views[index], true);
});
document.getElementById("stage").addEventListener("animationend", (event) => {
  if (event.animationName === "shake") event.currentTarget.classList.remove("shake");
});
window.addEventListener("resize", () => {
  moveGlider();
  if (!last) return;
  placeRail();
  moveToken(last.station || "brief", false);
});

loadReferences();
suggestLabel();
renderReferences();
briefSummary();
syncReaderEmpty();
startDust();
pollLibrary();
setInterval(pollLibrary, 3000);
moveGlider();
fetch("/api/status").then((res) => res.json()).then(apply).catch(() => {
  document.getElementById("note").textContent = "Could not read the desk.";
});
const events = new EventSource("/api/events");
events.onmessage = (event) => {
  streamOK = true;
  apply(JSON.parse(event.data));
};
events.onerror = () => { streamOK = false; };
setInterval(() => {
  if (streamOK) return;
  fetch("/api/status").then((res) => res.json()).then(apply).catch(() => {});
}, 1000);

document.getElementById("stop").addEventListener("click", async () => {
  const button = document.getElementById("stop");
  button.disabled = true;
  try {
    const res = await fetch("/api/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    });
    if (!res.ok) throw new Error("stop");
  } catch (error) {
    button.disabled = false;
    toast("Could not stop generation");
  }
});
document.getElementById("review-stills").addEventListener("click", openStillsReview);
document.getElementById("show-review-still").addEventListener("click", openStillsReview);
document.getElementById("reader-show-library").addEventListener("click", toggleLibrary);
document.getElementById("resume-images").addEventListener("click", () => reviewImage({}));
document.getElementById("proceed").addEventListener("click", proceedDraft);
document.getElementById("abort-draft").addEventListener("click", abortDraft);

async function proceedDraft() {
  const button = document.getElementById("proceed");
  button.disabled = true;
  follow = true;
  document.getElementById("follow").classList.add("is-on");
  setView("pipeline");
  try {
    const res = await fetch("/api/proceed", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        dry_run: document.getElementById("dry").checked,
        inspection_mode: document.getElementById("inspection-mode").value
      })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(data.error || "Could not continue this draft.");
      button.disabled = false;
      return;
    }
    toast("Continuing with the saved draft");
  } catch (error) {
    toast("Could not continue this draft.");
    button.disabled = false;
  }
}

async function abortDraft() {
  if (!window.confirm("Abort this draft? It stops here for good: no image prompts, images or clips will be made from it. Its files stay on disk.")) return;
  const button = document.getElementById("abort-draft");
  const proceed = document.getElementById("proceed");
  button.disabled = true;
  proceed.disabled = true;
  try {
    const res = await fetch("/api/abort-draft", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      toast(data.error || "Could not abort this draft.");
      button.disabled = false;
      proceed.disabled = false;
    }
  } catch (error) {
    toast("Could not abort this draft.");
    button.disabled = false;
    proceed.disabled = false;
  }
}


// ---- Idea tab: image, video, and writing models ------------------------------------

let settingsLabelSequence = 0;
function settingsRow(label, help, control) {
  const row = make("div", "settings-row");
  const text = make("label", "settings-label");
  text.id = "settings-label-" + (++settingsLabelSequence);
  const fields = control.matches && control.matches("select, input") ? [control] : [...control.querySelectorAll("select, input")];
  for (const field of fields) if (!field.getAttribute("aria-label")) field.setAttribute("aria-labelledby", text.id);
  text.append(make("span", "settings-name", label));
  if (help) text.append(make("small", "", help));
  row.append(text, control);
  return row;
}

function settingsSelect(options, value, onChange) {
  const select = document.createElement("select");
  select.className = "limit";
  for (const option of options) {
    const node = document.createElement("option");
    node.value = option.id;
    node.textContent = option.name;
    select.appendChild(node);
  }
  select.value = value;
  select.disabled = options.length < 2;
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

function modelNote(text, sources) {
  const note = make("p", "field-note", text);
  for (const [index, source] of (sources || []).entries()) {
    let host = "docs";
    try { host = new URL(source).hostname.replace(/^(www|docs)\./, ""); } catch (_) { /* The link text falls back to "docs". */ }
    const link = make("a", "", " · " + host + (sources.length > 1 ? "" : ""));
    link.title = "Source " + (index + 1);
    link.href = safeUrl(source);
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    note.append(link);
  }
  return note;
}

function rolesIn(group) {
  return settingsSchema.chat.roles.filter((role) => (role.group || "story") === group);
}

function fillRoles(host, roles, values, save) {
  for (const role of roles) {
    const controls = make("div", "settings-pair");
    controls.append(settingsSelect(settingsSchema.chat.models, values.chat[role.id], (value) => {
      values.chat[role.id] = value;
      save();
    }));
    if (settingsSchema.chat.efforts && values.effort) {
      const effort = settingsSelect(settingsSchema.chat.efforts, values.effort[role.id] || "default", (value) => {
        values.effort[role.id] = value;
        save();
      });
      effort.classList.add("effort-select");
      effort.title = "Reasoning effort. Higher thinks longer and costs more.";
      effort.setAttribute("aria-label", role.label + " reasoning effort");
      controls.append(effort);
    }
    host.appendChild(settingsRow(role.label, role.help, controls));
    const chosen = settingsSchema.chat.models.find((model) => model.id === values.chat[role.id]);
    if (chosen) host.appendChild(modelNote(chosen.note, chosen.sources));
  }
}

// The closed Models and quality panel shows the choices that shape cost and look.
function modelsSummary(values) {
  const name = (options, id) => ((options || []).find((option) => option.id === id) || {}).name || id;
  const cinematic = settingsSchema.images.groups.find((group) => group.id === "cinematic") || settingsSchema.images.groups[0];
  const video = values.video.selection === "auto" ? "Auto video" : name(settingsSchema.video.models, values.video.selection);
  const quality = String(settingsSchema.video.modes[values.video.mode] || values.video.mode).split(" · ").pop();
  const writers = [...new Set(rolesIn("story").map((role) => name(settingsSchema.chat.models, values.chat[role.id])))];
  const parts = [
    name(cinematic.options, values.images[cinematic.id]) + " " + (values.images.resolution === "auto" ? "(auto size)" : values.images.resolution),
    video + " " + quality + ", " + (values.video.sound ? "with sound" : "no sound"),
    writers.join(" / ")
  ];
  const custom = Object.values(values.effort || {}).filter((effort) => effort !== "default").length;
  if (custom) parts.push(custom + " custom reasoning " + (custom === 1 ? "setting" : "settings"));
  return parts.join(" · ");
}

let effortShown = null;
function syncEffort(values) {
  const toggle = document.getElementById("show-effort");
  // Shown from the start when a role already has a changed effort; the summary line names custom efforts either way.
  if (effortShown === null) effortShown = Object.values(values.effort || {}).some((effort) => effort !== "default");
  toggle.checked = effortShown;
  document.getElementById("models-panel").classList.toggle("show-effort", effortShown);
}

function renderSettings() {
  const images = document.getElementById("settings-images");
  const video = document.getElementById("settings-video");
  const writing = document.getElementById("settings-writing");
  if (!studioSettings || !settingsSchema || !images || !video || !writing) return;
  const values = studioSettings;
  const save = () => saveSettings();
  images.replaceChildren();
  video.replaceChildren();
  writing.replaceChildren();

  for (const item of settingsSchema.images.groups) {
    images.appendChild(settingsRow(item.label, item.help, settingsSelect(item.options, values.images[item.id], (value) => { values.images[item.id] = value; save(); })));
    const chosen = item.options.find((option) => option.id === values.images[item.id]) || item.options[0];
    const detail = chosen.resolutions.length ? chosen.resolutions.join(" / ") : "fixed quality";
    images.appendChild(modelNote(chosen.note + " " + (chosen.max_references ? "Up to " + chosen.max_references + " reference images" : "No reference images") + " · resolution " + detail + ".", chosen.sources));
  }
  const resolutions = settingsSchema.images.resolutions.map((id) => ({ id, name: id === "auto" ? "Auto · the image prompts decide (1K)" : id }));
  images.appendChild(settingsRow("Image resolution", "Higher resolution costs more. A model that cannot do a size falls back to Nano Banana.", settingsSelect(resolutions, values.images.resolution, (value) => { values.images.resolution = value; save(); })));
  const imageParallel = (settingsSchema.images.parallel || [1, 2, 3, 4]).map((count) => ({ id: String(count), name: count + (count === 1 ? " image" : " images") + " at a time" }));
  images.appendChild(settingsRow("Images generated at once", "More is faster. A shot image still waits for the character and location images it uses.", settingsSelect(imageParallel, String(values.images.parallel || 3), (value) => { values.images.parallel = Number(value); save(); })));
  fillRoles(images, rolesIn("images"), values, save);

  const models = settingsSchema.video.models;
  const selected = models.find((model) => model.id === values.video.selection) || models[0];
  video.appendChild(settingsRow("Video model", "Auto chooses per shot from timing, speech, subjects and camera.", settingsSelect(models, values.video.selection, (value) => {
    values.video.selection = value;
    const allowed = models.find((model) => model.id === value).modes;
    if (!allowed.includes(values.video.mode)) values.video.mode = allowed.includes("pro") ? "pro" : allowed[0];
    save();
  })));
  if (selected.id !== "auto") video.appendChild(modelNote(selected.note, selected.sources));
  const modes = selected.modes.map((id) => ({ id, name: settingsSchema.video.modes[id] }));
  video.appendChild(settingsRow("Video quality", values.video.selection === "seedance" ? "Seedance offers up to 1080p on this endpoint." : "4K is only produced by Kling; Auto routes 4K shots to it.", settingsSelect(modes, values.video.mode, (value) => { values.video.mode = value; save(); })));
  const parallel = settingsSchema.video.parallel.map((count) => ({ id: String(count), name: count + (count === 1 ? " clip" : " clips") + " at a time" }));
  video.appendChild(settingsRow("Clips generated at once", "More is faster; each running clip is billed as usual.", settingsSelect(parallel, String(values.video.parallel), (value) => { values.video.parallel = Number(value); save(); })));
  const sound = document.createElement("input");
  sound.type = "checkbox";
  sound.checked = values.video.sound;
  sound.addEventListener("change", () => { values.video.sound = sound.checked; save(); });
  const soundLabel = make("label", "check settings-check");
  soundLabel.append(sound, document.createTextNode(" Generate dialogue and sound from the shot notes"));
  video.appendChild(soundLabel);
  fillRoles(video, rolesIn("video"), values, save);

  fillRoles(writing, rolesIn("story"), values, save);
  writing.appendChild(make("p", "field-note", "Astra is the strongest. Luna is the faster GPT-6. Sol is the lighter choice for the script editor."));
  setText(document.getElementById("models-summary"), modelsSummary(values));
  syncEffort(values);
  briefEstimate();
}

async function responseJson(response) {
  const text = await response.text();
  try {
    return text ? JSON.parse(text) : {};
  } catch (error) {
    throw new Error(response.status === 404
      ? "The desk needs a restart to load these settings. Stop it, run it again, and refresh this page."
      : "The desk returned a response that could not be read.");
  }
}

let settingsSaveTimer = 0;
async function saveSettings() {
  renderSettings();
  clearTimeout(settingsSaveTimer);
  settingsSaveTimer = setTimeout(async () => {
    try {
      const response = await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ values: studioSettings }) });
      const data = await responseJson(response);
      if (!response.ok) throw new Error(data.error || "Could not save settings.");
      studioSettings = data.values;
      renderSettings();
      toast("Settings saved");
      fetch("/api/status").then((res) => res.json()).then(apply).catch(() => {});
    } catch (error) {
      toast(error.message, 5000);
      loadSettings();
    }
  }, 250);
}

async function loadSettings(reset) {
  try {
    const response = reset
      ? await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reset: true }) })
      : await fetch("/api/settings");
    const data = await responseJson(response);
    if (!response.ok) throw new Error(data.error || "Could not load settings.");
    studioSettings = data.values;
    if (data.schema) settingsSchema = data.schema;
    if (!settingsSchema) settingsSchema = await fetch("/api/settings").then((res) => res.json()).then((body) => body.schema);
    const status = document.getElementById("settings-status");
    if (status) status.hidden = true;
    renderSettings();
    if (reset) toast("Settings reset to defaults");
  } catch (error) {
    const status = document.getElementById("settings-status");
    if (status) {
      status.hidden = false;
      setText(status, error.message);
    }
    setText(document.getElementById("models-summary"), "Model settings could not be loaded.");
  }
}
document.getElementById("settings-reset").addEventListener("click", () => loadSettings(true));
document.getElementById("show-effort").addEventListener("change", (event) => {
  effortShown = event.target.checked;
  document.getElementById("models-panel").classList.toggle("show-effort", effortShown);
});
loadSettings();


async function openProduction(slug) {
  if (last && last.running) { toast("Stop the current run before opening another production.", 4000); return; }
  try {
    const res = await fetch("/api/production/open", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ slug }) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Could not open that production.");
    const state = await fetch("/api/status").then((r) => r.json());
    apply(state);
    setView(openingView(state) || "pipeline", true);
    toast("Opened " + slug);
  } catch (error) {
    toast(error.message, 5000);
  }
}

async function useTake(id, name) {
  try {
    const res = await fetch("/api/video/take", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ shot: id, take: name }) });
    const reply = await res.json();
    if (!res.ok) throw new Error(reply.error || "Could not switch takes.");
    toast(shotLabel(last || {}, id) + " now uses " + name + ". The previous take is kept.");
  } catch (error) { toast(error.message, 6000); }
}

async function forgetClip(id) {
  if (!window.confirm("Check KIE's task history first. If KIE has no task for " + shotLabel(last || {}, id) + ", it will be sent again on the next run (paid). If KIE did create one, both are billed. Continue?")) return;
  try {
    const res = await fetch("/api/video/forget", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ shot: id }) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Could not update that clip.");
    toast("Marked as not submitted");
    fetch("/api/status").then((r) => r.json()).then(apply).catch(() => {});
  } catch (error) {
    toast(error.message, 5000);
  }
}
