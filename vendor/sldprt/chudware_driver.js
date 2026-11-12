'use strict';
/* Chudware driver for the vendored sldprt converter (MIT, Chudware; not part of upstream).
 *
 *   <js runtime> chudware_driver.js <input> <out.step>
 *
 * Writes the exact native Parasolid body of a SolidWorks part (.sldprt, SolidWorks 2011 OLE2 and
 * 2015+ containers) or a Parasolid transmit file (.x_t text, .x_b neutral binary) as AP214 STEP in
 * millimetres, then prints one JSON line on stdout:
 *
 *   {"ok":true, "step":..., "format":"sldprt"|"x_t"|"x_b", "report":{...}, "saved":{...}}
 *   {"ok":false, "error":"..."}
 *
 * The exit code is 0 only when the STEP was written. Upstream code under src/ is used unmodified
 * (see SLDPRT_ARTIFACT.lock); runs under Node 18+ or Bun (omp with BUN_BE_BUN=1).
 */
const fs = require('fs');
const path = require('path');

function emit(obj, code) {
  process.stdout.write(JSON.stringify(obj) + '\n');
  process.exitCode = code;
}

function main(argv) {
  const [input, output] = argv;
  if (!input || !output) return emit({ ok: false, error: 'usage: chudware_driver.js <input> <out.step>' }, 2);
  const ext = path.extname(input).toLowerCase();
  const bytes = fs.readFileSync(input);
  let text, report, saved = null, format;
  if (ext === '.x_t' || ext === '.x_b') {
    format = ext.slice(1);
    const xt = require('./src/parasolid/xt');
    const stepExport = require('./src/step/export');
    const parsed = xt.parse(bytes, ext === '.x_b');
    if (parsed.error) throw Error('Parasolid ' + format + ' not fully read: ' + parsed.error);
    ({ text, report } = stepExport.toSTEP(parsed, {}));
  } else if (ext === '.sldprt') {
    format = 'sldprt';
    const sldprt = require('./src/index');
    ({ text, report } = sldprt.toSTEP(bytes, { mode: 'exact' }));
    try {
      const info = sldprt.info(bytes);
      const h = (info.saved && info.saved.history) || [];
      const created = h.find(e => e.created) || null;
      saved = {
        version: info.saved ? info.saved.version : null,
        release: info.saved ? info.saved.release : null,
        created: created ? created.release : null,
        container: info.container,
      };
    } catch (_) { /* provenance is informative only */ }
  } else {
    return emit({ ok: false, error: 'unsupported extension ' + ext + ' (expected .sldprt, .x_t or .x_b)' }, 2);
  }
  fs.writeFileSync(output, text);
  emit({ ok: true, step: output, format, report, saved }, 0);
}

try {
  main(process.argv.slice(2));
} catch (e) {
  emit({ ok: false, error: String(e && e.message || e) }, 1);
}
