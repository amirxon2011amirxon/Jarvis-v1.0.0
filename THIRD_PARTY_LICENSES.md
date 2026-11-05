# Third-Party License Inventory

This inventory covers the direct runtime dependencies declared in
`requirements.txt`, the direct frontend dependencies in `frontend/package.json`,
and bundled runtime assets. Transitive dependency licenses are recorded by their
upstream packages and the npm lockfile.

This document is a factual inventory, not legal advice. Jarvis's MIT license
does not cover third-party packages, external services, or assets with separate
terms.

## Python runtime dependencies

Versions below show the declared range and the version inspected in the local
release environment.

| Package | Declared range | Inspected version | License | Attribution / notes | Source |
|---|---:|---:|---|---|---|
| sounddevice | >=0.4,<1 | 0.5.6 | MIT | Preserve copyright/license notice | https://pypi.org/project/sounddevice/ |
| numpy | >=1.24,<3 | 2.5.2 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | Multiple bundled notices; preserve upstream notices | https://pypi.org/project/numpy/ |
| audioop-lts | >=0.2.1; Python >=3.13 | 0.2.2 | PSF-2.0 | Preserve PSF notice | https://pypi.org/project/audioop-lts/ |
| pylibsrtp | >=1.0,<2 | 1.0.0 | BSD-3-Clause | Preserve notice | https://pypi.org/project/pylibsrtp/ |
| google-genai | >=2.8.0,<3 | 2.19.0 | Apache-2.0 | Preserve NOTICE/LICENSE where required | https://pypi.org/project/google-genai/ |
| psutil | >=5.9,<8 | 5.9.8 | BSD-3-Clause | Preserve notice | https://pypi.org/project/psutil/ |
| pyyaml | >=6,<7 | 6.0.3 | MIT | Preserve notice | https://pypi.org/project/PyYAML/ |
| mss | >=10,<11 | 10.2.0 | MIT | Preserve notice | https://pypi.org/project/mss/ |
| pillow | >=11,<13 | 12.3.0 | MIT-CMU | Preserve notice | https://pypi.org/project/pillow/ |
| fastapi | >=0.115,<1 | 0.141.1 | MIT | Preserve notice | https://pypi.org/project/fastapi/ |
| uvicorn | >=0.30,<1 | 0.53.0 | BSD-3-Clause | Preserve notice | https://pypi.org/project/uvicorn/ |
| websockets | >=13,<17 | 16.1.1 | BSD-3-Clause | Preserve notice | https://pypi.org/project/websockets/ |
| python-docx | >=1.1,<2 | 1.2.0 | MIT | Preserve notice | https://pypi.org/project/python-docx/ |
| openpyxl | >=3.1,<4 | 3.1.5 | MIT | Preserve notice | https://pypi.org/project/openpyxl/ |
| python-pptx | >=0.6,<2 | 1.0.2 | MIT | Preserve notice | https://pypi.org/project/python-pptx/ |
| pypdf | >=4,<7 | 6.21.4 | BSD-3-Clause | Preserve notice | https://pypi.org/project/pypdf/ |
| **ebooklib** | >=0.18,<1 | 0.21 | **AGPL-3.0-or-later** | **Redistribution caution: AGPL obligations may apply. Legal review required before public distribution.** | https://pypi.org/project/ebooklib/ |
| **odfpy** | >=1.4,<2 | 1.4.1 | **UNKNOWN exact election** | Metadata classifiers list Apache, GPL and LGPL, but no authoritative license file/election was present. Legal review required. | https://pypi.org/project/odfpy/ |
| striprtf | >=0.0.26,<1 | 0.0.35 | BSD-3-Clause | Preserve notice | https://pypi.org/project/striprtf/ |
| xlrd | >=2.0,<3 | 2.0.2 | BSD (classifier) | Preserve notice | https://pypi.org/project/xlrd/ |
| charset-normalizer | >=3.3,<4 | 3.5.1 | MIT | Preserve notice | https://pypi.org/project/charset-normalizer/ |
| tzlocal | >=5,<6 | 5.4.4 | MIT | Preserve notice | https://pypi.org/project/tzlocal/ |
| httpx | >=0.28,<1 | 0.28.1 | BSD-3-Clause | Preserve notice | https://pypi.org/project/httpx/ |
| beautifulsoup4 | >=4.12,<5 | 4.15.0 | MIT | Preserve notice | https://pypi.org/project/beautifulsoup4/ |
| ddgs | >=9,<10 | 9.15.0 | MIT | Search fallback; preserve notice | https://pypi.org/project/ddgs/ |
| comtypes | >=1.2; Windows | 1.4.16 | MIT | Windows COM bridge; preserve notice | https://pypi.org/project/comtypes/ |
| pycaw | >=20240210; Windows | 20251023 | MIT | Windows volume control; preserve notice | https://pypi.org/project/pycaw/ |

**Direct Python runtime dependencies inspected: 27.**

These packages are installed by pip as separate distributions. The Jarvis wheel
does not vendor their source code.

## Frontend runtime dependencies

| Package | Version | License | Notes |
|---|---:|---|---|
| react | 19.3.1 | MIT | Bundled into the production UI; preserve notice |
| react-dom | 19.3.1 | MIT | Bundled into the production UI; preserve notice |
| three | 0.186.1 | MIT | Bundled into the production UI, including `examples/jsm`; preserve notice |

### Frontend development dependencies

`@tailwindcss/vite` (MIT), `@testing-library/react` (MIT), `@types/node` (MIT),
`@types/react` (MIT), `@types/react-dom` (MIT), `@types/three` (MIT),
`@vitejs/plugin-react` (MIT), `jsdom` (MIT), `tailwindcss` (MIT),
`typescript` (Apache-2.0), `vite` (MIT), and `vitest` (MIT).

They are development-only and are not installed by the Jarvis runtime.

### Lockfile aggregate

`frontend/package-lock.json` contains **193 installed package records**:

- MIT: 132
- Apache-2.0: 26
- MPL-2.0: 24
- ISC: 3
- MIT-0: 2
- BSD-2-Clause: 2
- BSD-3-Clause: 2
- CC0-1.0: 1
- BlueOak-1.0.0: 1

No GPL/AGPL/UNKNOWN-license frontend package was found in the lockfile. MPL-2.0
packages are file-level copyleft; review their notices if their code is ever
distributed outside the build toolchain.

## Bundled assets and external services

| Asset / service | Status | Notes |
|---|---|---|
| Inline SVG icons in `frontend/src/components/ui/Icons.tsx` | Project code, no external attribution found | No copied third-party asset identified. |
| Reactor fallback | Project CSS | Rendered from CSS gradients when WebGL is unavailable; no external image asset is bundled. |
| Fonts | No bundled font files | UI uses system font stacks. |
| YouTube IFrame API/player | External service | Subject to YouTube/Google terms; not bundled and not covered by MIT. |
| Gemini Live / Google AI services | External service | Subject to Google terms; not bundled and not covered by MIT. |

## Redistribution concerns

1. **`ebooklib` is AGPL-3.0-or-later.** Do not state or imply that it is covered
   by Jarvis's MIT license. Obtain legal review before public distribution or
   make an explicit dependency decision.
2. **`odfpy` does not expose an authoritative exact license election** in its
   installed metadata. Its classifiers list Apache/GPL/LGPL; legal review is
   required.
3. **Third-party notices:** When redistributing the built frontend, preserve the
   MIT/Apache notices required by React, React DOM and Three.js. Do not remove
   upstream license notices from dependency distributions.

## Scope of Jarvis's MIT license

The root `LICENSE` applies only to original Jarvis source code and assets whose
ownership is established. It does not relicense third-party dependencies,
external services, or assets with separate terms.
