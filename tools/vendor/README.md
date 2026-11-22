# Vendored front-end libraries

Served by `kg serve` at `/vendor/…` and copied next to `build/graph.html` by `kg build`, so pages work offline and
no CDN can change the code the app runs. Fetched with `npm pack` (registry integrity checked).

| File | Package | Version |
|---|---|---|
| `marked.min.js` | marked (`lib/marked.umd.js`) | 18.1.2 |
| `purify.min.js` | dompurify (`dist/purify.min.js`) | 3.4.17 |
| `vis-network.min.js` | vis-network (`standalone/umd/vis-network.min.js`) | 10.1.4 |

To update: check each project's security advisories, `npm pack <pkg>@<version>`, copy the same file, update this table.
