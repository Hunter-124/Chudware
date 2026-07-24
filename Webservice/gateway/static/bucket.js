/* chudware-web /bucket — team file-drop browser */
(function () {
  "use strict";

  var crumbsEl = document.getElementById("crumbs");
  var listEl = document.getElementById("list");
  var statusEl = document.getElementById("status");
  var dropZone = document.getElementById("drop-zone");
  var fileInput = document.getElementById("file-input");
  var folderInput = document.getElementById("folder-input");

  var state = { dir: "" };

  function setStatus(msg, cls) {
    statusEl.textContent = msg || "";
    statusEl.className = cls || "";
  }

  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  async function api(path, opts) {
    var res = await fetch(path, opts);
    var data = null;
    try { data = await res.json(); } catch (e) { /* non-JSON (download) */ }
    if (!res.ok) {
      throw new Error((data && data.detail) || res.status + " " + res.statusText);
    }
    return data;
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

  function renderCrumbs() {
    crumbsEl.innerHTML = "";
    var rootBtn = document.createElement("button");
    rootBtn.textContent = "Bucket";
    rootBtn.addEventListener("click", function () { navigate(""); });
    crumbsEl.appendChild(rootBtn);
    if (!state.dir) return;
    var parts = state.dir.split("/");
    var acc = "";
    parts.forEach(function (part) {
      acc = acc ? acc + "/" + part : part;
      var sep = document.createElement("span");
      sep.className = "sep";
      sep.textContent = " / ";
      crumbsEl.appendChild(sep);
      var btn = document.createElement("button");
      btn.textContent = part;
      var target = acc;
      btn.addEventListener("click", function () { navigate(target); });
      crumbsEl.appendChild(btn);
    });
  }

  function dirIconSvg() {
    return '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/></svg>';
  }
  function fileIconSvg() {
    return '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>';
  }

  async function refresh() {
    setStatus("");
    var data;
    try {
      data = await api("/api/bucket/list?dir=" + encodeURIComponent(state.dir));
    } catch (e) {
      listEl.innerHTML = "";
      setStatus(String(e.message || e), "err");
      return;
    }
    state.dir = data.dir || "";
    renderCrumbs();
    listEl.innerHTML = "";
    if (!data.dirs.length && !data.files.length) {
      var empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "Empty — drag files in or use Upload / New folder above.";
      listEl.appendChild(empty);
      return;
    }
    data.dirs.forEach(function (d) {
      var row = document.createElement("div");
      row.className = "row dir";
      row.innerHTML =
        dirIconSvg() +
        "<span class='name'>" + esc(d.name) + "</span>" +
        "<span class='meta'>folder</span>" +
        "<span class='actions'>" +
        "<button class='ren' title='Rename'>Rename</button>" +
        "<button class='del' title='Delete'>Delete</button>" +
        "</span>";
      row.querySelector(".name").addEventListener("click", function () { navigate(d.path); });
      row.querySelector(".ren").addEventListener("click", function (ev) { ev.stopPropagation(); renameItem(d.path, d.name); });
      row.querySelector(".del").addEventListener("click", function (ev) { ev.stopPropagation(); deleteItem(d.path, d.name); });
      listEl.appendChild(row);
    });
    data.files.forEach(function (f) {
      var row = document.createElement("div");
      row.className = "row";
      var dlHref = "/api/bucket/download?path=" + encodeURIComponent(f.path);
      row.innerHTML =
        fileIconSvg() +
        "<span class='name'>" + esc(f.name) + "</span>" +
        "<span class='meta'>" + fmtSize(f.size) + " &middot; " + fmtDate(f.mtime) + "</span>" +
        "<span class='actions'>" +
        "<a href='" + dlHref + "' download title='Download'>Download</a>" +
        "<button class='ren' title='Rename'>Rename</button>" +
        "<button class='del' title='Delete'>Delete</button>" +
        "</span>";
      row.querySelector(".ren").addEventListener("click", function () { renameItem(f.path, f.name); });
      row.querySelector(".del").addEventListener("click", function () { deleteItem(f.path, f.name); });
      listEl.appendChild(row);
    });
  }

  function navigate(dir) {
    state.dir = dir;
    refresh();
  }

  async function renameItem(path, currentName) {
    var name = prompt("Rename to:", currentName);
    if (!name || name === currentName) return;
    try {
      await api("/api/bucket/rename", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ src: path, name: name }),
      });
      setStatus("Renamed.", "ok");
      refresh();
    } catch (e) { setStatus(String(e.message || e), "err"); }
  }

  async function deleteItem(path, name) {
    if (!confirm("Delete \"" + name + "\"? This cannot be undone.")) return;
    try {
      await api("/api/bucket/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: path }),
      });
      setStatus("Deleted.", "ok");
      refresh();
    } catch (e) { setStatus(String(e.message || e), "err"); }
  }

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

  async function uploadBatch(entries) {
    var form = new FormData();
    entries.forEach(function (e) { form.append("files", e.file, e.relPath); });
    form.append("dir", state.dir);
    return api("/api/bucket/upload", { method: "POST", body: form });
  }

  async function uploadEntries(entries) {
    if (!entries || !entries.length) return;
    var oversize = entries.filter(function (e) { return (e.file.size || 0) > UPLOAD_HARD_CAP_BYTES; });
    entries = entries.filter(function (e) { return (e.file.size || 0) <= UPLOAD_HARD_CAP_BYTES; });
    var batches = chunkEntries(entries);
    var uploadedCount = 0, skippedCount = 0;
    var failedBatches = [];
    var next = 0, doneBatches = 0;
    var totalBatches = batches.length;

    async function worker() {
      while (next < batches.length) {
        var batch = batches[next++];
        try {
          var r = await uploadBatch(batch);
          uploadedCount += r.uploaded.length;
          skippedCount += (r.skipped || []).length;
        } catch (e) {
          failedBatches.push(String(e.message || e));
        }
        doneBatches++;
        if (totalBatches > 1) {
          setStatus("Uploading… " + doneBatches + "/" + totalBatches + " batch(es), " + uploadedCount + " file(s) done so far");
        }
      }
    }

    setStatus("Uploading " + entries.length + " file(s)" + (totalBatches > 1 ? " in " + totalBatches + " batch(es)" : "") + "…");
    var workers = [];
    for (var i = 0; i < Math.min(UPLOAD_CONCURRENCY, totalBatches); i++) workers.push(worker());
    await Promise.all(workers);

    var msg = "Uploaded " + uploadedCount + " file(s).";
    if (skippedCount) msg += " Skipped " + skippedCount + ".";
    if (oversize.length) msg += " " + oversize.length + " file(s) exceeded the ~95MB single-request limit (Cloudflare Tunnel cap) and were not uploaded.";
    if (failedBatches.length) msg += " " + failedBatches.length + " batch(es) failed: " + failedBatches[0] + (failedBatches.length > 1 ? " (+" + (failedBatches.length - 1) + " more)" : "");
    setStatus(msg, (failedBatches.length || oversize.length) ? "err" : "ok");
    refresh();
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

  document.getElementById("upload-btn").addEventListener("click", function () { fileInput.click(); });
  fileInput.addEventListener("change", function () {
    uploadEntries(entriesFromFileList(fileInput.files));
    fileInput.value = "";
  });

  document.getElementById("upload-folder-btn").addEventListener("click", function () { folderInput.click(); });
  folderInput.addEventListener("change", function () {
    uploadEntries(entriesFromFileList(folderInput.files));
    folderInput.value = "";
  });

  document.getElementById("new-folder-btn").addEventListener("click", async function () {
    var name = prompt("New folder name:");
    if (!name) return;
    var path = state.dir ? state.dir + "/" + name : name;
    try {
      await api("/api/bucket/mkdir", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: path }),
      });
      setStatus("Folder created.", "ok");
      refresh();
    } catch (e) { setStatus(String(e.message || e), "err"); }
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

  refresh();
})();
