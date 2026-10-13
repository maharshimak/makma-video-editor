# Setup and verification plan — commands NOT EXECUTED

Dependency installation, copied scripts, test execution, app launches, network calls, Docker builds and inference were not approved for this phase and were not run. Obtain execution approval before using the commands below. Never point these packages at original databases or copy original `.env` files into them.

Use Python 3.11+ (3.12 for the sandbox image). Select a Node.js version satisfying the copied frontend dependency engines; research frontend declares Node >=22.13.0. Provider model defaults are inherited source configuration, not a current availability guarantee.

From this staging root, after execution approval:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Configure a new local environment from the **blank authored example**. Never commit actual credentials. Select a strong private signing secret where required; enable demo mode only for reviewed synthetic fixtures. No secrets or preexisting DB content is provided.

Install trusted FFmpeg/ffprobe separately; executable binaries were excluded. Disable dev autologin/debug in any shared use and use the existing production guard. Review live-provider markers and FFmpeg-dependent tests before invoking pytest.

Proposed local backend command (after the required fixes/approval):

```powershell
python -m uvicorn makma.main:app --app-dir apps/api --host 127.0.0.1 --port 8000
```

Frontend installation/build in a separate terminal (after approval):

```powershell
Set-Location apps\web
npm ci
npm run build
```

Review available scripts in `apps/web/package.json` before selecting dev/typecheck/lint/test commands; a script's presence is not a passing result. Node package locks were copied with optional funding/contact metadata removed, preserving resolution/integrity fields.

Proposed Python verification:

```powershell
python -m pytest tests -m "not live"
```

No command above is authorized by this file alone. Record exact outcomes and use mocks/synthetic inputs before live APIs, rendering, browser interaction, downloads, training or inference. GitHub creation/commit/push/merge/deploy/deletion remain unapproved.

## Separate project environments and frontend start

Use a distinct virtual environment per package. The editor and research packages both use the import namespace makma; do not install them into one shared environment. Several backends/frontends use the same default ports; run separately or deliberately configure matching origins/ports.

After execution approval, the documented frontend start command from its directory is:

```powershell
npm run dev -- --hostname 127.0.0.1 --port 3000
```

These commands remain unexecuted. Browser-access, keyboard behavior, reduced motion, rendering cleanup and screen-reader labels need approved verification. Review prior data/secret exclusions before any setup.

Publication review update: keep API/UI bound to loopback. The unofficial Google translation scraping fallback is disabled in Lyrflow; missing AI translation returns an explicit unavailable result. Dependencies use an advisory-remediated FastAPI/Starlette stack, verified by offline tests.
