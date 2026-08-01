"""Offline decryption tool — generates a self-contained HTML decryptor.

Even if the DoctorAgent project disappears, users must still be able to decrypt
their vault files.  :func:`generate_offline_decryptor` produces a single HTML
file with no external dependencies that runs entirely in the browser using
the Web Crypto API (``window.crypto.subtle``).  The user opens the file,
enters their master key (hex or base64), drops one or more encrypted vault
files onto the page, and downloads the decrypted originals.

Security model
--------------

* The HTML embeds only the **SHA-256 hash** of the master key, never the key
  itself.  The hash is used to tell the user "wrong key" *before* attempting
  decryption, but it cannot be reversed to recover the key.
* All crypto runs locally in the browser.  No network requests are made.
* Both on-disk formats are supported:

  - **v1 (whole-file)** — ``[0x01][32B salt][12B nonce][ciphertext+tag]``
    with AAD ``0x01 + salt``.
  - **v2 (streaming)** — ``[0x02][32B salt]`` followed by length-prefixed
    chunks ``[4B len][12B iv][ciphertext+tag]`` with a final empty sentinel
    chunk.  Each chunk's AAD binds ``0x02 + salt + 8B big-endian index``.

Key derivation (mirrors :mod:`doctoragent.security.keytree`)
--------------------------------------------------------

1. master_key → HKDF-SHA256(salt=∅, info=``vault-key-v1``) → vault_key
2. vault_key + per-file salt → HKDF-SHA256(salt=salt, info=``doctoragent-file-key-v1``)
   → file_key
3. file_key + nonce/iv → AES-256-GCM decrypt → plaintext
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# The HTML template is kept as a module-level constant so it can be inspected
# and unit-tested without writing to disk.
_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DoctorAgent Offline Decryptor</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0f1117; color: #e0e0e0; min-height: 100vh;
    display: flex; flex-direction: column; align-items: center; padding: 2rem;
  }
  .container { max-width: 720px; width: 100%; }
  h1 { text-align: center; margin-bottom: 0.3rem; font-size: 1.6rem; color: #58a6ff; }
  .subtitle { text-align: center; color: #8b949e; margin-bottom: 2rem; font-size: 0.9rem; }
  .card {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 1.5rem; margin-bottom: 1.2rem;
  }
  label { display: block; font-size: 0.85rem; color: #8b949e; margin-bottom: 0.4rem; }
  input[type="text"], input[type="password"] {
    width: 100%; padding: 0.7rem; background: #0d1117; border: 1px solid #30363d;
    border-radius: 6px; color: #e0e0e0; font-size: 0.9rem; font-family: monospace;
  }
  input:focus { outline: none; border-color: #58a6ff; }
  .key-status { font-size: 0.8rem; margin-top: 0.5rem; min-height: 1.2rem; }
  .key-status.ok { color: #3fb950; }
  .key-status.err { color: #f85149; }
  .drop-zone {
    border: 2px dashed #30363d; border-radius: 8px; padding: 3rem 2rem;
    text-align: center; cursor: pointer; transition: border-color 0.2s, background 0.2s;
  }
  .drop-zone:hover, .drop-zone.dragover {
    border-color: #58a6ff; background: rgba(88,166,255,0.05);
  }
  .drop-zone p { color: #8b949e; font-size: 0.9rem; }
  .drop-zone .icon { font-size: 2.5rem; margin-bottom: 0.5rem; }
  #file-input { display: none; }
  .file-list { margin-top: 1rem; }
  .file-item {
    display: flex; align-items: center; justify-content: space-between;
    padding: 0.6rem 0.8rem; background: #0d1117; border: 1px solid #30363d;
    border-radius: 6px; margin-bottom: 0.5rem; font-size: 0.85rem;
  }
  .file-item .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .file-item .status { margin-left: 0.8rem; font-weight: 600; white-space: nowrap; }
  .file-item .status.done { color: #3fb950; }
  .file-item .status.error { color: #f85149; }
  .file-item .status.processing { color: #d29922; }
  .file-item .status.pending { color: #8b949e; }
  .btn {
    display: inline-block; padding: 0.6rem 1.5rem; background: #238636; color: #fff;
    border: none; border-radius: 6px; font-size: 0.9rem; cursor: pointer;
    text-decoration: none; transition: background 0.2s;
  }
  .btn:hover { background: #2ea043; }
  .btn:disabled { background: #21262d; color: #484f58; cursor: not-allowed; }
  .btn-secondary { background: #30363d; }
  .btn-secondary:hover { background: #3d444d; }
  .actions { display: flex; gap: 0.8rem; margin-top: 1rem; }
  .info {
    background: #0d1117; border-left: 3px solid #58a6ff; padding: 0.8rem 1rem;
    border-radius: 4px; margin-top: 1rem; font-size: 0.8rem; color: #8b949e; line-height: 1.5;
  }
  .info code { background: #161b22; padding: 0.1rem 0.3rem; border-radius: 3px; color: #e0e0e0; }
  .footer { text-align: center; margin-top: 2rem; color: #484f58; font-size: 0.75rem; }
</style>
</head>
<body>
<div class="container">
  <h1>DoctorAgent Offline Decryptor</h1>
  <p class="subtitle">Decrypt your vault files without any server &mdash; everything runs in your browser.</p>

  <div class="card">
    <label for="key-input">Master Key (hex or base64, 32 bytes)</label>
    <input type="password" id="key-input" placeholder="e.g. a1b2c3d4... (64 hex chars)" autocomplete="off">
    <div class="key-status" id="key-status"></div>
    <div class="actions">
      <button class="btn btn-secondary" id="verify-btn" disabled>Verify Key</button>
      <button class="btn" id="decrypt-all-btn" disabled>Decrypt All Files</button>
    </div>
  </div>

  <div class="card">
    <div class="drop-zone" id="drop-zone">
      <div class="icon">&#128274;</div>
      <p>Drag &amp; drop encrypted files here, or click to select</p>
    </div>
    <input type="file" id="file-input" multiple>
    <div class="file-list" id="file-list"></div>
  </div>

  <div class="info">
    <strong>How it works:</strong> Enter your 32-byte master key as a hex string (64 characters)
    or base64 string. The tool verifies it against a stored hash, then derives per-file keys
    using HKDF-SHA256 and decrypts with AES-256-GCM via the Web Crypto API.
    <br><br>
    <strong>Supported formats:</strong> DoctorAgent v1 (whole-file) and v2 (streaming chunked).
    <br><br>
    <strong>Privacy:</strong> No data leaves your browser. The page contains only a hash of
    your key &mdash; never the key itself.
  </div>

  <div class="footer">
    Generated by DoctorAgent. AES-256-GCM &middot; HKDF-SHA256 &middot; Web Crypto API.
  </div>
</div>

<script>
// ── Embedded key hash (SHA-256 of the master key, hex) ──────────────────
const MASTER_KEY_HASH = "__MASTER_KEY_HASH__";

// ── Wire-format constants (must match doctoragent/security/crypto.py) ────────
const VERSION_V1 = 0x01;
const VERSION_V2 = 0x02;
const SALT_LEN = 32;
const NONCE_LEN = 12;
const TAG_LEN = 16;
const CHUNK_HEADER_LEN = 4;

// ── State ────────────────────────────────────────────────────────────────
let verifiedVaultKey = null;  // ArrayBuffer once key is verified
let pendingFiles = [];        // [{file, status, plaintext, error}]

// ── Helpers ──────────────────────────────────────────────────────────────

function hexToBytes(hex) {
  hex = hex.trim().replace(/\s+/g, '');
  if (hex.length % 2 !== 0) throw new Error('Hex string has odd length');
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < hex.length; i += 2) {
    bytes[i / 2] = parseInt(hex.substr(i, 2), 16);
  }
  return bytes;
}

function base64ToBytes(b64) {
  b64 = b64.trim().replace(/\s+/g, '');
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function bytesToHex(bytes) {
  return Array.from(bytes).map(b => b.toString(16).padStart(2, '0')).join('');
}

async function sha256Hex(data) {
  const buf = data instanceof Uint8Array ? data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength) : data;
  const hash = await crypto.subtle.digest('SHA-256', buf);
  return bytesToHex(new Uint8Array(hash));
}

async function hkdfDerive(masterKeyBytes, salt, info, length) {
  const keyMaterial = await crypto.subtle.importKey(
    'raw', masterKeyBytes, 'HKDF', false, ['deriveBits']
  );
  const params = {
    name: 'HKDF',
    hash: 'SHA-256',
    salt: salt,
    info: info,
  };
  const bits = await crypto.subtle.deriveBits(params, keyMaterial, length * 8);
  return new Uint8Array(bits);
}

async function deriveVaultKey(masterKeyBytes) {
  const salt = new Uint8Array(0);  // empty salt for vault-key derivation
  const info = new TextEncoder().encode('vault-key-v1');
  return hkdfDerive(masterKeyBytes, salt, info, 32);
}

async function deriveFileKey(vaultKeyBytes, fileSalt) {
  const info = new TextEncoder().encode('doctoragent-file-key-v1');
  return hkdfDerive(vaultKeyBytes, fileSalt, info, 32);
}

async function aesGcmDecrypt(keyBytes, iv, ciphertextWithTag, aad) {
  const key = await crypto.subtle.importKey('raw', keyBytes, 'AES-GCM', false, ['decrypt']);
  const params = {
    name: 'AES-GCM',
    iv: iv,
    additionalData: aad,
    tagLength: 128,
  };
  // Web Crypto expects ciphertext + tag concatenated (same as Python's AESGCM).
  const plaintext = await crypto.subtle.decrypt(params, key, ciphertextWithTag);
  return new Uint8Array(plaintext);
}

// ── v1 decryption (whole-file) ───────────────────────────────────────────

async function decryptV1(data, vaultKey) {
  // [1B version][32B salt][12B nonce][ciphertext+tag]
  const salt = data.slice(1, 1 + SALT_LEN);
  const nonce = data.slice(1 + SALT_LEN, 1 + SALT_LEN + NONCE_LEN);
  const ctWithTag = data.slice(1 + SALT_LEN + NONCE_LEN);
  // AAD = version byte + salt
  const aad = new Uint8Array(1 + SALT_LEN);
  aad[0] = VERSION_V1;
  aad.set(salt, 1);

  const fileKey = await deriveFileKey(vaultKey, salt);
  return aesGcmDecrypt(fileKey, nonce, ctWithTag, aad);
}

// ── v2 decryption (streaming chunked) ────────────────────────────────────

async function decryptV2(data, vaultKey) {
  // [1B version][32B salt] then chunks of [4B len][12B iv][ct+tag]
  const salt = data.slice(1, 1 + SALT_LEN);
  const fileKey = await deriveFileKey(vaultKey, salt);

  let offset = 1 + SALT_LEN;
  const chunks = [];
  let index = 0;

  while (offset < data.length) {
    if (offset + CHUNK_HEADER_LEN > data.length) {
      throw new Error('Truncated stream: missing chunk header');
    }
    // 4-byte big-endian envelope length
    const envelopeLen = (data[offset] << 24) | (data[offset + 1] << 16) |
                        (data[offset + 2] << 8) | data[offset + 3];
    offset += CHUNK_HEADER_LEN;

    if (envelopeLen < NONCE_LEN + TAG_LEN) {
      throw new Error('Invalid chunk: envelope too short');
    }
    if (offset + envelopeLen > data.length) {
      throw new Error('Truncated stream: incomplete chunk body');
    }

    const iv = data.slice(offset, offset + NONCE_LEN);
    const ctWithTag = data.slice(offset + NONCE_LEN, offset + envelopeLen);
    offset += envelopeLen;

    // AAD = version + salt + 8-byte big-endian index
    const aad = new Uint8Array(1 + SALT_LEN + 8);
    aad[0] = VERSION_V2;
    aad.set(salt, 1);
    const dv = new DataView(aad.buffer, 1 + SALT_LEN, 8);
    dv.setUint32(0, Math.floor(index / 0x100000000));
    dv.setUint32(4, index >>> 0);

    const plaintext = await aesGcmDecrypt(fileKey, iv, ctWithTag, aad);

    if (plaintext.length === 0) {
      // Sentinel chunk: end of stream.
      break;
    }
    chunks.push(plaintext);
    index++;
  }

  if (chunks.length === 0) {
    return new Uint8Array(0);
  }
  // Concatenate all chunks into a single buffer.
  const totalLen = chunks.reduce((sum, c) => sum + c.length, 0);
  const result = new Uint8Array(totalLen);
  let pos = 0;
  for (const chunk of chunks) {
    result.set(chunk, pos);
    pos += chunk.length;
  }
  return result;
}

// ── Dispatch ─────────────────────────────────────────────────────────────

async function decryptFile(file, vaultKey) {
  const arrayBuffer = await file.arrayBuffer();
  const data = new Uint8Array(arrayBuffer);

  if (data.length < 1) {
    throw new Error('File is empty');
  }

  const version = data[0];
  if (version === VERSION_V1) {
    return decryptV1(data, vaultKey);
  }
  if (version === VERSION_V2) {
    return decryptV2(data, vaultKey);
  }
  throw new Error('Unknown format version: 0x' + version.toString(16).padStart(2, '0'));
}

// ── UI logic ─────────────────────────────────────────────────────────────

const keyInput = document.getElementById('key-input');
const keyStatus = document.getElementById('key-status');
const verifyBtn = document.getElementById('verify-btn');
const decryptAllBtn = document.getElementById('decrypt-all-btn');
const dropZone = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');
const fileList = document.getElementById('file-list');

keyInput.addEventListener('input', () => {
  verifiedVaultKey = null;
  keyStatus.textContent = '';
  keyStatus.className = 'key-status';
  verifyBtn.disabled = keyInput.value.trim().length === 0;
  decryptAllBtn.disabled = true;
});

verifyBtn.addEventListener('click', async () => {
  const raw = keyInput.value.trim();
  if (!raw) return;
  keyStatus.textContent = 'Verifying...';
  keyStatus.className = 'key-status';

  let masterKeyBytes;
  try {
    // Try hex first (64 chars = 32 bytes), then base64.
    if (/^[0-9a-fA-F]{64}$/.test(raw.replace(/\s+/g, ''))) {
      masterKeyBytes = hexToBytes(raw);
    } else {
      masterKeyBytes = base64ToBytes(raw);
    }
  } catch (e) {
    keyStatus.textContent = 'Invalid key format: ' + e.message;
    keyStatus.className = 'key-status err';
    return;
  }

  if (masterKeyBytes.length !== 32) {
    keyStatus.textContent = 'Key must be 32 bytes, got ' + masterKeyBytes.length;
    keyStatus.className = 'key-status err';
    return;
  }

  try {
    const hashHex = await sha256Hex(masterKeyBytes);
    if (hashHex.toLowerCase() === MASTER_KEY_HASH.toLowerCase()) {
      verifiedVaultKey = await deriveVaultKey(masterKeyBytes);
      keyStatus.textContent = 'Key verified successfully.';
      keyStatus.className = 'key-status ok';
      decryptAllBtn.disabled = pendingFiles.length === 0;
    } else {
      keyStatus.textContent = 'Key hash does not match. Wrong key.';
      keyStatus.className = 'key-status err';
    }
  } catch (e) {
    keyStatus.textContent = 'Verification failed: ' + e.message;
    keyStatus.className = 'key-status err';
  }
});

// Drag & drop
dropZone.addEventListener('click', () => fileInput.click());
dropZone.addEventListener('dragover', (e) => {
  e.preventDefault();
  dropZone.classList.add('dragover');
});
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop', (e) => {
  e.preventDefault();
  dropZone.classList.remove('dragover');
  handleFiles(e.dataTransfer.files);
});
fileInput.addEventListener('change', (e) => handleFiles(e.target.files));

function handleFiles(fileObjs) {
  for (const file of fileObjs) {
    pendingFiles.push({ file: file, status: 'pending', plaintext: null, error: null });
  }
  renderFileList();
  if (verifiedVaultKey) decryptAllBtn.disabled = false;
}

function renderFileList() {
  fileList.innerHTML = '';
  pendingFiles.forEach((item, idx) => {
    const div = document.createElement('div');
    div.className = 'file-item';

    const nameSpan = document.createElement('span');
    nameSpan.className = 'name';
    nameSpan.textContent = item.file.name;

    const statusSpan = document.createElement('span');
    const labels = {
      pending: 'Pending',
      processing: 'Decrypting...',
      done: 'Done',
      error: 'Error',
    };
    statusSpan.className = 'status ' + item.status;
    statusSpan.textContent = labels[item.status] || item.status;

    div.appendChild(nameSpan);

    if (item.status === 'done') {
      const btn = document.createElement('a');
      btn.className = 'btn btn-secondary';
      btn.textContent = 'Download';
      btn.href = URL.createObjectURL(new Blob([item.plaintext]));
      btn.download = item.file.name.replace(/\.[^.]+$/, '') + '.decrypted';
      div.appendChild(btn);
    }
    div.appendChild(statusSpan);
    fileList.appendChild(div);
  });
}

decryptAllBtn.addEventListener('click', async () => {
  if (!verifiedVaultKey) return;
  decryptAllBtn.disabled = true;

  for (let i = 0; i < pendingFiles.length; i++) {
    if (pendingFiles[i].status === 'done') continue;
    pendingFiles[i].status = 'processing';
    renderFileList();
    try {
      const plaintext = await decryptFile(pendingFiles[i].file, verifiedVaultKey);
      pendingFiles[i].plaintext = plaintext;
      pendingFiles[i].status = 'done';
    } catch (e) {
      pendingFiles[i].error = e.message;
      pendingFiles[i].status = 'error';
      pendingFiles[i].plaintext = null;
    }
    renderFileList();
  }
  decryptAllBtn.disabled = false;
});
</script>
</body>
</html>"""


def compute_master_key_hash(master_key: bytes) -> str:
    """Compute the SHA-256 hash of *master_key* as a lowercase hex string.

    The hash is embedded in the generated HTML so the browser can verify the
    user-supplied key *before* attempting any decryption.  Only the hash is
    stored — the master key itself never appears in the HTML.
    """
    if master_key is None or len(master_key) != 32:
        raise ValueError("master_key must be 32 bytes")
    return hashlib.sha256(master_key).hexdigest()


def generate_offline_decryptor(
    master_key_hash: str,
    output_path: Path | None = None,
) -> Path:
    """Generate a self-contained HTML decryptor page.

    Parameters
    ----------
    master_key_hash:
        SHA-256 hash of the master key as a hex string (64 chars).
        Use :func:`compute_master_key_hash` to compute it, or pass the
        hash directly if you already have it.
    output_path:
        Where to write the HTML file.  When ``None`` the file is written
        to the current directory as ``doctoragent_decryptor.html``.

    Returns
    -------
    The :class:`Path` to the generated HTML file.

    The generated file has **zero external dependencies**: no CDN links,
    no script tags, no network calls.  It can be opened directly in any
    modern browser (Chrome 63+, Firefox 57+, Safari 11.1+, Edge 79+)
    that supports the Web Crypto API.
    """
    if not master_key_hash or not isinstance(master_key_hash, str):
        raise ValueError("master_key_hash must be a non-empty hex string")
    # Normalise to lowercase hex.
    clean_hash = master_key_hash.strip().lower()
    if len(clean_hash) != 64 or not all(c in "0123456789abcdef" for c in clean_hash):
        raise ValueError("master_key_hash must be a 64-character hex string (SHA-256)")

    html = _HTML_TEMPLATE.replace("__MASTER_KEY_HASH__", clean_hash)

    if output_path is None:
        output_path = Path("doctoragent_decryptor.html")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    logger.info("Offline decryptor generated at %s", output_path)
    return output_path


__all__ = [
    "compute_master_key_hash",
    "generate_offline_decryptor",
]
