const GLYPHS = {
  sheet: '<svg viewBox="0 0 8 8" aria-hidden="true"></svg>',
  page: '<svg viewBox="0 0 8 8" aria-hidden="true"></svg>',
  shot: '<svg viewBox="0 0 8 8" aria-hidden="true"></svg>',
  frame: '<svg viewBox="0 0 8 8" aria-hidden="true"></svg>'
};
const PIPS = ["brief", "development", "writer", "editor", "art", "images"];
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
const localFiles = new Map();
const LOCAL_DOCS = new Set(["json", "md", "fountain", "txt"]);
const LOCAL_IMAGES = new Set(["png", "jpg", "jpeg", "webp", "gif"]);

function plain(text) {
  return String(text || "")
    .replace(/\*\*/g, "")
    .replace(/`/g, "")
    .replace(/(?:\/[\w.-]+)+\/productions\//g, "productions/");
}

const MODEL_NAMES = {
  "nano-banana-2-1": "Nano Banana",
  "gpt-image-2-5-sunburst-text-to-image": "Sunburst",
  "gpt-image-2-5-sunburst-image-to-image": "Sunburst edit",
  "seedream/5-pro-text-to-image": "Seedream"
};

function modelTitle(model) {
  const raw = String(model || "Still");
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
  el.classList.remove("is-working", "is-done", "is-idle");
  el.classList.add(mode === "working" ? "is-working" : mode === "done" ? "is-done" : "is-idle");
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
      if (stillFilter === "waiting") show = waitingSlot || state === "running";
      else if (stillFilter !== "all") show = !ghost && kind.includes(stillFilter);
      frame.hidden = !show;
      if (show) visible += 1;
    });
    col.hidden = visible === 0;
    if (!col.hidden) shownCols += 1;
  });
  bay.dataset.filterEmpty = bay.children.length && shownCols === 0 ? "1" : "";
}

function filterLibrary() {
  const q = (document.getElementById("library-search").value || "").trim().toLowerCase();
  document.querySelectorAll("#library .file, #opened-folder .file").forEach((button) => {
    button.hidden = !!q && !button.textContent.toLowerCase().includes(q);
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
    const section = document.createElement("section");
    const head = document.createElement("div");
    head.className = "folder-head";
    const heading = document.createElement("h3");
    heading.textContent = group.slug;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "linkish";
    close.id = "close-folder";
    close.textContent = "Close";
    head.append(heading, close);
    section.appendChild(head);
    for (const [label, items] of [["Documents", group.documents], ["Images", group.images]]) {
      if (!items.length) continue;
      const name = document.createElement("p");
      name.className = "label";
      name.textContent = label;
      section.appendChild(name);
      for (const item of items) section.appendChild(fileButton(item));
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
    toast("That folder has no scripts or stills");
    return;
  }
  renderOpenedFolder(groups);
  toast(groups.length === 1 ? groups[0].slug + " opened" : groups.length + " folders opened");
}

function paintCrew(state) {
  const done = new Set(state.done || []);
  for (const id of CREW) {
    const el = stationEl(id);
    const crew = (state.crew || []).find((item) => item.id === id);
    if (crew) {
      setText(el.querySelector("strong"), crew.name);
      setText(el.querySelector(".model"), crew.model || "");
    }
    let mode = "idle";
    let label = "Waiting";
    if (state.active === id) {
      mode = "working";
      label = "Working";
    } else if (done.has(id)) {
      mode = "done";
      label = "Done";
    }
    setMode(el, mode);
    setText(el.querySelector(".status"), label);
  }
  const brief = stationEl("brief");
  const handedOff = state.active === "development" || done.has("development") || ["writer", "editor", "art", "images"].includes(state.station);
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

function placeRail() {
  const slot = document.getElementById("return-slot");
  const writer = stationEl("writer");
  const editor = stationEl("editor");
  const rail = document.getElementById("return-rail");
  if (!writer || !editor) return;
  const slotBox = slot.getBoundingClientRect();
  const writerBox = writer.getBoundingClientRect();
  const editorBox = editor.getBoundingClientRect();
  const width = editorBox.right - writerBox.left;
  if (width < 20) return;
  rail.style.marginLeft = Math.max(0, writerBox.left - slotBox.left) + "px";
  rail.style.width = width + "px";
}

function playReturn(instant) {
  const rail = document.getElementById("return-rail");
  const token = document.getElementById("return-token");
  rail.classList.add("is-live");
  token.classList.add("no-anim");
  token.style.left = instant ? "0px" : "calc(100% - 8px)";
  if (instant) return;
  void token.offsetWidth;
  token.classList.remove("no-anim");
  token.style.left = "0px";
}

function parkReturn() {
  const rail = document.getElementById("return-rail");
  const token = document.getElementById("return-token");
  rail.classList.remove("is-live");
  token.classList.add("no-anim");
  token.style.left = "calc(100% - 8px)";
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
  if (found) return found;
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
  } else if (image.state === "done") {
    frame.classList.add("is-done", "printed");
    showDone(frame, image);
  } else if (image.state === "planned") {
    img.removeAttribute("src");
    img.hidden = true;
    hold.hidden = false;
    hold.textContent = "Planned — no image generated";
  } else if (image.state === "failed") {
    frame.classList.add("is-failed");
    img.hidden = true;
    hold.hidden = false;
    hold.textContent = "Failed";
  }
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
    else if (image.state === "done") showDone(frame, image);
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
  const plaque = document.getElementById("plaque");
  setText(plaque, state.brief || "");
  plaque.hidden = !state.brief;
  document.getElementById("live").hidden = !state.running;
  stage.classList.toggle("is-running", !!state.running);
  const run = document.getElementById("run");
  run.disabled = !!state.running;
  setText(run, state.running ? "Working…" : "Start production");
  paintCrew(state);
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
  placeRail();
  const first = !prev;
  moveToken(state.station || "brief", !first);
  token.classList.toggle("is-hidden", !!state.returning);
  const label = document.getElementById("return-label");
  setText(label, state.returning ? "Script page sent back" : "Editor loop, up to two rewrites");
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
  setText(document.getElementById("count-activity"), String((state.log || []).length));
  setText(document.getElementById("stat-steps"), stepsDone + " of 5 done");
  const stillCount = (state.images || []).length;
  setText(document.getElementById("stat-stills"), stillCount + (stillCount === 1 ? " still" : " stills"));
  setText(document.getElementById("stat-loop"), state.returning ? "Rewrite in flight" : "Editor loop ready");
  if (!viewChosen) {
    if (stillCount) setView("stills");
    else if ((state.done || []).length) setView("pipeline");
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
        pages_only: document.getElementById("pages").checked,
        dry_run: document.getElementById("dry").checked,
        max_shots: document.getElementById("max-shots").value ? Number(document.getElementById("max-shots").value) : null
      })
    });
    if (!res.ok) {
      const note = document.getElementById("note");
      note.textContent = "Could not start the crew.";
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
    const nav = document.getElementById("library");
    nav.replaceChildren();
    if (!data.projects || !data.projects.length) {
      if (localFiles.size) return;
      const p = document.createElement("p");
      p.className = "empty";
      p.textContent = "No productions yet.";
      nav.appendChild(p);
      return;
    }
    for (const project of data.projects) {
      const section = document.createElement("section");
      const heading = document.createElement("h3");
      heading.textContent = project.slug;
      section.appendChild(heading);
      for (const [label, items] of [["Documents", project.documents], ["Images", project.images]]) {
        const name = document.createElement("p");
        name.className = "label";
        name.textContent = label;
        section.appendChild(name);
        if (!items.length) {
          const empty = document.createElement("p");
          empty.className = "empty";
          empty.textContent = "None yet.";
          section.appendChild(empty);
          continue;
        }
        for (const item of items) {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "file";
          if (item.path === openPath) button.classList.add("is-open");
          button.textContent = item.name;
          button.dataset.path = item.path;
          section.appendChild(button);
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
  const img = document.getElementById("reader-image");
  const src = img.getAttribute("src") || "";
  if (src.startsWith("blob:")) URL.revokeObjectURL(src);
  img.removeAttribute("src");
  img.alt = "";
  img.hidden = true;
}

function showReaderDocument(name, text) {
  readerSource = String(text == null ? "" : text);
  readerFormat = readerFormatFor(name);
  readerMode = readerFormat ? "preview" : "source";
  document.getElementById("reader-title").textContent = name || "Reader";
  releaseReaderImage();
  paintReader();
}

function showReaderImage(name, src) {
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
}

function resetReader() {
  readerSource = "";
  readerFormat = "";
  readerMode = "source";
  const hint = document.getElementById("reader-hint");
  if (hint) hint.hidden = false;
  const pre = document.getElementById("reader-text");
  pre.hidden = true;
  pre.textContent = "";
  const preview = document.getElementById("reader-preview");
  preview.hidden = true;
  preview.replaceChildren();
  releaseReaderImage();
  document.getElementById("reader-title").textContent = "Reader";
  syncPreviewButton();
}

function toggleReaderPreview() {
  if (readerFormat !== "md" && readerFormat !== "json") return;
  readerMode = readerMode === "preview" ? "source" : "preview";
  paintReader();
}

async function openLocalFile(path) {
  const file = localFiles.get(path);
  const hint = document.getElementById("reader-hint");
  if (!file) return;
  if (hint) hint.hidden = true;
  const suffix = file.name.includes(".") ? file.name.split(".").pop().toLowerCase() : "";
  if (LOCAL_IMAGES.has(suffix)) {
    showReaderImage(file.name, URL.createObjectURL(file));
    return;
  }
  showReaderDocument(file.name, await file.text());
}

async function openFile(path) {
  const hint = document.getElementById("reader-hint");
  if (hint) hint.hidden = true;
  setView("reader", true);
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
    showReaderDocument(data.name || "Reader", data.text || data.error || "That file is not in a production folder.");
  } catch (error) {
    showReaderDocument("Reader", "That file is not in a production folder.");
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
for (const name of ["idea", "pages", "dry"]) {
  document.getElementById("help-" + name).addEventListener("click", () => {
    const copy = document.getElementById("help-" + name + "-copy");
    copy.hidden = !copy.hidden;
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
document.getElementById("toggle-library").addEventListener("click", () => {
  const hidden = document.body.classList.toggle("library-hidden");
  document.getElementById("toggle-library").textContent = hidden ? "Show library" : "Hide library";
});
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
  copyText(readerSource, "Reader copied");
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
document.getElementById("library-search").addEventListener("input", filterLibrary);
document.querySelector("aside").addEventListener("click", (event) => {
  if (event.target.closest("#close-folder")) {
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
document.getElementById("clear-log").addEventListener("click", async () => {
  try {
    const res = await fetch("/api/log/clear", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    if (!res.ok) throw new Error("clear");
    toast("Activity cleared");
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
    document.getElementById("pages").checked = false;
    document.getElementById("dry").checked = false;
    document.getElementById("max-shots").value = "";
    resetReader();
    openPath = null;
    setView("brief");
    toast("New session");
  } catch (error) {
    toast("Could not start a new session");
  }
});
document.addEventListener("keydown", (event) => {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement && document.activeElement.tagName);
  if (typing) return;
  const views = ["brief", "pipeline", "stills", "activity", "reader"];
  const index = "12345".indexOf(event.key);
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
