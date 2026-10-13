"""Export the project schema as JSON Schema.

The Python models are the source of truth. Emitting them as JSON Schema gives
the frontend, future plugins and any external tooling one machine-readable
contract instead of a hand-maintained copy that silently drifts.

Run:  python -m makma.schema.export
"""
from __future__ import annotations

import json
from pathlib import Path

from .core import ProjectDocument, Sequence
from .media import MediaAnalysis, ProbeResult
from .operations import EditOperation, EditPlan

OUTPUT_DIR = Path(__file__).resolve().parents[4] / "packages" / "project-schema"

DOCUMENTS = {
    "project-document.schema.json": ProjectDocument,
    "sequence.schema.json": Sequence,
    "edit-plan.schema.json": EditPlan,
    "edit-operation.schema.json": EditOperation,
    "media-analysis.schema.json": MediaAnalysis,
    "probe-result.schema.json": ProbeResult,
}


def export(directory: Path | None = None) -> list[Path]:
    target = directory or OUTPUT_DIR
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, model in DOCUMENTS.items():
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://makma.dev/schema/{filename}"
        path = target / filename
        path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":  # pragma: no cover
    for path in export():
        print(f"wrote {path}")
