# Makma video editor

Status: **NEEDS WORK**. This is a clean local publication-preparation copy, not a tested release or an approved GitHub upload.

Next.js editor -> authenticated FastAPI -> SQLAlchemy project/version/job state -> local media storage and FFmpeg render worker; a provider router supplies optional AI planning.

Implemented source scope: Timeline operations (move/trim/split/transition), viewport/indexing, compositor preview, immutable project revisions and undo/redo, asset analysis/proxies/waveforms, job progress/SSE, AI director planning and usage tracking.

The original source was not modified. Secrets, existing databases, media, model weights, binary tools, personal outputs and dependency/generated directories are absent. Credentials are blank in `.env.example`; obtain operator values privately only after the next execution step is approved. Code source rights/licensing remain to be confirmed; no license was fabricated.

See [SETUP.md](SETUP.md), [ARCHITECTURE.md](ARCHITECTURE.md), [SECURITY_REVIEW.md](SECURITY_REVIEW.md), [TECHNICAL_ISSUES.md](TECHNICAL_ISSUES.md), [DEPENDENCY_REVIEW.md](DEPENDENCY_REVIEW.md), [READINESS.md](READINESS.md) and [CHANGES.md](CHANGES.md). SOURCE_MANIFEST.csv lists the exact inspected source paths/hashes and EXCLUDED_FILES.csv records exclusions. Static syntax/config checks do not establish passing tests/builds.

Entry point: `apps/api/makma/main.py`. Frontend source: `apps/web`. Proposed name: `makma-video-editor`. Proposed visibility: **private for initial review; public only after security, ownership/license and verification clearance**.
