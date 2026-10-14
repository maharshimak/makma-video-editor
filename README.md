# Makma video editor

Status: **PUBLIC SOURCE / REVIEW PENDING — NOT PRODUCTION READY**. This repository is currently publicly visible, although the earlier publication plan required private-first security and source-rights review. Public availability does not establish licensing clearance, security approval, or readiness to deploy. Do not use real credentials, personal data, or production services until reviewed.

Next.js editor -> authenticated FastAPI -> SQLAlchemy project/version/job state -> local media storage and FFmpeg render worker; a provider router supplies optional AI planning.

Implemented source scope: Timeline operations (move/trim/split/transition), viewport/indexing, compositor preview, immutable project revisions and undo/redo, asset analysis/proxies/waveforms, job progress/SSE, AI director planning and usage tracking.

The original source was not modified. Secrets, existing databases, media, model weights, binary tools, personal outputs and dependency/generated directories are absent. Credentials are blank in `.env.example`; obtain operator values privately only after the next execution step is approved. Code source rights/licensing remain to be confirmed; no license was fabricated.

Available publication documentation: [SETUP.md](SETUP.md), [ARCHITECTURE.md](ARCHITECTURE.md), and [PUBLICATION_NOTES.md](PUBLICATION_NOTES.md). Earlier references to security/readiness review reports and source manifests are not present in this repository and must not be represented as published evidence. The draft CI workflow verifies specific automated tests; it does not establish production readiness or real-provider integration.

Entry point: `apps/api/makma/main.py`. Frontend source: `apps/web`. Proposed name: `makma-video-editor`. Earlier intended visibility: **private until security, ownership/license and verification clearance**. Current visibility: **public**. The accompanying PR does not change repository visibility.
