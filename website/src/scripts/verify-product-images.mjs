#!/usr/bin/env node

import { createHash } from 'node:crypto';
import { readFile, readdir, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SITE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const IMAGE_DIR = path.join(SITE_ROOT, 'public', 'images');
const MANIFEST_PATH = path.join(IMAGE_DIR, 'provenance.json');
const REPO_ROOT = path.resolve(SITE_ROOT, '../..');
const PRODUCT_BINARY_PATH = path.join(REPO_ROOT, 'host', 'build-linux', 'bin', 'chudware');
const PRODUCT_VERSION_PATH = path.join(REPO_ROOT, 'host', 'build-linux', 'generated', 'api', 'chudware_version.h');
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const TOP_LEVEL_KEYS = ['images', 'schemaVersion'];
const IMAGE_KEYS = [
  'captureCommand',
  'chudwareShot',
  'filename',
  'height',
  'productRevision',
  'rawCapture',
  'scenarioStep',
  'sha256',
  'sourceRevision',
  'width',
];

const CRC32_TABLE = Uint32Array.from({ length: 256 }, (_, value) => {
  let crc = value;
  for (let bit = 0; bit < 8; bit += 1) {
    crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
  }
  return crc >>> 0;
});

function pngCrc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc = CRC32_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function checkExactKeys(value, expected, label, errors) {
  if (!isObject(value)) {
    errors.push(`${label} must be an object`);
    return false;
  }

  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  if (actual.join('\0') !== wanted.join('\0')) {
    errors.push(`${label} keys must be exactly: ${wanted.join(', ')}`);
    return false;
  }
  return true;
}

function parseRawCapturePng(bytes, filename, errors) {
  if (bytes.length < 45 || !bytes.subarray(0, 8).equals(PNG_SIGNATURE)) {
    errors.push(`${filename} is not a PNG`);
    return null;
  }

  const chunkTypes = [];
  let offset = 8;
  let width = null;
  let height = null;

  while (offset + 12 <= bytes.length) {
    const length = bytes.readUInt32BE(offset);
    const end = offset + 12 + length;
    if (end > bytes.length) {
      errors.push(`${filename} has a truncated PNG chunk`);
      return null;
    }

    const declaredCrc = bytes.readUInt32BE(offset + 8 + length);
    const actualCrc = pngCrc32(bytes.subarray(offset + 4, offset + 8 + length));
    if (declaredCrc !== actualCrc) {
      errors.push(`${filename} has an invalid PNG chunk checksum`);
    }

    const type = bytes.toString('ascii', offset + 4, offset + 8);
    chunkTypes.push(type);
    if (chunkTypes.length === 1) {
      if (type !== 'IHDR' || length !== 13) {
        errors.push(`${filename} does not begin with a valid IHDR chunk`);
        return null;
      }
      width = bytes.readUInt32BE(offset + 8);
      height = bytes.readUInt32BE(offset + 12);
      const rawRgba8 =
        bytes[offset + 16] === 8 &&
        bytes[offset + 17] === 6 &&
        bytes[offset + 18] === 0 &&
        bytes[offset + 19] === 0 &&
        bytes[offset + 20] === 0;
      if (!rawRgba8) {
        errors.push(`${filename} is not the RGBA8, non-interlaced output produced by the framebuffer capture`);
      }
    }

    offset = end;
    if (type === 'IEND') {
      if (length !== 0) errors.push(`${filename} has an invalid IEND chunk`);
      break;
    }
  }

  const rawChunkShape =
    chunkTypes[0] === 'IHDR' &&
    chunkTypes.at(-1) === 'IEND' &&
    chunkTypes.slice(1, -1).length > 0 &&
    chunkTypes.slice(1, -1).every((type) => type === 'IDAT') &&
    offset === bytes.length;
  if (!rawChunkShape) {
    errors.push(`${filename} contains metadata, trailing data, or a non-capture PNG chunk layout`);
  }

  return { width, height };
}

// A published capture is either written straight to its marketed path by the scenario, or —
// when the scenario mirrors a DIFFERENT step to --ui-screenshot than the one we publish — copied
// byte-for-byte out of that scenario's own scratch output. Both forms name the scenario, the
// current built binary, every environment knob the frame depended on, and the exact
// published path, so the command in the manifest is the command that reproduces the file.
const REPO_RELATIVE_IMAGE_DIR = 'Webservice/public-site/public/images';
const CAPTURE_RUN_PATTERN =
  /^((?:(?:CHUDWARE_[A-Z0-9_]+|XDG_DATA_HOME)=[^\s'"]+ )+)\.\/host\/build-linux\/bin\/chudware --ui-screenshot ([^\s'"]+)$/;
const CAPTURE_COPY_PATTERN = /^cp (scratch\/[A-Za-z0-9._/-]+\.png) ([^\s'"]+)$/;

function parseCaptureCommand(command) {
  if (typeof command !== 'string' || command.trim() !== command || command.includes('  ')) return null;

  const steps = command.split(' && ');
  if (steps.length > 2) return null;

  const run = CAPTURE_RUN_PATTERN.exec(steps[0]);
  if (!run) return null;

  const env = new Map();
  const order = [];
  for (const assignment of run[1].trim().split(' ')) {
    const split = assignment.indexOf('=');
    const key = assignment.slice(0, split);
    if (env.has(key)) return null;
    env.set(key, assignment.slice(split + 1));
    order.push(key);
  }

  let copy = null;
  if (steps.length === 2) {
    const copied = CAPTURE_COPY_PATTERN.exec(steps[1]);
    if (!copied) return null;
    copy = { from: copied[1], to: copied[2] };
  }

  return { env, order, shotPath: run[2], copy };
}

function validSourceRevision(value) {
  return typeof value === 'string' && /^[0-9a-f]{40}$/.test(value) && !/^0+$/.test(value);
}

function validProductRevision(value, sourceRevision) {
  if (typeof value !== 'string' || !validSourceRevision(sourceRevision)) return false;
  const match = /^\d+\.\d+\.\d+\+([0-9a-f]{8,12})(?:\.dirty)?$/.exec(value);
  return Boolean(match && sourceRevision.startsWith(match[1]));
}

async function readBuiltProductRevision() {
  let binary;
  let header;
  try {
    [binary, header] = await Promise.all([
      stat(PRODUCT_BINARY_PATH),
      readFile(PRODUCT_VERSION_PATH, 'utf8'),
    ]);
  } catch (error) {
    throw new Error(`cannot verify the current built Chudware binary: ${error.message}`);
  }
  if (!binary.isFile() || (binary.mode & 0o111) === 0) {
    throw new Error('host/build-linux/bin/chudware is not an executable file');
  }

  const version = header.match(/^#define\s+CHUDWARE_VERSION\s+"([^"]+)"/m)?.[1];
  const sourcePrefix = header.match(/^#define\s+CHUDWARE_GIT_SHA\s+"([0-9a-f]{8,12})"/m)?.[1];
  const dirty = header.match(/^#define\s+CHUDWARE_BUILD_DIRTY\s+([01])/m)?.[1];
  if (!version || !sourcePrefix || dirty === undefined) {
    throw new Error('current built Chudware binary has incomplete revision metadata');
  }

  return {
    productRevision: `${version}+${sourcePrefix}${dirty === '1' ? '.dirty' : ''}`,
    sourcePrefix,
  };
}

async function main() {
  const errors = [];
  let manifest;

  try {
    manifest = JSON.parse(await readFile(MANIFEST_PATH, 'utf8'));
  } catch (error) {
    throw new Error(`cannot read ${path.relative(SITE_ROOT, MANIFEST_PATH)}: ${error.message}`);
  }

  const builtProduct = await readBuiltProductRevision();

  const topLevelValid = checkExactKeys(manifest, TOP_LEVEL_KEYS, 'manifest', errors);
  if (topLevelValid && manifest.schemaVersion !== 1) {
    errors.push('manifest.schemaVersion must be 1');
  }
  if (!Array.isArray(manifest?.images) || manifest.images.length === 0) {
    errors.push('manifest.images must be a non-empty array');
  }

  const entries = Array.isArray(manifest?.images) ? manifest.images : [];
  const filenames = [];
  const manifestHashes = new Map();
  const productRevisions = new Set();
  const sourceRevisions = new Set();
  const copySources = new Map();

  for (const [index, entry] of entries.entries()) {
    const label = `manifest.images[${index}]`;
    if (!checkExactKeys(entry, IMAGE_KEYS, label, errors)) continue;

    if (typeof entry.filename !== 'string' || !/^[a-z0-9][a-z0-9-]*\.png$/.test(entry.filename)) {
      errors.push(`${label}.filename must be a lowercase PNG basename`);
    } else {
      filenames.push(entry.filename);
    }

    if (typeof entry.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(entry.sha256)) {
      errors.push(`${label}.sha256 must be a lowercase SHA-256 digest`);
    } else if (manifestHashes.has(entry.sha256)) {
      errors.push(`${entry.filename} duplicates the hash declared for ${manifestHashes.get(entry.sha256)}`);
    } else {
      manifestHashes.set(entry.sha256, entry.filename);
    }

    if (
      !Number.isInteger(entry.width) ||
      entry.width <= 0 ||
      !Number.isInteger(entry.height) ||
      entry.height <= 0
    ) {
      errors.push(`${entry.filename} must declare positive integer width and height`);
    }
    if (typeof entry.chudwareShot !== 'string' || !/^[a-z0-9][a-z0-9_-]*$/.test(entry.chudwareShot)) {
      errors.push(`${entry.filename} has an invalid CHUDWARE_SHOT scenario`);
    }
    if (
      typeof entry.scenarioStep !== 'string' ||
      entry.scenarioStep.trim() !== entry.scenarioStep ||
      entry.scenarioStep.length < 3 ||
      /^(?:unknown|placeholder|tbd|todo)$/i.test(entry.scenarioStep)
    ) {
      errors.push(`${entry.filename} has an invalid or placeholder scenario step`);
    }
    if (entry.rawCapture !== true) {
      errors.push(`${entry.filename} must declare rawCapture: true`);
    }

    const publishedPath = `${REPO_RELATIVE_IMAGE_DIR}/${entry.filename}`;
    const capture = parseCaptureCommand(entry.captureCommand);
    if (!capture) {
      errors.push(
        `${entry.filename} captureCommand must be CHUDWARE_SHOT=… [CHUDWARE_*=… …] [XDG_DATA_HOME=…] ` +
          './host/build-linux/bin/chudware --ui-screenshot <path>, optionally followed by ' +
          '"&& cp scratch/<step>.png <published image>"',
      );
    } else if (
      capture.order[0] !== 'CHUDWARE_SHOT' ||
      capture.env.get('CHUDWARE_SHOT') !== entry.chudwareShot
    ) {
      errors.push(`${entry.filename} captureCommand must lead with CHUDWARE_SHOT=${entry.chudwareShot}`);
    } else if (!capture.copy) {
      if (capture.shotPath !== publishedPath) {
        errors.push(`${entry.filename} captureCommand must write --ui-screenshot straight to ${publishedPath}`);
      }
    } else if (capture.copy.to !== publishedPath || capture.shotPath === publishedPath) {
      errors.push(
        `${entry.filename} captureCommand must copy the published step to ${publishedPath} and leave ` +
          "--ui-screenshot pointed at the step the scenario itself mirrors",
      );
    } else {
      copySources.set(entry.filename, capture.copy.from);
    }

    if (!validSourceRevision(entry.sourceRevision)) {
      errors.push(`${entry.filename} has an invalid or placeholder sourceRevision`);
    } else {
      sourceRevisions.add(entry.sourceRevision);
    }
    if (!validProductRevision(entry.productRevision, entry.sourceRevision)) {
      errors.push(`${entry.filename} productRevision must identify the product version and source commit`);
    } else {
      productRevisions.add(entry.productRevision);
    }
    if (
      validSourceRevision(entry.sourceRevision) &&
      !entry.sourceRevision.startsWith(builtProduct.sourcePrefix)
    ) {
      errors.push(`${entry.filename} sourceRevision is stale relative to the current built binary`);
    }
    if (
      validProductRevision(entry.productRevision, entry.sourceRevision) &&
      entry.productRevision !== builtProduct.productRevision
    ) {
      errors.push(`${entry.filename} productRevision is stale relative to the current built binary`);
    }
  }

  const uniqueFilenames = new Set(filenames);
  if (uniqueFilenames.size !== filenames.length) errors.push('manifest contains duplicate filenames');
  if (filenames.join('\0') !== [...filenames].sort().join('\0')) {
    errors.push('manifest images must be sorted by filename');
  }
  if (productRevisions.size > 1 || sourceRevisions.size > 1) {
    errors.push('all product images must come from one product/source revision');
  }

  const dirents = await readdir(IMAGE_DIR, { withFileTypes: true });
  const pngDirents = dirents.filter((entry) => /\.png$/i.test(entry.name));
  const diskNames = pngDirents.map((entry) => entry.name).sort();
  const manifestNames = [...uniqueFilenames].sort();
  for (const filename of manifestNames.filter((name) => !diskNames.includes(name))) {
    errors.push(`missing marketed product image: ${filename}`);
  }
  for (const filename of diskNames.filter((name) => !uniqueFilenames.has(name))) {
    errors.push(`unmanifested product PNG: ${filename}`);
  }

  const actualHashes = new Map();
  for (const entry of entries) {
    if (!isObject(entry) || !uniqueFilenames.has(entry.filename)) continue;
    const dirent = pngDirents.find((candidate) => candidate.name === entry.filename);
    if (!dirent) continue;
    if (!dirent.isFile()) {
      errors.push(`${entry.filename} must be a regular file, not a symlink or directory`);
      continue;
    }

    const bytes = await readFile(path.join(IMAGE_DIR, entry.filename));
    const dimensions = parseRawCapturePng(bytes, entry.filename, errors);
    if (dimensions && (dimensions.width !== entry.width || dimensions.height !== entry.height)) {
      errors.push(
        `${entry.filename} is ${dimensions.width}x${dimensions.height}; ` +
          `manifest declares ${entry.width}x${entry.height}`,
      );
    }
    const digest = createHash('sha256').update(bytes).digest('hex');
    if (digest !== entry.sha256) {
      errors.push(`${entry.filename} SHA-256 mismatch; recapture it and refresh provenance.json`);
    }

    const copySource = copySources.get(entry.filename);
    if (copySource) {
      // The scenario's scratch output is transient, so a missing step file is not a failure —
      // but when it is still there the published bytes must be exactly that step.
      let stepBytes = null;
      try {
        stepBytes = await readFile(path.join(REPO_ROOT, copySource));
      } catch {
        stepBytes = null;
      }
      if (stepBytes && !stepBytes.equals(bytes)) {
        errors.push(`${entry.filename} does not match the bytes of ${copySource} named in its captureCommand`);
      }
    }
    if (actualHashes.has(digest)) {
      errors.push(`${entry.filename} duplicates the bytes of ${actualHashes.get(digest)}`);
    } else {
      actualHashes.set(digest, entry.filename);
    }
  }

  if (errors.length > 0) {
    console.error('Product image verification failed:');
    for (const error of errors) console.error(`- ${error}`);
    process.exitCode = 1;
    return;
  }

  console.log(`Verified ${entries.length} raw product PNGs against public/images/provenance.json.`);
}

main().catch((error) => {
  console.error(`Product image verification failed: ${error.message}`);
  process.exitCode = 1;
});
