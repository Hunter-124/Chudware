mergeInto(LibraryManager.library, {
  chudware_ws_browse__proxy: 'sync',
  chudware_ws_browse__deps: ['$UTF8ToString', '$stringToUTF8', '$lengthBytesUTF8'],
  chudware_ws_browse: function(mode, acceptPtr, sugPtr) {
    var accept = acceptPtr ? UTF8ToString(acceptPtr) : '';
    var suggested = sugPtr ? UTF8ToString(sugPtr) : '';

    // Tear down any leftover overlay.
    var old = document.getElementById('chudware-ws-browse');
    if (old) try { old.remove(); } catch (e) {}

    var finished = false;
    function finish(ok, path) {
      if (finished) return;
      finished = true;
      try {
        var el = document.getElementById('chudware-ws-browse');
        if (el) el.remove();
      } catch (e) {}
      var pathPtr = 0;
      if (ok && path) {
        var nb = lengthBytesUTF8(path) + 1;
        pathPtr = Module._malloc(nb);
        stringToUTF8(path, pathPtr, nb);
      }
      Module._chudware_ws_browse_done(ok ? 1 : 0, pathPtr);
      if (pathPtr) Module._free(pathPtr);
    }

    var acceptList = [];
    if (accept) {
      accept.split(',').forEach(function(s) {
        s = (s || '').trim().toLowerCase();
        if (!s) return;
        if (s.charAt(0) !== '.') s = '.' + s;
        acceptList.push(s);
      });
    }
    function extOk(name) {
      if (!acceptList.length) return true;
      var low = (name || '').toLowerCase();
      for (var i = 0; i < acceptList.length; i++) {
        if (low.endsWith(acceptList[i])) return true;
      }
      return false;
    }

    var overlay = document.createElement('div');
    overlay.id = 'chudware-ws-browse';
    overlay.style.cssText =
      'position:fixed;inset:0;z-index:999999;background:rgba(0,0,0,0.45);' +
      'display:flex;align-items:center;justify-content:center;font-family:' +
      'system-ui,-apple-system,Segoe UI,Roboto,sans-serif;';

    var panel = document.createElement('div');
    panel.style.cssText =
      'background:#1e1e24;color:#e8e8ee;border-radius:10px;width:min(560px,92vw);' +
      'max-height:min(70vh,640px);display:flex;flex-direction:column;' +
      'box-shadow:0 12px 40px rgba(0,0,0,0.45);border:1px solid #3a3a48;';

    var title = document.createElement('div');
    title.style.cssText =
      'padding:12px 16px;font-weight:600;font-size:14px;border-bottom:1px solid #3a3a48;';
    title.textContent = mode === 1 ? 'Open Project Folder' :
                        mode === 2 ? 'Save As' : 'Open from Workspace';
    panel.appendChild(title);

    var crumb = document.createElement('div');
    crumb.style.cssText =
      'padding:8px 16px;font-size:12px;color:#a0a0b0;border-bottom:1px solid #2a2a34;' +
      'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
    panel.appendChild(crumb);

    var list = document.createElement('div');
    list.style.cssText =
      'flex:1;overflow:auto;padding:6px 8px;min-height:200px;';
    panel.appendChild(list);

    var nameRow = null;
    var nameInput = null;
    if (mode === 2) {
      nameRow = document.createElement('div');
      nameRow.style.cssText =
        'display:flex;gap:8px;align-items:center;padding:8px 16px;' +
        'border-top:1px solid #2a2a34;';
      var nameLab = document.createElement('span');
      nameLab.textContent = 'Name:';
      nameLab.style.cssText = 'font-size:12px;color:#a0a0b0;';
      nameInput = document.createElement('input');
      nameInput.type = 'text';
      nameInput.value = suggested || 'untitled';
      nameInput.style.cssText =
        'flex:1;background:#121218;color:#e8e8ee;border:1px solid #3a3a48;' +
        'border-radius:6px;padding:6px 8px;font-size:13px;outline:none;';
      nameRow.appendChild(nameLab);
      nameRow.appendChild(nameInput);
      panel.appendChild(nameRow);
    }

    var bar = document.createElement('div');
    bar.style.cssText =
      'display:flex;gap:8px;justify-content:flex-end;padding:10px 16px;' +
      'border-top:1px solid #3a3a48;';

    var uploadBtn = null;
    if (mode === 0) {
      uploadBtn = document.createElement('button');
      uploadBtn.textContent = 'Upload from my machine…';
      uploadBtn.style.cssText =
        'margin-right:auto;background:#2a2a38;color:#e8e8ee;border:1px solid #4a4a5a;' +
        'border-radius:6px;padding:6px 12px;font-size:12px;cursor:pointer;';
      bar.appendChild(uploadBtn);
    }

    var cancelBtn = document.createElement('button');
    cancelBtn.textContent = 'Cancel';
    cancelBtn.style.cssText =
      'background:transparent;color:#c0c0cc;border:1px solid #4a4a5a;' +
      'border-radius:6px;padding:6px 14px;font-size:12px;cursor:pointer;';
    cancelBtn.onclick = function() { finish(0, null); };

    var okBtn = document.createElement('button');
    okBtn.textContent = mode === 1 ? 'Select Folder' :
                        mode === 2 ? 'Save' : 'Open';
    okBtn.style.cssText =
      'background:#3b6ef5;color:#fff;border:none;border-radius:6px;' +
      'padding:6px 16px;font-size:12px;cursor:pointer;font-weight:600;';
    bar.appendChild(cancelBtn);
    bar.appendChild(okBtn);
    panel.appendChild(bar);
    overlay.appendChild(panel);
    document.body.appendChild(overlay);

    var curRel = '';
    var selected = null;  // {name, rel, isDir}

    function workspacePath(rel) { return rel || '.'; }
    function validRelative(rel) {
      return typeof rel === 'string' && !/^[\/]/.test(rel) &&
        !/[\\:\0]/.test(rel) && rel.split('/').indexOf('..') === -1;
    }
    function parentRel(rel) {
      if (!rel) return '';
      var i = rel.lastIndexOf('/');
      return i < 0 ? '' : rel.substring(0, i);
    }

    function renderRow(label, isDir, onActivate, isSelected) {
      var row = document.createElement('div');
      row.style.cssText =
        'display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:6px;' +
        'cursor:pointer;font-size:13px;' +
        (isSelected ? 'background:#2a3a6a;' : '');
      row.onmouseenter = function() {
        if (!isSelected) row.style.background = '#2a2a38';
      };
      row.onmouseleave = function() {
        row.style.background = isSelected ? '#2a3a6a' : '';
      };
      var icon = document.createElement('span');
      icon.textContent = isDir ? '📁' : '📄';
      icon.style.cssText = 'width:18px;text-align:center;';
      var txt = document.createElement('span');
      txt.textContent = label;
      txt.style.cssText = 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
      row.appendChild(icon);
      row.appendChild(txt);
      row.onclick = function(ev) {
        ev.stopPropagation();
        onActivate(false);
      };
      row.ondblclick = function(ev) {
        ev.stopPropagation();
        onActivate(true);
      };
      return row;
    }

    function paintList(j) {
      if (j.root !== '.' || !Array.isArray(j.dirs) || !Array.isArray(j.files))
        throw new Error('Invalid workspace listing');
      curRel = (j.dir !== undefined && j.dir !== null) ? j.dir : curRel;
      if (!validRelative(curRel)) throw new Error('Invalid workspace directory');
      crumb.textContent = workspacePath(curRel);
      list.innerHTML = '';

      if (curRel) {
        list.appendChild(renderRow('..', true, function(dbl) {
          if (dbl) {
            selected = null;
            loadDir(parentRel(curRel));
          }
        }, false));
      }

      (j.dirs || []).forEach(function(d) {
        var name = d.name || '';
        var drel = d.path || (curRel ? (curRel + '/' + name) : name);
        if (!validRelative(drel) || !name || /[\/\\:\0]/.test(name) ||
            name === '.' || name === '..') throw new Error('Invalid workspace entry');
        var isSel = !!(selected && selected.rel === drel);
        list.appendChild(renderRow(name, true, function(dbl) {
          if (dbl) {
            selected = null;
            loadDir(drel);
            return;
          }
          selected = {name: name, rel: drel, isDir: true};
          paintList(j);
        }, isSel));
      });

      if (mode !== 1) {
        (j.files || []).forEach(function(f) {
          var name = f.name || '';
          if (!extOk(name)) return;
          var frel = f.path || (curRel ? (curRel + '/' + name) : name);
          if (!validRelative(frel) || !name || /[\/\\:\0]/.test(name) ||
              name === '.' || name === '..') throw new Error('Invalid workspace entry');
          var isSel = !!(selected && selected.rel === frel);
          list.appendChild(renderRow(name, false, function(dbl) {
            selected = {name: name, rel: frel, isDir: false};
            if (mode === 2 && nameInput) nameInput.value = name;
            if (dbl && mode === 0) {
              finish(1, workspacePath(frel));
              return;
            }
            paintList(j);
          }, isSel));
        });
      }

      if (!list.children.length) {
        var empty = document.createElement('div');
        empty.style.cssText = 'padding:12px;color:#888;font-size:12px;';
        empty.textContent = mode === 1 ? 'No subfolders' : 'No matching files';
        list.appendChild(empty);
      }
    }

    function loadDir(rel) {
      if (!validRelative(rel || '')) return;
      list.innerHTML = '';
      var status = document.createElement('div');
      status.style.cssText = 'padding:12px;color:#888;font-size:12px;';
      status.textContent = 'Loading…';
      list.appendChild(status);

      var url = '/workspace/list?dir=' + encodeURIComponent(rel || '');
      fetch(url, {credentials: 'same-origin'})
        .then(function(r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(function(j) {
          selected = null;
          curRel = rel || '';
          paintList(j);
        })
        .catch(function(err) {
          list.innerHTML = '';
          var e = document.createElement('div');
          e.style.cssText = 'padding:12px;color:#f66;font-size:12px;';
          e.textContent = 'Failed to list workspace: ' +
            (err && err.message ? err.message : err);
          list.appendChild(e);
        });
    }

    okBtn.onclick = function() {
      if (mode === 0) {
        if (selected && !selected.isDir) {
          finish(1, workspacePath(selected.rel));
          return;
        }
        return;  // need a file
      }
      if (mode === 1) {
        var folderRel = (selected && selected.isDir && selected.name !== '..')
          ? selected.rel : curRel;
        finish(1, workspacePath(folderRel));
        return;
      }
      // SaveAs
      var fname = nameInput ? (nameInput.value || '').trim() : '';
      if (!fname) return;
      if (!validRelative(fname) || /[\/]/.test(fname) || fname.charAt(0) === '.') return;
      var destRel = curRel ? (curRel + '/' + fname) : fname;
      finish(1, workspacePath(destRel));
    };

    if (uploadBtn) {
      // Open <input type=file> in THIS click handler so the browser keeps the
      // user gesture. Returning "__UPLOAD__" to C++ and opening the picker later
      // (after ASYNCIFY resume) loses the gesture — browsers block input.click()
      // and/or the focus-cancel timer races FileReader, wedging or silently
      // cancelling the Open flow. Fill g_pick via chudware_file_pick_done, then
      // finish browse with "__UPLOAD__" so C++ can consume bytes + fsUpload.
      uploadBtn.onclick = function() {
        if (finished) return;
        var input = document.createElement('input');
        input.type = 'file';
        if (accept && accept.length) input.accept = accept;
        input.style.display = 'none';
        document.body.appendChild(input);

        function showUploadStatus(msg, isErr) {
          list.innerHTML = '';
          var e = document.createElement('div');
          e.style.cssText = 'padding:12px;font-size:12px;color:' +
            (isErr ? '#f66' : '#a0a0b0') + ';';
          e.textContent = msg;
          list.appendChild(e);
        }

        function deliverPick(ok, name, data, len, errStr) {
          if (typeof Module === 'undefined' || !Module._chudware_file_pick_done) {
            showUploadStatus('Upload unavailable (module not ready)', true);
            return false;
          }
          try {
            if (ok && data && len > 0) {
              var ptr = Module._malloc(len);
              if (!ptr) {
                showUploadStatus('Out of memory reading file (' + len + ' bytes)', true);
                return false;
              }
              // Chunked heap write — large .step+ files are multi-MB.
              var heap = Module.HEAPU8;
              var CHUNK = 1 << 20;
              for (var off = 0; off < len; off += CHUNK) {
                var end = off + CHUNK;
                if (end > len) end = len;
                heap.set(data.subarray(off, end), ptr + off);
              }
              var namePtr = 0;
              if (name) {
                var nb = lengthBytesUTF8(name) + 1;
                namePtr = Module._malloc(nb);
                stringToUTF8(name, namePtr, nb);
              }
              Module._chudware_file_pick_done(1, namePtr, ptr, len, 0);
              if (namePtr) Module._free(namePtr);
              Module._free(ptr);
              return true;
            }
            var errPtr = 0;
            if (errStr) {
              var eb = lengthBytesUTF8(errStr) + 1;
              errPtr = Module._malloc(eb);
              stringToUTF8(errStr, errPtr, eb);
            }
            Module._chudware_file_pick_done(0, 0, 0, 0, errPtr);
            if (errPtr) Module._free(errPtr);
            return false;
          } catch (ex) {
            showUploadStatus('Upload failed: ' +
              (ex && ex.message ? ex.message : ex), true);
            return false;
          }
        }

        input.addEventListener('change', function() {
          try { document.body.removeChild(input); } catch (e) {}
          if (!input.files || input.files.length === 0) {
            // Native picker cancelled — keep workspace overlay open.
            return;
          }
          var f = input.files[0];
          showUploadStatus('Reading ' + (f.name || 'file') + '…', false);
          var reader = new FileReader();
          reader.onload = function() {
            try {
              var u8 = new Uint8Array(reader.result);
              if (!u8.length) {
                showUploadStatus('File is empty', true);
                return;
              }
              showUploadStatus('Uploading ' + (f.name || 'file') + '…', false);
              if (deliverPick(1, f.name || 'upload.bin', u8, u8.length, null)) {
                finish(1, '__UPLOAD__');
              }
            } catch (ex) {
              showUploadStatus('Failed to read file: ' +
                (ex && ex.message ? ex.message : ex), true);
            }
          };
          reader.onerror = function() {
            showUploadStatus('FileReader failed', true);
          };
          reader.readAsArrayBuffer(f);
        });
        // No focus-cancel timer here: cancelling the native picker must leave
        // the workspace overlay open so the user can retry or Cancel cleanly.
        input.click();
      };
    }

    // Esc cancels.
    function onKey(ev) {
      if (ev.key === 'Escape') {
        finish(0, null);
      } else if (ev.key === 'Enter') {
        okBtn.onclick();
      }
    }
    window.addEventListener('keydown', onKey);
    var _origFinish = finish;
    finish = function(ok, path) {
      window.removeEventListener('keydown', onKey);
      _origFinish(ok, path);
    };

    loadDir('');
  },

  // Canvas HTML5 file drop → Module._chudware_web_drop_done (app.cpp).
  // Installed once from C++ after GL/canvas is ready. Does not block;
  // FileReader completion posts into the C++ pending slot drained per frame.
  chudware_install_canvas_drop__deps: ['$UTF8ToString', '$stringToUTF8', '$lengthBytesUTF8'],
  chudware_install_canvas_drop: function() {
    if (typeof window === 'undefined' || window.__chudwareDropInstalled) return;
    window.__chudwareDropInstalled = true;

    function canvasEl() {
      return document.getElementById('canvas') ||
             (typeof Module !== 'undefined' && Module['canvas']) ||
             document.querySelector('canvas');
    }

    function finishDrop(ok, name, data, len, errStr) {
      if (typeof Module === 'undefined' || !Module._chudware_web_drop_done) return;
      if (ok && data && len > 0) {
        var ptr = Module._malloc(len);
        HEAPU8.set(data, ptr);
        var namePtr = 0;
        if (name) {
          var nb = lengthBytesUTF8(name) + 1;
          namePtr = Module._malloc(nb);
          stringToUTF8(name, namePtr, nb);
        }
        Module._chudware_web_drop_done(1, namePtr, ptr, len, 0);
        if (namePtr) Module._free(namePtr);
        Module._free(ptr);
      } else {
        var errPtr = 0;
        if (errStr) {
          var eb = lengthBytesUTF8(errStr) + 1;
          errPtr = Module._malloc(eb);
          stringToUTF8(errStr, errPtr, eb);
        }
        Module._chudware_web_drop_done(0, 0, 0, 0, errPtr);
        if (errPtr) Module._free(errPtr);
      }
    }

    function onDragOver(ev) {
      ev.preventDefault();
      try { ev.dataTransfer.dropEffect = 'copy'; } catch (e) {}
    }
    function onDrop(ev) {
      ev.preventDefault();
      ev.stopPropagation();
      var files = (ev.dataTransfer && ev.dataTransfer.files) ? ev.dataTransfer.files : null;
      if (!files || !files.length) {
        finishDrop(0, null, null, 0, 'Drop a CAD or ECAD file to open it');
        return;
      }
      var f = files[0];
      var reader = new FileReader();
      reader.onload = function() {
        var u8 = new Uint8Array(reader.result);
        finishDrop(1, f.name || 'upload.bin', u8, u8.length, null);
      };
      reader.onerror = function() {
        finishDrop(0, null, null, 0, 'FileReader failed');
      };
      reader.readAsArrayBuffer(f);
    }

    function attach() {
      var c = canvasEl();
      if (!c) {
        setTimeout(attach, 200);
        return;
      }
      c.addEventListener('dragover', onDragOver);
      c.addEventListener('drop', onDrop);
      // Also catch drops on the page body so drops outside the GL canvas still work.
      document.addEventListener('dragover', onDragOver);
      document.addEventListener('drop', onDrop);
    }
    attach();
  }
});
