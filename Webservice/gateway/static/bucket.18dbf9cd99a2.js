/* chudware-web /bucket — files workspace (personal + shared team destinations) */
(function () {
  "use strict";

  function el(id) { return document.getElementById(id); }

  var listEl = el("list");
  var crumbsEl = el("crumbs");
  var statusEl = el("status");
  var noticeEl = el("notice");
  var scopeTitleEl = el("scope-title");
  var scopeContextEl = el("scope-context");
  var destSelect = el("dest-select");
  var dropZone = el("drop-zone");
  var fileInput = el("file-input");
  var folderInput = el("folder-input");
  var progressEl = el("upload-progress");
  var inputDialog = el("input-dialog");
  var confirmDialog = el("confirm-dialog");

  var state = {
    dir: "",
    team: null,   // null => personal; else an entry of state.teams
    teams: [],    // flattened accessible teams from /api/orgs
  };

  // ---------------------------------------------------------------- helpers

  function setStatus(msg, cls) {
    statusEl.textContent = msg || "";
    statusEl.className = "status" + (cls ? " " + cls : "");
  }

  function setNotice(msg) {
    noticeEl.textContent = msg || "";
    noticeEl.hidden = !msg;
  }

  async function api(path, opts) {
    var init = Object.assign({ credentials: "same-origin" }, opts || {});
    var res = await fetch(path, init);
    var data = null;
    try { data = await res.json(); } catch (e) { /* non-JSON body */ }
    if (!res.ok) {
      throw new Error((data && data.detail) || res.status + " " + res.statusText);
    }
    return data;
  }

  // Every bucket call carries the selected destination. Personal scope sends
  // no team parameter at all, so the API's existing personal behavior applies.
  function bucketQuery(params, teamId) {
    var q = new URLSearchParams(params || {});
    var id = teamId !== undefined ? teamId : (state.team ? state.team.id : null);
    if (id) q.set("team", id);
    return q.toString();
  }

  function fmtSize(n) {
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    if (n < 1024 * 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + " MB";
    return (n / (1024 * 1024 * 1024)).toFixed(2) + " GB";
  }

  function fmtDate(unixSec) {
    var d = new Date(unixSec * 1000);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " +
           d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  }

  function plural(n, word) {
    return n + " " + word + (n === 1 ? "" : "s");
  }

  function titleCase(s) {
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
  }

  // ------------------------------------------------------------ destinations

  async function loadDestinations() {
    var teams = [];
    try {
      var payload = await api("/api/orgs");
      (payload.organizations || []).forEach(function (org) {
        (org.teams || []).forEach(function (team) {
          teams.push({
            id: team.id,
            name: team.name,
            role: team.role || "",
            memberCount: team.member_count,
            orgName: org.name,
            orgRole: org.role || "",
          });
        });
      });
    } catch (e) {
      setNotice("Could not load your teams (" + String(e.message || e) +
                "). Showing personal files; reload to try again.");
    }
    state.teams = teams;
  }

  function renderDestinations() {
    destSelect.textContent = "";
    destSelect.appendChild(new Option("Personal — only you", "personal"));
    state.teams.forEach(function (t) {
      destSelect.appendChild(new Option(t.orgName + " / " + t.name, t.id));
    });
    destSelect.value = state.team ? state.team.id : "personal";
  }

  function renderScope() {
    if (state.team) {
      var t = state.team;
      scopeTitleEl.textContent = t.name;
      var role = t.role ? titleCase(t.role) : titleCase(t.orgRole) + " (organization)";
      scopeContextEl.textContent =
        "Shared team space · " + t.orgName + " · " +
        plural(t.memberCount, "member") + " · your role: " + role;
    } else {
      scopeTitleEl.textContent = "Personal files";
      scopeContextEl.textContent = "Private to your account — no one else can see these files.";
    }
    document.title = (state.team ? state.team.name : "Personal files") + " — Chudware";
  }

  function syncUrl() {
    var q = new URLSearchParams(window.location.search);
    q.set("team", state.team ? state.team.id : "personal");
    history.replaceState(null, "", "/bucket?" + q.toString());
  }

  function selectDestination(team) {
    state.team = team;
    state.dir = "";
    syncUrl();
    renderScope();
    destSelect.value = team ? team.id : "personal";
    setStatus("");
    refresh();
  }

  destSelect.addEventListener("change", function () {
    var value = destSelect.value;
    selectDestination(value === "personal" ? null :
      state.teams.find(function (t) { return t.id === value; }) || null);
  });

  // ------------------------------------------------------------------ crumbs

  function renderCrumbs() {
    crumbsEl.textContent = "";
    var rootName = state.team ? state.team.name : "Personal";
    var parts = state.dir ? state.dir.split("/") : [];
    function crumb(label, target, isLast) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "crumb";
      btn.textContent = label;
      if (isLast) {
        btn.setAttribute("aria-current", "location");
        btn.disabled = true;
      } else {
        btn.addEventListener("click", function () { navigate(target); });
      }
      crumbsEl.appendChild(btn);
    }
    crumb(rootName, "", parts.length === 0);
    var acc = "";
    parts.forEach(function (part, i) {
      acc = acc ? acc + "/" + part : part;
      var sep = document.createElement("span");
      sep.className = "crumb-sep";
      sep.setAttribute("aria-hidden", "true");
      sep.textContent = "/";
      crumbsEl.appendChild(sep);
      crumb(part, acc, i === parts.length - 1);
    });
  }

  // -------------------------------------------------------------------- list

  var ICONS = {
    dir: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/></svg>',
    file: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>',
  };

  function stateBlock(kind, message, retry) {
    var box = document.createElement("div");
    box.className = "list-state " + kind;
    var p = document.createElement("p");
    p.textContent = message;
    box.appendChild(p);
    if (retry) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn";
      btn.textContent = "Retry";
      btn.addEventListener("click", refresh);
      box.appendChild(btn);
    }
    return box;
  }

  function rowActions(item, isDir) {
    var actions = document.createElement("span");
    actions.className = "row-actions";
    if (!isDir) {
      var dl = document.createElement("a");
      dl.className = "row-act";
      dl.href = "/api/bucket/download?" + bucketQuery({ path: item.path });
      dl.setAttribute("download", "");
      dl.textContent = "Download";
      actions.appendChild(dl);
    }
    var ren = document.createElement("button");
    ren.type = "button";
    ren.className = "row-act";
    ren.textContent = "Rename";
    ren.setAttribute("aria-label", "Rename " + item.name);
    ren.addEventListener("click", function () { renameItem(item.path, item.name); });
    actions.appendChild(ren);
    var del = document.createElement("button");
    del.type = "button";
    del.className = "row-act row-danger";
    del.textContent = "Delete";
    del.setAttribute("aria-label", "Delete " + item.name);
    del.addEventListener("click", function () { deleteItem(item.path, item.name, isDir); });
    actions.appendChild(del);
    return actions;
  }

  function dirRow(d) {
    var li = document.createElement("li");
    li.className = "row is-dir";
    li.innerHTML = ICONS.dir;
    var open = document.createElement("button");
    open.type = "button";
    open.className = "row-name";
    open.textContent = d.name;
    open.setAttribute("aria-label", "Open folder " + d.name);
    open.addEventListener("click", function () { navigate(d.path); });
    li.appendChild(open);
    var meta = document.createElement("span");
    meta.className = "row-meta";
    meta.textContent = "Folder";
    li.appendChild(meta);
    li.appendChild(rowActions(d, true));
    return li;
  }

  function fileRow(f) {
    var li = document.createElement("li");
    li.className = "row";
    li.innerHTML = ICONS.file;
    var name = document.createElement("a");
    name.className = "row-name";
    name.href = "/api/bucket/download?" + bucketQuery({ path: f.path });
    name.setAttribute("download", "");
    name.textContent = f.name;
    li.appendChild(name);
    var meta = document.createElement("span");
    meta.className = "row-meta";
    meta.textContent = fmtSize(f.size) + " · " + fmtDate(f.mtime);
    li.appendChild(meta);
    li.appendChild(rowActions(f, false));
    return li;
  }

  var listSeq = 0;

  async function refresh() {
    var seq = ++listSeq;
    renderCrumbs();
    listEl.setAttribute("aria-busy", "true");
    listEl.textContent = "";
    listEl.appendChild(stateBlock("loading", "Loading files…"));
    var data;
    try {
      data = await api("/api/bucket/list?" + bucketQuery({ dir: state.dir }));
    } catch (e) {
      if (seq !== listSeq) return;
      listEl.textContent = "";
      listEl.appendChild(stateBlock("error", "Could not load this folder: " + String(e.message || e), true));
      listEl.setAttribute("aria-busy", "false");
      return;
    }
    if (seq !== listSeq) return;
    state.dir = data.dir || "";
    renderCrumbs();
    listEl.textContent = "";
    listEl.setAttribute("aria-busy", "false");
    if (!data.dirs.length && !data.files.length) {
      var where = state.team ? "this team space" : "your personal bucket";
      listEl.appendChild(stateBlock("empty",
        "Nothing in " + where + " yet — drag files in, or use Upload files or New folder above."));
      return;
    }
    var ul = document.createElement("ul");
    ul.className = "rows";
    data.dirs.forEach(function (d) { ul.appendChild(dirRow(d)); });
    data.files.forEach(function (f) { ul.appendChild(fileRow(f)); });
    listEl.appendChild(ul);
  }

  function navigate(dir) {
    state.dir = dir;
    refresh();
  }

  // ----------------------------------------------------------------- dialogs

  function promptDialog(opts) {
    return new Promise(function (resolve) {
      el("input-dialog-title").textContent = opts.title;
      el("input-dialog-label").textContent = opts.label;
      el("input-dialog-confirm").textContent = opts.confirmLabel || "Save";
      var field = el("input-dialog-field");
      field.value = opts.value || "";
      function onClose() {
        inputDialog.removeEventListener("close", onClose);
        var value = field.value.trim();
        resolve(inputDialog.returnValue === "confirm" && value ? value : null);
      }
      inputDialog.addEventListener("close", onClose);
      inputDialog.showModal();
      field.select();
    });
  }

  function confirmDialog_(opts) {
    return new Promise(function (resolve) {
      el("confirm-dialog-title").textContent = opts.title;
      el("confirm-dialog-body").textContent = opts.body;
      el("confirm-dialog-confirm").textContent = opts.confirmLabel || "Delete";
      function onClose() {
        confirmDialog.removeEventListener("close", onClose);
        resolve(confirmDialog.returnValue === "confirm");
      }
      confirmDialog.addEventListener("close", onClose);
      confirmDialog.showModal();
    });
  }

  function scopeLabel() {
    return state.team ? "the " + state.team.name + " team space (" + state.team.orgName + ")"
                      : "your personal bucket";
  }

  // --------------------------------------------------------------- mutations

  async function renameItem(path, currentName) {
    var name = await promptDialog({
      title: "Rename",
      label: "New name for “" + currentName + "”",
      value: currentName,
      confirmLabel: "Rename",
    });
    if (!name || name === currentName) return;
    if (name.indexOf("/") !== -1) { setStatus("Names cannot contain “/”.", "err"); return; }
    try {
      await api("/api/bucket/rename?" + bucketQuery(), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ src: path, name: name }),
      });
      setStatus("Renamed “" + currentName + "” to “" + name + "”.", "ok");
      refresh();
    } catch (e) { setStatus(String(e.message || e), "err"); }
  }

  async function deleteItem(path, name, isDir) {
    var ok = await confirmDialog_({
      title: "Delete " + (isDir ? "folder" : "file"),
      body: "Delete “" + name + "” from " + scopeLabel() + "? This cannot be undone" +
            (isDir ? " and removes everything inside the folder." : "."),
      confirmLabel: "Delete",
    });
    if (!ok) return;
    try {
      await api("/api/bucket/delete?" + bucketQuery(), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: path }),
      });
      setStatus("Deleted “" + name + "”.", "ok");
      refresh();
    } catch (e) { setStatus(String(e.message || e), "err"); }
  }

  el("new-folder-btn").addEventListener("click", async function () {
    var name = await promptDialog({
      title: "New folder",
      label: "Folder name (created in " + scopeLabel() + ")",
      confirmLabel: "Create",
    });
    if (!name) return;
    if (name.indexOf("/") !== -1) { setStatus("Names cannot contain “/”.", "err"); return; }
    var path = state.dir ? state.dir + "/" + name : name;
    try {
      await api("/api/bucket/mkdir?" + bucketQuery(), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: path }),
      });
      setStatus("Folder “" + name + "” created.", "ok");
      refresh();
    } catch (e) { setStatus(String(e.message || e), "err"); }
  });

  // ----------------------------------------------------------------- uploads

  // The whole app sits behind a Cloudflare Tunnel, which hard-caps every HTTP
  // request at 100MB (Free/Pro plans) — no way around it short of upgrading
  // the Cloudflare plan. A folder drop can easily add up to far more than
  // that in one shot, and a request over the cap gets killed at Cloudflare's
  // edge before it ever reaches this app, which surfaces in the browser as a
  // bare "Failed to fetch" network error. So: split into small batches well
  // under the cap and upload them with limited concurrency instead of one
  // giant multipart POST.
  var UPLOAD_BATCH_TARGET_BYTES = 40 * 1024 * 1024;  // soft grouping size per request
  var UPLOAD_HARD_CAP_BYTES = 95 * 1024 * 1024;      // safety margin under Cloudflare's 100MB/request cap
  var UPLOAD_BATCH_MAX_FILES = 100;
  var UPLOAD_CONCURRENCY = 3;

  // entries: array of { file: File, relPath: "sub/dir/name.ext" }
  function chunkEntries(entries) {
    var batches = [];
    var cur = [];
    var curBytes = 0;
    entries.forEach(function (e) {
      var sz = e.file.size || 0;
      if (cur.length && (curBytes + sz > UPLOAD_BATCH_TARGET_BYTES || cur.length >= UPLOAD_BATCH_MAX_FILES)) {
        batches.push(cur);
        cur = [];
        curBytes = 0;
      }
      cur.push(e);
      curBytes += sz;
    });
    if (cur.length) batches.push(cur);
    return batches;
  }

  function uploadBatch(entries, dir, teamId) {
    var form = new FormData();
    entries.forEach(function (e) { form.append("files", e.file, e.relPath); });
    form.append("dir", dir);
    return api("/api/bucket/upload?" + bucketQuery(null, teamId), { method: "POST", body: form });
  }

  async function uploadEntries(entries) {
    if (!entries || !entries.length) return;
    // Freeze the destination at drop time: switching scope or folder while an
    // upload runs must not redirect the remaining batches.
    var dir = state.dir;
    var teamId = state.team ? state.team.id : null;
    var destination = scopeLabel();
    var oversize = entries.filter(function (e) { return (e.file.size || 0) > UPLOAD_HARD_CAP_BYTES; });
    entries = entries.filter(function (e) { return (e.file.size || 0) <= UPLOAD_HARD_CAP_BYTES; });
    var batches = chunkEntries(entries);
    var uploadedCount = 0, skippedCount = 0;
    var failedBatches = [];
    var next = 0, doneBatches = 0;
    var totalBatches = batches.length;

    progressEl.max = Math.max(totalBatches, 1);
    progressEl.value = 0;
    progressEl.hidden = totalBatches < 2;

    async function worker() {
      while (next < batches.length) {
        var batch = batches[next++];
        try {
          var r = await uploadBatch(batch, dir, teamId);
          uploadedCount += r.uploaded.length;
          skippedCount += (r.skipped || []).length;
        } catch (e) {
          failedBatches.push(String(e.message || e));
        }
        doneBatches++;
        progressEl.value = doneBatches;
        if (totalBatches > 1) {
          setStatus("Uploading to " + destination + "… " + doneBatches + "/" + totalBatches +
                    " batch(es), " + uploadedCount + " file(s) done so far");
        }
      }
    }

    setStatus("Uploading " + entries.length + " file(s) to " + destination +
              (totalBatches > 1 ? " in " + totalBatches + " batch(es)" : "") + "…");
    var workers = [];
    for (var i = 0; i < Math.min(UPLOAD_CONCURRENCY, totalBatches); i++) workers.push(worker());
    await Promise.all(workers);

    progressEl.hidden = true;
    var msg = "Uploaded " + uploadedCount + " file(s) to " + destination + ".";
    if (skippedCount) msg += " Skipped " + skippedCount + ".";
    if (oversize.length) msg += " " + oversize.length + " file(s) exceeded the ~95MB single-request limit (Cloudflare Tunnel cap) and were not uploaded.";
    if (failedBatches.length) msg += " " + failedBatches.length + " batch(es) failed: " + failedBatches[0] + (failedBatches.length > 1 ? " (+" + (failedBatches.length - 1) + " more)" : "");
    setStatus(msg, (failedBatches.length || oversize.length) ? "err" : "ok");
    // Only repaint if the user is still looking at the destination we wrote to.
    if (state.dir === dir && (state.team ? state.team.id : null) === teamId) refresh();
  }

  // Flat FileList (plain <input multiple> or drag-drop fallback without entry API).
  function entriesFromFileList(fileList) {
    return Array.prototype.map.call(fileList || [], function (f) {
      return { file: f, relPath: f.webkitRelativePath || f.name };
    });
  }

  // Recursively walk a dropped FileSystemEntry (file or directory) into flat entries.
  function readEntry(entry, prefix) {
    return new Promise(function (resolve) {
      if (entry.isFile) {
        entry.file(function (file) {
          resolve([{ file: file, relPath: prefix + entry.name }]);
        }, function () { resolve([]); });
      } else if (entry.isDirectory) {
        var reader = entry.createReader();
        var all = [];
        (function readBatch() {
          reader.readEntries(function (batch) {
            if (!batch.length) {
              Promise.all(all.map(function (child) {
                return readEntry(child, prefix + entry.name + "/");
              })).then(function (nested) {
                resolve([].concat.apply([], nested));
              });
            } else {
              all = all.concat(batch);
              readBatch();
            }
          }, function () { resolve([]); });
        })();
      } else {
        resolve([]);
      }
    });
  }

  // Dropped items: use the DataTransferItem entry API (supports folders) when
  // available, falling back to the flat FileList (no folder support) otherwise.
  async function entriesFromDataTransfer(dt) {
    var items = dt && dt.items;
    if (items && items.length && items[0].webkitGetAsEntry) {
      var roots = [];
      for (var i = 0; i < items.length; i++) {
        var entry = items[i].webkitGetAsEntry && items[i].webkitGetAsEntry();
        if (entry) roots.push(entry);
      }
      if (roots.length) {
        var nested = await Promise.all(roots.map(function (e) { return readEntry(e, ""); }));
        return [].concat.apply([], nested);
      }
    }
    return entriesFromFileList(dt && dt.files);
  }

  el("upload-btn").addEventListener("click", function () { fileInput.click(); });
  fileInput.addEventListener("change", function () {
    uploadEntries(entriesFromFileList(fileInput.files));
    fileInput.value = "";
  });

  el("upload-folder-btn").addEventListener("click", function () { folderInput.click(); });
  folderInput.addEventListener("change", function () {
    uploadEntries(entriesFromFileList(folderInput.files));
    folderInput.value = "";
  });

  ["dragenter", "dragover"].forEach(function (evt) {
    dropZone.addEventListener(evt, function (e) {
      e.preventDefault(); e.stopPropagation();
      dropZone.classList.add("drag");
    });
  });
  ["dragleave", "drop"].forEach(function (evt) {
    dropZone.addEventListener(evt, function (e) {
      e.preventDefault(); e.stopPropagation();
      dropZone.classList.remove("drag");
    });
  });
  dropZone.addEventListener("drop", function (e) {
    var dt = e.dataTransfer;
    if (dt) entriesFromDataTransfer(dt).then(uploadEntries);
  });
  // Allow dropping anywhere on the page, not just the highlighted box.
  document.addEventListener("dragover", function (e) { e.preventDefault(); });
  document.addEventListener("drop", function (e) {
    e.preventDefault();
    if (e.target === dropZone) return; // already handled above
    var dt = e.dataTransfer;
    if (dt) entriesFromDataTransfer(dt).then(uploadEntries);
  });

  // -------------------------------------------------------------------- init

  async function init() {
    var requested = new URLSearchParams(window.location.search).get("team");
    await loadDestinations();
    var chosen = null;
    if (requested && requested !== "personal") {
      chosen = state.teams.find(function (t) { return t.id === requested; }) || null;
      if (!chosen) {
        setNotice("That team isn't available from this account. Showing " +
                  (state.teams.length ? "your first team" : "your personal files") + " instead.");
      }
    }
    if (!chosen && requested !== "personal") chosen = state.teams[0] || null;
    state.team = chosen;
    syncUrl();
    renderDestinations();
    renderScope();
    refresh();
  }

  init();
})();
