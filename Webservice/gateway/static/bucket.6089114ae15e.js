/* chudware-web /bucket — files workspace (personal + shared team destinations).
 *
 * Two hosts, one implementation:
 *   1. the standalone /bucket page, where this script is deferred and mounts
 *      the .bucket-surface node it finds in the document;
 *   2. the cloud app's "Workspaces and files" modal, which imports
 *      that same .bucket-surface node from fetched /bucket HTML, links this
 *      file's stylesheet, and loads this script ONCE. Every later open calls
 *      window.ChudwareBucket.mount(freshRoot).
 *
 * So: no module-level DOM lookups, all element references live inside the
 * per-mount closure, and document-level drag listeners are installed exactly
 * once and always routed to the currently mounted surface.
 */
(function () {
  "use strict";

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
  // Shared across mounts so reopening the modal cannot hide an active transfer.
  var activeUploads = 0;

  // A hung GET must never leave the surface sitting on a spinner: every read
  // is bounded and surfaces a retryable error instead.
  var READ_TIMEOUT_MS = 20000;

  // Row and state icons. Line style mirrors the portal icon set: 24 viewBox,
  // stroke 1.7, round caps/joins, decorative (aria-hidden) with the adjacent
  // text carrying the meaning.
  function lineIcon(body) {
    return '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" fill="none" ' +
           'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" ' +
           'stroke-linejoin="round">' + body + "</svg>";
  }

  var ICONS = {
    dir: lineIcon('<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/>'),
    file: lineIcon('<path d="M14 2.6H6.2A1.8 1.8 0 0 0 4.4 4.4v15.2a1.8 1.8 0 0 0 1.8 1.8h11.6a1.8 1.8 0 0 0 1.8-1.8V8.2z"/><path d="M14 2.6v5.6h5.6"/>'),
    doc: lineIcon('<path d="M14 2.6H6.2A1.8 1.8 0 0 0 4.4 4.4v15.2a1.8 1.8 0 0 0 1.8 1.8h11.6a1.8 1.8 0 0 0 1.8-1.8V8.2z"/><path d="M14 2.6v5.6h5.6M8.4 13h7.2M8.4 16.4h4.8"/>'),
    image: lineIcon('<rect x="3.6" y="4.6" width="16.8" height="14.8" rx="2.2"/><circle cx="9" cy="9.6" r="1.6"/><path d="M20.4 15.4 15.8 10.8l-7.4 7.4"/>'),
    model: lineIcon('<path d="M3.6 7.9 12 3.6l8.4 4.3v8.2L12 20.4 3.6 16.1z"/><path d="M3.6 7.9 12 12.2l8.4-4.3M12 12.2v8.2"/>'),
    archive: lineIcon('<path d="M4.6 4.6h14.8v3.6H4.6zM6 8.2v11.2h12V8.2"/><path d="M10.4 11.8h3.2"/>'),
    code: lineIcon('<rect x="3" y="4.6" width="18" height="14.8" rx="2.2"/><path d="M7.4 10l2.6 2.4-2.6 2.4M12.8 15h4"/>'),
    empty: '<svg class="state-glyph" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/><path d="M12 11v5M9.5 13.5h5"/></svg>',
    error: '<svg class="state-glyph" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.2v.6"/></svg>',
  };

  // Extension -> ICONS key. CAD and mesh formats get the package icon; the
  // fallback for anything unlisted is the plain file.
  var EXT_ICONS = {};
  [
    ["model", "step stp iges igs stl obj 3mf amf brep sldprt sldasm prt asm f3d fcstd scad aicm dxf dwg"],
    ["image", "png jpg jpeg gif webp svg bmp tif tiff avif heic"],
    ["archive", "zip tar gz tgz bz2 xz zst 7z rar"],
    ["code", "js mjs ts py c cc cpp h hpp rs go java kt swift sh bat json xml yaml yml toml ini cfg html css"],
    ["doc", "txt md rst rtf csv tsv log pdf doc docx odt"],
  ].forEach(function (group) {
    group[1].split(" ").forEach(function (ext) { EXT_ICONS[ext] = group[0]; });
  });

  function iconFor(name) {
    var match = /\.([a-z0-9]+)$/i.exec(name || "");
    var kind = match && EXT_ICONS[match[1].toLowerCase()];
    return (kind && ICONS[kind]) || ICONS.file;
  }

  // ------------------------------------------------------------- formatting

  function fmtSize(n) {
    if (typeof n !== "number" || !isFinite(n)) return "—";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    if (n < 1024 * 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + " MB";
    return (n / (1024 * 1024 * 1024)).toFixed(2) + " GB";
  }

  function fmtDate(unixSec) {
    if (!unixSec) return "—";
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

  function errText(e) {
    return String((e && e.message) || e || "unknown error");
  }

  function elem(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  // ------------------------------------------------------- upload plumbing

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

  // ------------------------------------------------------------------ fetch

  async function request(path, opts, timeoutMs) {
    var init = Object.assign({ credentials: "same-origin" }, opts || {});
    var timer = null;
    if (timeoutMs && typeof AbortController !== "undefined") {
      var ctl = new AbortController();
      init.signal = ctl.signal;
      timer = setTimeout(function () { ctl.abort(); }, timeoutMs);
    }
    var res;
    try {
      res = await fetch(path, init);
    } catch (e) {
      if (e && e.name === "AbortError") {
        throw new Error("the server did not answer within " +
                        Math.round(timeoutMs / 1000) + "s");
      }
      throw new Error(errText(e));
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
    var data = null;
    try { data = await res.json(); } catch (e) { /* non-JSON body */ }
    if (!res.ok) {
      throw new Error((data && data.detail) || res.status + " " + res.statusText);
    }
    return data;
  }

  // ------------------------------------------- one mounted surface at a time

  var active = null;        // currently mounted surface handle
  var docBound = false;     // document-level drag listeners installed once

  function bindDocumentDragOnce() {
    if (docBound) return;
    docBound = true;
    // Allow dropping anywhere on the page/modal, not just the highlighted box.
    document.addEventListener("dragover", function (e) {
      e.preventDefault();
      if (active) active.dragHint();
    });
    document.addEventListener("dragleave", function () {
      if (active) active.dragHintOff();
    });
    document.addEventListener("drop", function (e) {
      if (!active) return;
      e.preventDefault();
      active.dragHintOff();
      if (active.ownsDrop(e.target)) return;  // the zone's own handler took it
      var dt = e.dataTransfer;
      if (dt) entriesFromDataTransfer(dt).then(active.upload);
    });
  }

  function mount(root) {
    if (!root) return null;

    var need = function (id) {
      var node = root.querySelector("#" + id);
      if (!node) throw new Error("bucket surface is missing #" + id);
      return node;
    };

    var listEl, crumbsEl, statusEl, countEl, noticeEl, scopeKindEl, scopeTitleEl,
        scopeContextEl, scopeGlyphEl, destSelect, dropZone, fileInput, folderInput,
        progressEl, inputDialog, confirmDialog, newFolderBtn, uploadBtn,
        uploadFolderBtn, promptTitle, promptLabel, promptConfirm, promptField,
        confirmTitle, confirmBody, confirmAccept, openWorkspaceBtn;
    try {
      listEl = need("list");
      crumbsEl = need("crumbs");
      statusEl = need("status");
      countEl = need("list-count");
      noticeEl = need("notice");
      scopeKindEl = need("scope-kind");
      scopeTitleEl = need("scope-title");
      scopeContextEl = need("scope-context");
      scopeGlyphEl = need("scope-glyph");
      destSelect = need("dest-select");
      openWorkspaceBtn = need("open-workspace-btn");
      dropZone = need("drop-zone");
      fileInput = need("file-input");
      folderInput = need("folder-input");
      progressEl = need("upload-progress");
      newFolderBtn = need("new-folder-btn");
      uploadBtn = need("upload-btn");
      uploadFolderBtn = need("upload-folder-btn");
      inputDialog = need("input-dialog");
      promptTitle = need("input-dialog-title");
      promptLabel = need("input-dialog-label");
      promptConfirm = need("input-dialog-confirm");
      promptField = need("input-dialog-field");
      confirmDialog = need("confirm-dialog");
      confirmTitle = need("confirm-dialog-title");
      confirmBody = need("confirm-dialog-body");
      confirmAccept = need("confirm-dialog-confirm");
    } catch (e) {
      root.textContent = "";
      var fatal = elem("div", "list-state error");
      fatal.insertAdjacentHTML("beforeend", ICONS.error);
      fatal.appendChild(elem("p", "state-title", "This file browser could not start"));
      fatal.appendChild(elem("p", null, errText(e) + " — reload the page to try again."));
      root.appendChild(fatal);
      return null;
    }

    // Only the real /bucket document owns the address bar and the tab title.
    var standalone = window.location.pathname === "/bucket";

    var state = {
      dir: "",
      team: null,   // null => personal; else an entry of state.teams
      teams: [],    // flattened accessible teams from /api/orgs
      ready: false,
      destinationsLoaded: false,
      opening: false,
    };
    var listSeq = 0;
    var dragTimer = null;

    // ---------------------------------------------------------- small setters

    function setStatus(msg, cls) {
      statusEl.textContent = msg || "";
      statusEl.className = "status" + (cls ? " " + cls : "");
    }

    function setCount(text) {
      countEl.textContent = text || "";
    }

    function setNotice(msg) {
      noticeEl.textContent = msg || "";
      noticeEl.hidden = !msg;
    }

    function updateControls() {
      destSelect.disabled = !state.destinationsLoaded || state.opening;
      openWorkspaceBtn.disabled = !state.ready || state.opening;
      newFolderBtn.disabled = uploadBtn.disabled = uploadFolderBtn.disabled =
        !state.ready || state.opening;
    }

    // Every bucket call carries the selected destination, including explicit
    // personal scope, independent of the CAD workspace selection cookie.
    function bucketQuery(params, teamId) {
      var q = new URLSearchParams(params || {});
      var id = teamId !== undefined ? teamId : (state.team ? state.team.id : null);
      if (id) q.set("team", id);
      else q.set("scope", "personal");
      return q.toString();
    }

    function scopeLabel() {
      return state.team ? "the " + state.team.name + " team space (" + state.team.orgName + ")"
                        : "your personal space";
    }

    // ------------------------------------------------------------ destinations

    async function loadDestinations() {
      var teams = [];
      var payload = await request("/api/orgs", null, READ_TIMEOUT_MS);
      if (!Array.isArray(payload.organizations)) {
        throw new Error("The workspace list response was invalid.");
      }
      payload.organizations.forEach(function (org) {
        (org.teams || []).forEach(function (team) {
          teams.push({
            id: String(team.id),
            name: team.name,
            role: team.role || "",
            storageProvider: team.storage_provider,
            memberCount: team.member_count,
            orgName: org.name,
            orgRole: org.role || "",
          });
        });
      });
      state.teams = teams;
      state.destinationsLoaded = true;
    }

    function renderDestinations() {
      destSelect.textContent = "";
      if (!state.ready) {
        var placeholder = new Option("Choose a workspace", "");
        placeholder.disabled = true;
        destSelect.appendChild(placeholder);
      }
      destSelect.appendChild(new Option("Personal — only you", "personal"));
      state.teams.forEach(function (t) {
        destSelect.appendChild(new Option(t.orgName + " / " + t.name, t.id));
      });
      destSelect.value = state.ready ? (state.team ? state.team.id : "personal") : "";
    }

    function renderPending() {
      scopeGlyphEl.className = "dest-glyph";
      scopeKindEl.className = "dest-kind is-pending";
      scopeKindEl.textContent = "Workspace";
      scopeTitleEl.textContent = "Workspaces and files";
      scopeContextEl.textContent = "Loading your workspaces…";
      openWorkspaceBtn.textContent = "Open workspace";
    }

    function renderScope() {
      if (state.team) {
        var t = state.team;
        scopeGlyphEl.className = "dest-glyph is-team";
        scopeKindEl.className = "dest-kind is-team";
        scopeKindEl.textContent = t.storageProvider === "box" ? "Live Box-backed team space" : "Shared team space";
        scopeTitleEl.textContent = t.name;
        var role = t.role ? titleCase(t.role) : titleCase(t.orgRole) + " (organization)";
        scopeContextEl.textContent =
          t.orgName + " · " + plural(t.memberCount, "member") +
          " can read and write here · your role: " + role;
      } else {
        scopeGlyphEl.className = "dest-glyph is-personal";
        scopeKindEl.className = "dest-kind is-personal";
        scopeKindEl.textContent = "Personal space";
        scopeTitleEl.textContent = "Personal workspace";
        scopeContextEl.textContent = "Private to your account — no one else can see these files.";
      }
      openWorkspaceBtn.textContent = "Open workspace — " +
        (state.team ? state.team.name : "Personal");
      if (standalone) {
        document.title = (state.team ? state.team.name : "Personal files") + " — Chudware";
      }
    }

    function syncUrl() {
      if (!standalone) return;
      var q = new URLSearchParams(window.location.search);
      q.set("team", state.team ? state.team.id : "personal");
      history.replaceState(null, "", "/bucket?" + q.toString());
    }

    function selectDestination(team) {
      state.team = team;
      state.ready = true;
      state.dir = "";
      setNotice("");
      syncUrl();
      renderDestinations();
      renderScope();
      updateControls();
      setStatus("");
      refresh();
    }

    destSelect.addEventListener("change", function () {
      if (!state.destinationsLoaded || state.opening) return;
      var value = destSelect.value;
      var team = state.teams.find(function (t) { return t.id === value; });
      if (value !== "personal" && !team) {
        setNotice("That workspace is unavailable. Choose another workspace or retry.");
        return;
      }
      selectDestination(team || null);
    });

    openWorkspaceBtn.addEventListener("click", async function () {
      if (!state.ready || state.opening) return;
      if (activeUploads) {
        setStatus("Wait for uploads to finish before opening a workspace.", "err");
        return;
      }
      var teamId = state.team ? state.team.id : "personal";
      var name = state.team ? state.team.name : "Personal";
      state.opening = true;
      updateControls();
      try {
        if (!standalone && !await askConfirm({
          title: "Open " + name + " workspace?",
          body: "Save your work before continuing. Opening this workspace reloads CAD in this tab. Unsaved changes will be lost.",
          confirmLabel: "Open workspace",
          primary: true,
        })) return;
        if (activeUploads) {
          setStatus("Wait for uploads to finish before opening a workspace.", "err");
          return;
        }
        window.location.assign("/app/?team=" + encodeURIComponent(teamId));
      } catch (e) {
        setStatus("Could not open the workspace: " + errText(e), "err");
      } finally {
        state.opening = false;
        updateControls();
      }
    });

    // ----------------------------------------------------------------- crumbs

    function renderCrumbs() {
      crumbsEl.textContent = "";
      var rootName = state.team ? state.team.name : "Personal";
      var parts = state.dir ? state.dir.split("/") : [];
      function crumb(label, target, isLast, isRoot) {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "crumb" + (isRoot ? " crumb-root" : "");
        btn.textContent = label;
        btn.title = label;
        if (isLast) {
          btn.setAttribute("aria-current", "location");
          btn.disabled = true;
        } else {
          btn.addEventListener("click", function () { navigate(target); });
        }
        crumbsEl.appendChild(btn);
      }
      crumb(rootName, "", parts.length === 0, true);
      var acc = "";
      parts.forEach(function (part, i) {
        acc = acc ? acc + "/" + part : part;
        var sep = elem("span", "crumb-sep", "/");
        sep.setAttribute("aria-hidden", "true");
        crumbsEl.appendChild(sep);
        crumb(part, acc, i === parts.length - 1, false);
      });
    }

    // ------------------------------------------------------------ list states

    function stateBlock(kind, opts) {
      var box = elem("div", "list-state " + kind);
      if (kind === "loading") {
        box.appendChild(elem("div", "state-spinner"));
      } else if (ICONS[kind]) {
        box.insertAdjacentHTML("beforeend", ICONS[kind]);
      }
      if (opts.title) box.appendChild(elem("p", "state-title", opts.title));
      if (opts.body) {
        var p = elem("p", null, opts.body);
        if (opts.detail) p.appendChild(elem("code", null, opts.detail));
        box.appendChild(p);
      }
      if (opts.action) {
        var btn = elem("button", "btn" + (opts.actionPrimary ? " primary" : ""), opts.action);
        btn.type = "button";
        btn.addEventListener("click", opts.onAction);
        box.appendChild(btn);
      }
      return box;
    }

    function showListState(kind, opts) {
      listEl.textContent = "";
      listEl.appendChild(stateBlock(kind, opts));
    }

    // ------------------------------------------------------------------- rows

    function rowActions(item, isDir) {
      var actions = elem("span", "row-actions");
      if (!isDir) {
        var dl = elem("a", "row-act", "Download");
        dl.href = "/api/bucket/download?" + bucketQuery({ path: item.path });
        dl.setAttribute("download", "");
        dl.setAttribute("aria-label", "Download " + item.name);
        actions.appendChild(dl);
      }
      var ren = elem("button", "row-act", "Rename");
      ren.type = "button";
      ren.setAttribute("aria-label", "Rename " + item.name);
      ren.addEventListener("click", function () { renameItem(item.path, item.name); });
      actions.appendChild(ren);
      var del = elem("button", "row-act row-danger", "Delete");
      del.type = "button";
      del.setAttribute("aria-label", "Delete " + item.name);
      del.addEventListener("click", function () { deleteItem(item.path, item.name, isDir); });
      actions.appendChild(del);
      return actions;
    }

    function dirRow(d) {
      var li = elem("li", "row is-dir");
      li.insertAdjacentHTML("afterbegin", ICONS.dir);
      var open = elem("button", "row-name", d.name);
      open.type = "button";
      open.title = d.name;
      open.setAttribute("aria-label", "Open folder " + d.name);
      open.addEventListener("click", function () { navigate(d.path); });
      li.appendChild(open);
      li.appendChild(elem("span", "row-size", "Folder"));
      li.appendChild(elem("span", "row-when", "—"));
      li.appendChild(rowActions(d, true));
      return li;
    }

    function fileRow(f) {
      var li = elem("li", "row");
      li.insertAdjacentHTML("afterbegin", iconFor(f.name));
      var name = elem("a", "row-name", f.name);
      name.href = "/api/bucket/download?" + bucketQuery({ path: f.path });
      name.setAttribute("download", "");
      name.title = f.name;
      li.appendChild(name);
      li.appendChild(elem("span", "row-size", fmtSize(f.size)));
      li.appendChild(elem("span", "row-when", fmtDate(f.mtime)));
      li.appendChild(rowActions(f, false));
      return li;
    }

    function rowsHead() {
      var head = elem("div", "rows-head");
      head.setAttribute("aria-hidden", "true");
      head.appendChild(elem("span", null, ""));
      head.appendChild(elem("span", null, "Name"));
      head.appendChild(elem("span", null, "Size"));
      head.appendChild(elem("span", null, "Modified"));
      head.appendChild(elem("span", null, ""));
      return head;
    }

    async function refresh() {
      if (!state.ready) return;
      var seq = ++listSeq;
      renderCrumbs();
      listEl.setAttribute("aria-busy", "true");
      setCount("");
      showListState("loading", {
        title: "Loading files…",
        body: "Reading " + (state.dir ? "“" + state.dir + "” in " : "") + scopeLabel() + ".",
      });
      var data;
      try {
        data = await request("/api/bucket/list?" + bucketQuery({ dir: state.dir }),
                             null, READ_TIMEOUT_MS);
      } catch (e) {
        if (seq !== listSeq) return;
        listEl.setAttribute("aria-busy", "false");
        showListState("error", {
          title: "Couldn't load this folder",
          body: "The file list request failed.",
          detail: errText(e),
          action: "Retry",
          onAction: refresh,
        });
        return;
      }
      if (seq !== listSeq) return;
      state.dir = data.dir || "";
      renderCrumbs();
      listEl.setAttribute("aria-busy", "false");
      var dirs = data.dirs || [];
      var files = data.files || [];
      if (!dirs.length && !files.length) {
        showListState("empty", {
          title: state.dir ? "“" + state.dir + "” is empty" : "Nothing here yet",
          body: "Drag files or folders in, or use Upload files and New folder above. " +
                "Everything you add lands in " + scopeLabel() + ".",
          action: "Upload files",
          actionPrimary: true,
          onAction: function () { fileInput.click(); },
        });
        return;
      }
      var frag = document.createDocumentFragment();
      frag.appendChild(rowsHead());
      var ul = elem("ul", "rows");
      dirs.forEach(function (d) { ul.appendChild(dirRow(d)); });
      files.forEach(function (f) { ul.appendChild(fileRow(f)); });
      frag.appendChild(ul);
      listEl.textContent = "";
      listEl.appendChild(frag);
      var bytes = files.reduce(function (a, f) { return a + (f.size || 0); }, 0);
      setCount(
        (dirs.length ? plural(dirs.length, "folder") + " · " : "") +
        plural(files.length, "file") + (files.length ? " · " + fmtSize(bytes) : "")
      );
    }

    function navigate(dir) {
      state.dir = dir;
      refresh();
    }

    // ---------------------------------------------------------------- dialogs

    function promptDialog(opts) {
      return new Promise(function (resolve) {
        promptTitle.textContent = opts.title;
        promptLabel.textContent = opts.label;
        promptConfirm.textContent = opts.confirmLabel || "Save";
        var field = promptField;
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

    function askConfirm(opts) {
      return new Promise(function (resolve) {
        confirmTitle.textContent = opts.title;
        confirmBody.textContent = opts.body;
        confirmAccept.textContent = opts.confirmLabel || "Delete";
        confirmAccept.className = opts.primary ? "btn primary" : "btn danger";
        confirmDialog.returnValue = "";
        function onClose() {
          confirmDialog.removeEventListener("close", onClose);
          resolve(confirmDialog.returnValue === "confirm");
        }
        confirmDialog.addEventListener("close", onClose);
        confirmDialog.showModal();
      });
    }

    // -------------------------------------------------------------- mutations

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
        await request("/api/bucket/rename?" + bucketQuery(), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ src: path, name: name }),
        });
        setStatus("Renamed “" + currentName + "” to “" + name + "”.", "ok");
        refresh();
      } catch (e) { setStatus(errText(e), "err"); }
    }

    async function deleteItem(path, name, isDir) {
      var ok = await askConfirm({
        title: "Delete " + (isDir ? "folder" : "file"),
        body: "Delete “" + name + "” from " + scopeLabel() + "? This cannot be undone" +
              (isDir ? " and removes everything inside the folder." : "."),
        confirmLabel: "Delete",
      });
      if (!ok) return;
      try {
        await request("/api/bucket/delete?" + bucketQuery(), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: path }),
        });
        setStatus("Deleted “" + name + "”.", "ok");
        refresh();
      } catch (e) { setStatus(errText(e), "err"); }
    }

    newFolderBtn.addEventListener("click", async function () {
      if (!state.ready || state.opening) return;
      var name = await promptDialog({
        title: "New folder",
        label: "Folder name (created in " + scopeLabel() + ")",
        confirmLabel: "Create",
      });
      if (!name) return;
      if (name.indexOf("/") !== -1) { setStatus("Names cannot contain “/”.", "err"); return; }
      var path = state.dir ? state.dir + "/" + name : name;
      try {
        await request("/api/bucket/mkdir?" + bucketQuery(), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: path }),
        });
        setStatus("Folder “" + name + "” created.", "ok");
        refresh();
      } catch (e) { setStatus(errText(e), "err"); }
    });

    // ----------------------------------------------------------------- upload

    function uploadBatch(entries, dir, teamId) {
      var form = new FormData();
      entries.forEach(function (e) { form.append("files", e.file, e.relPath); });
      form.append("dir", dir);
      return request("/api/bucket/upload?" + bucketQuery(null, teamId),
                     { method: "POST", body: form });
    }

    async function uploadEntries(entries) {
      if (!entries || !entries.length) return;
      if (!state.ready || state.opening) {
        setStatus("Choose a loaded workspace before uploading files.", "err");
        return;
      }
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
            failedBatches.push(errText(e));
          }
          doneBatches++;
          progressEl.value = doneBatches;
          if (totalBatches > 1) {
            setStatus("Uploading to " + destination + "… part " + doneBatches + " of " +
                      totalBatches + ", " + plural(uploadedCount, "file") + " done so far");
          }
        }
      }

      setStatus("Uploading " + plural(entries.length, "file") + " to " + destination +
                (totalBatches > 1 ? " in " + plural(totalBatches, "part") : "") + "…");
      activeUploads++;
      try {
        var workers = [];
        for (var i = 0; i < Math.min(UPLOAD_CONCURRENCY, totalBatches); i++) workers.push(worker());
        await Promise.all(workers);
      } finally {
        activeUploads--;
      }

      progressEl.hidden = true;
      var msg = "Uploaded " + plural(uploadedCount, "file") + " to " + destination + ".";
      if (skippedCount) msg += " " + plural(skippedCount, "file") + " had a name that can't be stored and " + (skippedCount === 1 ? "was" : "were") + " skipped.";
      if (oversize.length) msg += " " + plural(oversize.length, "file") + " over the 95 MB browser upload limit " + (oversize.length === 1 ? "was" : "were") + " not uploaded — use the desktop app for very large files.";
      if (failedBatches.length) msg += " " + plural(failedBatches.length, "part") + " failed: " + failedBatches[0] + (failedBatches.length > 1 ? " (+" + (failedBatches.length - 1) + " more)" : "");
      setStatus(msg, (failedBatches.length || oversize.length) ? "err" : "ok");
      // Only repaint if the user is still looking at the destination we wrote to.
      if (state.dir === dir && (state.team ? state.team.id : null) === teamId) refresh();
    }

    uploadBtn.addEventListener("click", function () { fileInput.click(); });
    fileInput.addEventListener("change", function () {
      uploadEntries(entriesFromFileList(fileInput.files));
      fileInput.value = "";
    });

    uploadFolderBtn.addEventListener("click", function () { folderInput.click(); });
    folderInput.addEventListener("change", function () {
      uploadEntries(entriesFromFileList(folderInput.files));
      folderInput.value = "";
    });

    // Drag highlighting: the zone reacts to a drag anywhere over this surface
    // (or the modal hosting it) and clears itself on a short idle timer, so a
    // dragleave storm across child nodes cannot leave it stuck highlighted.
    function dragHint() {
      dropZone.classList.add("drag");
      if (dragTimer !== null) clearTimeout(dragTimer);
      dragTimer = setTimeout(function () {
        dragTimer = null;
        dropZone.classList.remove("drag");
      }, 220);
    }
    function dragHintOff() {
      if (dragTimer !== null) { clearTimeout(dragTimer); dragTimer = null; }
      dropZone.classList.remove("drag");
    }

    ["dragenter", "dragover"].forEach(function (evt) {
      dropZone.addEventListener(evt, function (e) {
        e.preventDefault(); e.stopPropagation();
        dragHint();
      });
    });
    dropZone.addEventListener("dragleave", function (e) {
      e.preventDefault(); e.stopPropagation();
      dragHintOff();
    });
    dropZone.addEventListener("drop", function (e) {
      e.preventDefault(); e.stopPropagation();
      dragHintOff();
      var dt = e.dataTransfer;
      if (dt) entriesFromDataTransfer(dt).then(uploadEntries);
    });

    // -------------------------------------------------------------------- init

    async function init() {
      state.ready = false;
      state.destinationsLoaded = false;
      state.team = null;
      state.dir = "";
      ++listSeq;
      updateControls();
      setNotice("");
      setStatus("");
      setCount("");
      crumbsEl.textContent = "";
      renderPending();
      destSelect.textContent = "";
      destSelect.appendChild(new Option("Loading workspaces…", ""));
      listEl.setAttribute("aria-busy", "true");
      showListState("loading", {
        title: "Loading your workspaces…",
        body: "Finding your personal and shared team workspaces.",
      });
      var requested = new URLSearchParams(window.location.search).get("team");
      await loadDestinations();
      if (requested === null) {
        var account = await request("/api/portal/account", null, READ_TIMEOUT_MS);
        requested = account.workspace ? String(account.workspace.id) : "personal";
      }
      var chosen = null;
      if (requested !== "personal") {
        chosen = state.teams.find(function (t) { return t.id === requested; }) || null;
        if (!chosen) {
          throw new Error("The selected team is unavailable. Choose another workspace above, or retry to reload your teams.");
        }
      }
      state.team = chosen;
      state.ready = true;
      syncUrl();
      renderDestinations();
      renderScope();
      updateControls();
      await refresh();
    }

    // The surface may never be left sitting on a bare loading message: any
    // unexpected failure still ends on a labelled, retryable state.
    function start() {
      init().catch(function (e) {
        state.ready = false;
        if (state.destinationsLoaded) {
          renderDestinations();
        } else {
          destSelect.textContent = "";
          destSelect.appendChild(new Option("Workspaces unavailable", ""));
        }
        updateControls();
        scopeGlyphEl.className = "dest-glyph";
        scopeKindEl.className = "dest-kind is-pending";
        scopeKindEl.textContent = "Workspace";
        scopeTitleEl.textContent = "Workspace unavailable";
        scopeContextEl.textContent = state.destinationsLoaded
          ? "Choose a workspace above, or retry below."
          : "Workspace list unavailable — retry below.";
        setNotice("Could not load your workspace: " + errText(e));
        listEl.setAttribute("aria-busy", "false");
        showListState("error", {
          title: "Couldn't load your workspace",
          body: "No workspace has been selected. Your CAD session has not changed.",
          detail: errText(e),
          action: "Retry",
          onAction: start,
        });
      });
    }

    bindDocumentDragOnce();
    active = {
      root: root,
      upload: uploadEntries,
      dragHint: dragHint,
      dragHintOff: dragHintOff,
      ownsDrop: function (target) {
        return target === dropZone || (target && dropZone.contains(target));
      },
      reload: start,
    };
    start();
    return active;
  }

  window.ChudwareBucket = {
    // Mount (or re-mount) a freshly inserted .bucket-surface node. Safe to
    // call any number of times; the script itself is only ever loaded once.
    mount: function (root) {
      var handle = mount(root || document.querySelector(".bucket-surface"));
      window.ChudwareBucket.mounted = !!handle;
      return handle;
    },
    mounted: false,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      window.ChudwareBucket.mount();
    });
  } else {
    window.ChudwareBucket.mount();
  }
}());
