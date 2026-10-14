const GLYPHS = {
  sheet: '<svg viewBox="0 0 8 8" aria-hidden="true"></svg>',
  page: '<svg viewBox="0 0 8 8" aria-hidden="true"></svg>',
  shot: '<svg viewBox="0 0 8 8" aria-hidden="true"></svg>',
  frame: '<svg viewBox="0 0 8 8" aria-hidden="true"></svg>'
};
const PIPS = ["brief", "development", "writer", "editor", "approval", "art", "images"];
const CREW = ["development", "writer", "editor", "art"];
const SAMPLE = "A 30-second vertical film about someone who keeps a seat on the last bus.";

let last = null;
let revision = -1;
let shown = 0;
let streamOK = false;
let openPath = null;
let readerSource = "";
let readerFormat = "";
let readerMode = "source";
let follow = true;
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
let deskFolder = "";
let referenceKey = "";
// Path -> "style" (look only) or "lock" (same subject in the shots that show it).
const selectedRefs = new Map();
const refInfo = new Map();
let uploadedRefs = [];

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
  } else if (image.state === "failed") {
    frame.classList.add("is-failed");
    img.hidden = true;
    hold.hidden = false;
    hold.textContent = "Failed";
  }
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
    line("Repair model: " + image.repair_model + ". Approval spends credits on one repair and its inspection.");
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
  if (images.some((image) => image.state === "review")) return false;
  const open = new Set(["failed", "planned", "polling", "submitting", "downloading", "running", "inspecting"]);
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
    card.appendChild(make("p", "shot-action", shot.action));
    if (shot.framing || shot.audio || shot.notes) {
      const more = make("details", "shot-more");
      more.appendChild(make("summary", "", "Camera, audio and notes"));
      if (shot.framing) more.appendChild(make("p", "shot-meta", "Camera · " + shot.framing));
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
      link.setAttribute("download", image.id + ".png");
      frame.appendChild(link);
    }
    if (link) { link.hidden = !image.src; if (image.src) link.href = image.src; }
  }
}

function syncVideoExtras(state) {
  const shots = state.shots || [];
  const video = state.video || {};
  const clips = video.clips || [];
  const details = document.getElementById("video-ready");
  details.hidden = !shots.length;
  if (shots.length) {
    const readyCount = shots.filter((shot) => (imageOf(state, shot.id) || {}).state === "done").length;
    setText(document.getElementById("video-ready-title"), "Shots and images · " + readyCount + " of " + shots.length + " ready");
    const key = JSON.stringify(shots.map((shot) => [shot.id, (imageOf(state, shot.id) || {}).state, (imageOf(state, shot.id) || {}).src]));
    const list = document.getElementById("video-ready-list");
    if (list.dataset.key !== key) {
      list.dataset.key = key;
      list.replaceChildren();
      for (const shot of shots) {
        const image = imageOf(state, shot.id);
        const ok = Boolean(image && image.state === "done");
        const row = make("li", "ready-row" + (ok ? " is-ok" : ""));
        if (image && image.src) { const thumb = make("img", "ready-thumb"); thumb.src = image.src; thumb.alt = ""; thumb.loading = "lazy"; row.appendChild(thumb); }
        else row.appendChild(make("span", "ready-thumb is-empty"));
        row.append(make("strong", "", "Shot " + shot.number + " · " + shot.duration_seconds + "s"),
          make("span", "ready-text", ok ? "Image approved" : image ? "Image " + (image.state || "pending") + ": finish it in the Images tab" : "No image yet: generate it in the Images tab"));
        list.appendChild(row);
      }
      if (!details.dataset.touched) details.open = readyCount < shots.length;
    }
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
  document.getElementById("download-all").hidden = !clips.some((clip) => clip.state === "done");
}

function syncExtras(state) {
  syncNextStep(state);
  syncShotBoard(state);
  syncImageExtras(state);
  syncVideoExtras(state);
}

function loopLabel(state) {
  if (state.can_proceed && !state.running) return state.pages_passed ? "Approval waiting" : "Draft needs a decision";
  if (reviewWaiting(state)) return "Review waiting";
  if (stillsPaused(state)) return "Images paused";
  if (state.returning) return "Rewrite in flight";
  if ((state.overridden || []).includes("editor")) return "Editor overridden";
  return "Script editor ready";
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
    if (frame.dataset.state !== (image.state || "")) applyFrameState(frame, image);
    else if (["done", "review", "inspecting"].includes(image.state)) showDone(frame, image);
    renderInspection(frame, image, state.running);
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
  if (mode) chip.title = mode === "style" ? "Style reference: look only" : "Locked reference: same subject";
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
      for (const ref of refs) row.appendChild(modeChip((ref.name || ref.id) + " · " + (ref.mode === "style" ? "style" : "lock"), ref.mode));
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
    (video.saved ? " · locked for this production once clips are generated." : " · change these in the Idea tab."));
  document.getElementById("plan-video").disabled = running || !video.can_plan;
  const generate = document.getElementById("generate-video");
  generate.disabled = running || !video.ready || Boolean(video.complete);
  setText(generate, video.complete ? "All clips ready" : video.saved ? "Resume video clips" : "Generate video clips");
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
    setText(card.querySelector("h3"), clip.id + " · " + modelTitle(clip.model || "Video") + " · " + clip.state);
    setText(card.querySelector("p"), clip.error || "");
    card.dataset.state = clip.state;
    const source = imageOf(state, clip.id);
    const planned = (video.shots || []).find((shot) => shot.id === clip.id) || {};
    const thumb = card.querySelector(".clip-thumb");
    thumb.hidden = Boolean(clip.src) || !(source && source.src);
    if (source && source.src && thumb.getAttribute("src") !== source.src) { thumb.src = source.src; thumb.alt = "First frame of " + clip.id; }
    setText(card.querySelector(".clip-meta"), [planned.duration_seconds ? planned.duration_seconds + "s" : "", shotOf(state, clip.id) ? shotOf(state, clip.id).action : ""].filter(Boolean).join(" · "));
    card.querySelector("video").poster = source && source.src ? source.src : "";
    const action = card.querySelector("button");
    setText(action, clip.state === "done" ? "Redo this shot · paid" : "Render this shot · paid");
    action.disabled = running || !video.ready || !video.saved;
    action.hidden = Boolean(video.saved) === false;
    const preview = card.querySelector("video");
    preview.hidden = !clip.src;
    if (clip.src && preview.getAttribute("src") !== clip.src) preview.src = clip.src;
    const download = card.querySelector("a.video-download");
    download.hidden = !clip.src;
    if (clip.src) { download.href = clip.src + "&download=1"; download.setAttribute("download", clip.id + ".mp4"); }
    const takes = card.querySelector(".takes");
    takes.replaceChildren();
    for (const take of clip.takes || []) {
      const link = make("a", "", "Earlier take · " + take.name);
      link.href = take.src;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      takes.append(link, document.createElement("br"));
    }
  }
  const shots = video.shots || [];
  const promptKey = JSON.stringify(shots);
  if (videoPromptKey !== promptKey) {
    videoPromptKey = promptKey;
    const prompts = document.getElementById("video-prompts");
    prompts.replaceChildren();
    for (const shot of shots) {
      prompts.append(make("h3", "", shot.id + " · " + shot.duration_seconds + "s · " + (shot.model_name || "Kling 3.0")),
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

async function startVideo(dryRun, only = {}) {
  const button = document.getElementById(dryRun ? "plan-video" : "generate-video");
  button.disabled = true;
  try {
    const response = await fetch("/api/video", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...only, dry_run: dryRun })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not start video generation.");
    setView("video", true);
    toast(dryRun ? "Saving video plan" : "Video generation started");
    await fetch("/api/status").then((res) => res.json()).then(apply);
  } catch (error) {
    toast(error.message, 6000);
    button.disabled = Boolean(only.shots || only.redo);
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
  setText(document.getElementById("stat-steps"), stepsDone + " of " + stationRow().length + " steps done");
  const stillCount = (state.images || []).length;
  setText(document.getElementById("stat-stills"), stillCount + (stillCount === 1 ? " image" : " images"));
  setText(document.getElementById("stat-loop"), loopLabel(state));
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
  if (state.revision === revision) return;
  const prev = last;
  last = state;
  revision = state.revision;
  paint(state, prev);
}

function startDust() {}

function suggestLabel() {
  ideaCount();
  const button = document.getElementById("suggest");
  if (!button || button.dataset.busy === "1") return;
  const empty = !document.getElementById("idea").value.trim();
  button.textContent = empty ? "Suggest a story" : "Strengthen this";
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
  button.textContent = "Writing…";
  try {
    const res = await fetch("/api/brief/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idea: idea.value })
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
    toast("Brief ready to edit");
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
  const parts = [deskFolder];
  for (const project of libraryProjects) {
    parts.push(project.slug + "=" + (project.images || []).map((item) => item.path).join(","));
  }
  parts.push(uploadedRefs.map((item) => item.id).join(","));
  return parts.join("\n");
}

function referenceSummary() {
  if (!selectedRefs.size) return "None selected";
  const styles = [...selectedRefs.values()].filter((mode) => mode === "style").length;
  const locks = selectedRefs.size - styles;
  const parts = [];
  if (styles) parts.push(styles + " style");
  if (locks) parts.push(locks + " locked");
  return parts.join(" · ");
}

function renderReferenceTray() {
  const tray = document.getElementById("reference-selected");
  tray.replaceChildren();
  for (const [path, mode] of selectedRefs) {
    const info = refInfo.get(path) || { name: path, src: "/library-media?path=" + encodeURIComponent(path) };
    const card = make("div", "ref-chosen");
    const img = document.createElement("img");
    img.alt = "";
    img.src = info.src;
    const body = make("div", "ref-chosen-body");
    const name = make("span", "ref-chosen-name", info.name);
    name.title = info.name;
    const toggle = make("div", "segmented");
    toggle.setAttribute("role", "group");
    toggle.setAttribute("aria-label", "How to use " + info.name);
    for (const [value, label] of [["style", "Style"], ["lock", "Lock"]]) {
      const option = make("button", value === mode ? "is-on" : "", label);
      option.type = "button";
      option.setAttribute("aria-pressed", value === mode ? "true" : "false");
      option.title = value === "style" ? "Look only, on every shot" : "Same subject, only where it appears";
      option.addEventListener("click", () => {
        selectedRefs.set(path, value);
        markReferenceSelection();
      });
      toggle.appendChild(option);
    }
    body.append(name, toggle);
    const remove = make("button", "ref-remove", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", "Remove " + info.name);
    remove.addEventListener("click", () => {
      selectedRefs.delete(path);
      markReferenceSelection();
    });
    card.append(img, body, remove);
    tray.appendChild(card);
  }
  tray.hidden = !selectedRefs.size;
}

function markReferenceSelection() {
  document.querySelectorAll("#reference-picker .ref-thumb").forEach((button) => {
    const mode = selectedRefs.get(button.dataset.ref);
    button.classList.toggle("is-selected", !!mode);
    button.dataset.badge = mode === "lock" ? "Lock" : mode ? "Style" : "";
    button.setAttribute("aria-pressed", mode ? "true" : "false");
  });
  document.getElementById("reference-count").textContent = referenceSummary();
  renderReferenceTray();
  briefSummary();
}

function referenceThumb(item) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "ref-thumb";
  button.dataset.ref = item.path;
  button.title = item.name || item.path;
  const img = document.createElement("img");
  img.alt = "";
  img.src = item.src || ("/library-media?path=" + encodeURIComponent(item.path));
  refInfo.set(item.path, { name: item.name || item.path, src: img.src });
  const caption = document.createElement("span");
  caption.textContent = item.name || item.path;
  button.append(img, caption);
  button.addEventListener("click", () => {
    if (selectedRefs.has(item.path)) selectedRefs.delete(item.path);
    else selectedRefs.set(item.path, "style");
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
  for (const project of libraryProjects) {
    const images = (project.images || []).filter((item) => REFERENCE_EXT.has(referenceExtension(item.path || item.name)));
    if (slug && project.slug === slug) mine.push(...images);
    else others.push(...images.map((item) => ({ ...item, name: project.slug + "/" + item.name })));
  }
  fillReferenceGrid(current, mine, slug ? "No images in this production yet." : "No production is on the desk yet.");
  fillReferenceGrid(document.getElementById("reference-other"), others, "No other images in the library.");
  fillReferenceGrid(
    document.getElementById("reference-uploads"),
    uploadedRefs.map((item) => ({ path: item.id, name: item.name, src: item.src })),
    ""
  );
  const uploads = document.getElementById("reference-uploads");
  if (!uploadedRefs.length) uploads.replaceChildren();
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
  selectedRefs.set(data.id, "style");
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

// Shot counts follow budget.py: at least one per 15s, at most one per 3s, one per 8s by default.
function briefEstimate() {
  const note = document.getElementById("format-estimate");
  const run = document.getElementById("run");
  const runtime = Number(document.getElementById("runtime").value);
  const maxShots = Number(document.getElementById("max-shots").value);
  let problem = "";
  if (runtime && (!Number.isInteger(runtime) || runtime < 6 || runtime > 720)) problem = "Length must be a whole number between 6 and 720 seconds.";
  else if (maxShots && (!Number.isInteger(maxShots) || maxShots < 1 || maxShots > 240)) problem = "Maximum shots must be between 1 and 240.";
  const low = runtime ? Math.max(1, Math.ceil(runtime / 15)) : 0;
  const high = runtime ? Math.min(240, Math.max(1, Math.floor(runtime / 3))) : 0;
  if (!problem && runtime && maxShots && (maxShots < low || maxShots > high)) problem = "For " + runtime + " seconds, maximum shots must be between " + low + " and " + high + ".";
  note.dataset.bad = problem ? "1" : "";
  run.disabled = Boolean(problem) || Boolean(last && last.running);
  if (problem) { setText(note, problem); return; }
  if (!runtime) { setText(note, "Leave these empty and the idea decides. Shots last 3–15 seconds and together fill the length exactly."); return; }
  const shots = maxShots || Math.max(low, Math.floor(runtime / 8));
  setText(note, runtime + "s film · up to " + shots + (shots === 1 ? " shot" : " shots") + " → " + shots + " shot images and up to " + shots + " video clips, plus character and location images. Each clip is 3–15 seconds.");
}

function ideaCount() {
  const words = document.getElementById("idea").value.trim().split(/\s+/).filter(Boolean).length;
  setText(document.getElementById("idea-count"), words ? words + (words === 1 ? " word" : " words") : "Empty");
}

async function startRun() {
  releaseNote();
  const run = document.getElementById("run");
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
        references: [...selectedRefs].map(([path, mode]) => ({ path, mode }))
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
      const { section, content } = libraryFolder(project, "production");
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
document.getElementById("brief-form").addEventListener("input", briefSummary);
document.getElementById("track").addEventListener("click", (event) => {
  const station = event.target.closest(".station");
  if (station) pickStation(station.dataset.station);
});
document.getElementById("detail-follow").addEventListener("click", () => pickStation(""));
for (const name of ["idea", "gate", "dry"]) {
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
document.getElementById("sample").addEventListener("click", () => {
  document.getElementById("idea").value = SAMPLE;
  document.getElementById("idea").focus();
  suggestLabel();
});
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
  if (!window.confirm("Clear this session and start from a blank brief?")) return;
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
  const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement && document.activeElement.tagName);
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


// ---- Models and quality (Idea tab) -------------------------------------------------
let studioSettings = null;
let settingsSchema = null;

function settingsRow(label, help, control) {
  const row = make("div", "settings-row");
  const text = make("label", "settings-label");
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

function renderSettings() {
  const host = document.getElementById("settings-host");
  if (!studioSettings || !settingsSchema) return;
  const values = studioSettings;
  host.replaceChildren();
  const group = (title, open) => {
    const box = make("details", "settings-group");
    box.open = open;
    box.appendChild(make("summary", "", title));
    host.appendChild(box);
    return box;
  };
  const save = () => saveSettings();

  const crew = group("Crew · chat models", true);
  for (const role of settingsSchema.chat.roles) {
    crew.appendChild(settingsRow(role.label, role.help, settingsSelect(settingsSchema.chat.models, values.chat[role.id], (value) => { values.chat[role.id] = value; save(); })));
  }
  crew.appendChild(make("p", "field-note", "Models differ in cost and quality. Premium models write better stories; the lighter one is faster for checklist work."));

  const images = group("Images", true);
  for (const item of settingsSchema.images.groups) {
    images.appendChild(settingsRow(item.label, item.help, settingsSelect(item.options, values.images[item.id], (value) => { values.images[item.id] = value; save(); })));
    const chosen = item.options.find((option) => option.id === values.images[item.id]) || item.options[0];
    const detail = chosen.resolutions.length ? chosen.resolutions.join(" / ") : "fixed quality";
    images.appendChild(modelNote(chosen.note + " " + (chosen.max_references ? "Up to " + chosen.max_references + " reference images" : "No reference images") + " · resolution " + detail + ".", chosen.sources));
  }
  const resolutions = settingsSchema.images.resolutions.map((id) => ({ id, name: id === "auto" ? "Auto · the image prompts decide (1K)" : id }));
  images.appendChild(settingsRow("Image resolution", "Higher resolution costs more. A model that cannot do a size falls back to Nano Banana.", settingsSelect(resolutions, values.images.resolution, (value) => { values.images.resolution = value; save(); })));

  const video = group("Video clips", true);
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
  const sound = document.createElement("input");
  sound.type = "checkbox";
  sound.checked = values.video.sound;
  sound.addEventListener("change", () => { values.video.sound = sound.checked; save(); });
  const soundLabel = make("label", "check");
  soundLabel.append(sound, document.createTextNode(" Generate dialogue and sound from the shot notes"));
  video.appendChild(soundLabel);
  const parallel = settingsSchema.video.parallel.map((count) => ({ id: String(count), name: count + (count === 1 ? " clip" : " clips") + " at a time" }));
  video.appendChild(settingsRow("Clips generated at once", "More is faster; each running clip is billed as usual.", settingsSelect(parallel, String(values.video.parallel), (value) => { values.video.parallel = Number(value); save(); })));
}

let settingsSaveTimer = 0;
async function saveSettings() {
  renderSettings();
  clearTimeout(settingsSaveTimer);
  settingsSaveTimer = setTimeout(async () => {
    try {
      const response = await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ values: studioSettings }) });
      const data = await response.json();
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
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not load settings.");
    studioSettings = data.values;
    if (data.schema) settingsSchema = data.schema;
    if (!settingsSchema) settingsSchema = await fetch("/api/settings").then((res) => res.json()).then((body) => body.schema);
    renderSettings();
    if (reset) toast("Settings reset to defaults");
  } catch (error) {
    setText(document.querySelector("#settings-host .field-note"), error.message);
  }
}
document.getElementById("settings-reset").addEventListener("click", () => loadSettings(true));
loadSettings();
