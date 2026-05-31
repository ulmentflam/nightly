# Vendored dashboard assets

These files are committed directly into the repo so `vault build` never
needs network egress and the dashboard opens cleanly from `file://`.
Refresh by running the curl commands below and updating the SHA-256
hashes.

| File | Source | SHA-256 |
|---|---|---|
| `cytoscape.min.js` | https://unpkg.com/cytoscape@3.30.0/dist/cytoscape.min.js | `a298b253e4b9b08bd0b2fe222ad67b2b9f42d057d7c17f7a050512079c46fddd` |
| `sql-wasm.js` | https://unpkg.com/sql.js@1.10.3/dist/sql-wasm.js | `558a72c3ab3415d0e6d243cfd23f9d61543600d59054b4b7b8da3cd65f6b9fd4` |
| `sql-wasm.wasm` | https://unpkg.com/sql.js@1.10.3/dist/sql-wasm.wasm | `d7e61b828523001f26ce0b3f88dabcf6c12e5e6edf80eb4f08b26ac7b946ff88` |

## Why these versions

- **cytoscape 3.30.0** — the v3 line is the long-term stable branch; 3.30
  is the most recent at the time of vendoring. v4 is on the roadmap but
  ships breaking changes.
- **sql.js 1.10.3** — actively maintained, ships both the `sql-wasm.js`
  loader and the `sql-wasm.wasm` binary. The dashboard build step
  base64-inlines the wasm into a sibling `sql-wasm-inline.js` so the
  dashboard works from `file://` without a server (browsers refuse to
  `fetch()` cross-origin wasm from `file://`).

## Refresh procedure

```bash
cd packages/nightly-core/src/nightly_core/vault/assets

curl -sSfL -o cytoscape.min.js https://unpkg.com/cytoscape@3.30.0/dist/cytoscape.min.js
curl -sSfL -o sql-wasm.js      https://unpkg.com/sql.js@1.10.3/dist/sql-wasm.js
curl -sSfL -o sql-wasm.wasm    https://unpkg.com/sql.js@1.10.3/dist/sql-wasm.wasm

shasum -a 256 cytoscape.min.js sql-wasm.js sql-wasm.wasm
```

Update the table above with the new SHAs and bump version numbers in
the Source column.
