# Dependency licenses

One-off scan before the Apache-2.0 release, 2026-10-27.
Question: does anything we ship or depend on conflict with Apache-2.0?

## Verdict

**No conflicts.** Every runtime dependency (Python, npm, Swift) is permissive:
MIT, BSD, ISC, Apache-2.0, PSF, MIT-0, or a dual license that includes one of
those. Nothing is GPL, AGPL, LGPL or SSPL, nothing is missing a license, and
no package is custom.

Flagged, none blocking:

| Item | License | How it is used | Verdict |
|---|---|---|---|
| `certifi`, `py-vapid`, `pywebpush` (Python) | MPL-2.0 | Installed from PyPI, unmodified, imported at runtime | Fine. MPL is file-level copyleft: it binds only changes to those files. We vendor nothing and patch nothing. Don't copy their source into the repo. |
| `lightningcss` and its platform packages (npm) | MPL-2.0 | Dev dependency of the Vite build (CSS minifier); not in the shipped JS or CSS | Fine. Build tool only. |
| PyAV `av` (optional voice extra) | BSD-3-Clause for the Python code | Installed on a box by `deploy/install-voice.sh` | Not installed here, so not verified. Its wheels bundle FFmpeg binaries, which carry their own (L)GPL terms. The box pulls them from PyPI and the repo redistributes nothing, so Apache is unaffected. Look at the wheel's bundled license before ever shipping a prebuilt image or bundle. |
| Parakeet speech model (voice extra) | Apache-2.0 (Mike, 2026-10-27) | Downloaded onto the box by `install-voice.sh`; not in the repo | Permissive. Not checked against the model card (offline). |
| GeoNames place data | CC BY 4.0 | `server/data/places.tsv.gz`, bundled | Compatible with Apache-2.0 for data. Attribution is in `NOTICE` and `server/data/NOTICE`. |

Things to do before a binary ships (none apply to a source repo):

- An App Store build links FluidAudio (Apache-2.0). If FluidAudio has a
  `NOTICE` file, the app needs the matching acknowledgements screen. Not
  checked offline.
- A hosted Funnel build or Docker image that bundles PyAV would need the FFmpeg
  point above settled.

## Python (installed in the box's venv)

Runtime = `server/requirements.txt` and its transitive closure, read from the
installed metadata (`importlib.metadata`). 40 packages.

| License | Packages |
|---|---|
| MIT | annotated-doc 0.0.4, annotated-types 0.7.0, anyio 4.14.2, attrs 26.1.0, charset-normalizer 3.5.2, fastapi 0.139.2, h11 0.16.0, h2 4.4.0, hpack 4.2.0, http_ece 1.2.1, httptools 0.8.0, hyperframe 6.1.0, pydantic 2.13.4, pydantic_core 2.46.4, PyJWT 2.13.0, PyYAML 6.0.3, typing-inspection 0.4.2, urllib3 2.8.0, watchfiles 1.2.0 |
| MIT-0 | cffi 2.1.0 |
| BSD-3-Clause | click 8.4.2, idna 3.18, pycparser 3.0, python-dotenv 1.2.2, starlette 1.3.1, uvicorn 0.51.0, websockets 16.1.1 |
| Apache-2.0 | aioapns 4.0, aiosignal 1.4.0, frozenlist 1.8.0, multidict 6.9.1, propcache 0.5.4, pyOpenSSL 26.3.0, requests 2.34.2, yarl 1.25.1 |
| Apache-2.0 AND MIT | aiohttp 3.14.3 |
| Apache-2.0 OR BSD-3-Clause | cryptography 49.0.0 |
| MIT or Apache-2.0 (uvloop's metadata lists both) | uvloop 0.22.1 |
| PSF-2.0 | aiohappyeyeballs 2.7.1, typing_extensions 4.16.0 |
| **MPL-2.0** | certifi 2026.6.17, py-vapid 1.9.4, pywebpush 2.5.0 |

Not in `requirements.txt`, present in the dev venv:

| Package | License | Why it is there |
|---|---|---|
| pytest 9.1.1, pluggy 1.6.0, iniconfig 2.3.0 | MIT | test suite (`requirements-dev.txt`) |
| httpx 0.28.1, httpcore 1.0.9 | BSD-3-Clause | test client (`requirements-dev.txt`) |
| Pygments 2.20.0 | BSD-2-Clause | pytest dependency |
| packaging 26.2 | Apache-2.0 OR BSD-2-Clause | pytest dependency |
| pyflakes 3.4.0 | MIT | linting |
| anthropic 0.121.0 | MIT | only `scripts/memory_write_descriptions.py`, a hand-run tool; its dependencies are distro (Apache-2.0), docstring_parser (MIT), jiter (MIT), sniffio (MIT or Apache-2.0) |

Optional voice extra (`server/requirements-voice.txt`), **not installed here, so
from memory and unverified**: `av` BSD-3-Clause (see flag above), `numpy`
BSD-3-Clause, `sherpa-onnx` Apache-2.0.

Build backend: setuptools (MIT), fetched by pip's build isolation.

## npm (`web/`, the Android client)

From `package-lock.json` plus each installed `package.json` (`npm ci`,
2026-10-27). 105 lockfile entries. `web/package.json` is `private`; the web
app is built to static files, so only the production set can reach users.

**Production (4):** marked 18.0.14, react 19.3.0, react-dom 19.3.0,
scheduler 0.28.0. All MIT.

**Dev and build only (101):**

| License | Count | Notes |
|---|---|---|
| MIT | 79 | vite, vitest, jsdom, rolldown, postcss, @babel, @testing-library and others |
| Apache-2.0 | 10 | typescript, @playwright/test, playwright, playwright-core, aria-query, detect-libc, expect-type, fake-indexeddb, xml-name-validator, @typescript/typescript-darwin-arm64 |
| BSD-2-Clause | 2 | entities, webidl-conversions |
| BSD-3-Clause | 2 | source-map-js, tough-cookie |
| ISC | 2 | picocolors, saxes |
| MIT-0 | 2 | @csstools/color-helpers, @csstools/css-syntax-patches-for-csstree |
| BlueOak-1.0.0 | 1 | lru-cache (permissive) |
| CC0-1.0 | 1 | mdn-data |
| **MPL-2.0** | 2 installed | lightningcss, lightningcss-darwin-arm64 |
| platform binaries not installed on macOS | 43 | `@rolldown/binding-*` (MIT), `@typescript/typescript-*` (Apache-2.0), `lightningcss-*` (MPL-2.0), the same licenses as their installed siblings; the lockfile records no license for them |

No other `package.json` exists in the repo.

## iOS (Swift packages)

`ios/Marlow/project.yml` declares one package, and `Package.resolved` pins
only that one, so there are no transitive Swift packages.

| Package | License | Linked | Source of the license claim |
|---|---|---|---|
| FluidAudio (pinned revision) | Apache-2.0 | into the app | `voice-mode-tts-engines.md` § Licences; confirmed by Mike, 2026-10-27 |

Weights (not bundled; the app downloads them when the user turns voice mode on,
`VoiceModels.json`): Phonon 31m, Apache-2.0. Its own `LICENSE` is in the
download manifest at 11357 bytes, the size of the Apache-2.0 text. Phonon 40m
(CC BY-NC) is not used.

Apple frameworks are system libraries. XcodeGen (MIT) generates the project and
is not distributed. `Info.plist` and `project.yml` carry no copyright string, so
there is nothing to change there.

## Bundled data and assets

| Item | License | Where | Notes |
|---|---|---|---|
| GeoNames places | CC BY 4.0 | `server/data/places.tsv.gz` | filtered and reduced, noted in `NOTICE` |
| Face and icon art | project license (Apache-2.0) | `ios/.../Assets.xcassets`, `web/public/faces/`, `web/public/icon-*.png`, `marlow-icon-square.png` | AI-generated from the author's prompts; see `NOTICE` § Artwork. Confirm the generator's terms allow this use, and note that copyright in purely machine-generated images is unsettled in some jurisdictions. |
| Weather and routing fixtures | CC BY 4.0 (Open-Meteo, MET Norway); Open Government Licence - Canada; US public domain (NWS); ODbL (OSRM, OpenStreetMap data) | `tests/fixtures/weather/` | recorded API responses, test-only; attribution added to `NOTICE` |
| Fonts | none | | the web app uses system font stacks; no font files in the repo |

Not in the repo and not redistributed: the Claude Code CLI (Anthropic's
proprietary terms, installed by each user), Tailscale, and the Parakeet model
fetched onto the box by `install-voice.sh`.

## What changed in the repo

- `LICENSE` (verbatim Apache-2.0), `NOTICE`, `pyproject.toml` (SPDX
  `license`, `license-files`, setuptools floor raised from 61 to 77, the first
  release that reads them), `web/package.json` (`license`).
- `NOTICE` gained the artwork statement and the test-fixture attributions.
  `server/data/NOTICE` stays where it is: code and docs point to it, and it
  sits beside the data it describes.

## Not verified

- PyAV's bundled FFmpeg license, numpy and sherpa-onnx: not installed or
  fetched here.
- Whether FluidAudio ships a `NOTICE` file. Its license (Apache-2.0) and the
  Parakeet model's (Apache-2.0) are confirmed by Mike, 2026-10-27.
- That the image generator's terms permit the artwork's use under Apache-2.0.
- Licenses of the 43 platform-specific npm binaries, which are not installed
  on this Mac.
