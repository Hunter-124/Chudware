"""Emit a model-facing trial pack so a REAL vision model can be measured.

The scripted controls in `__main__` validate the measuring instrument; they cannot
answer the question the instrument exists for — whether the frozen oracle's
geometric inspectability predicts whether a real model can actually answer.

This module writes a pack that a real model consumes: one PNG per trial plus a
`questions.json` carrying only the model-facing `PublicQuestion` (prompt, choices,
marker labels). Ground truth and every oracle value stay in a separate
`answers.json` the answering model never reads, so a run cannot leak its own label.

Trials are stratified by the frozen aggregate inspectability so a correlation is
measurable at all: an unstratified sample of a distribution with little variance
would report a null for lack of spread rather than for lack of relationship.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from ..pixels import PixelCorpusConfig, generate_pixel_corpus
from .binding import bind_pixel_corpus
from .catalog import build_question_catalog


def _write_png(path: Path, image: np.ndarray) -> None:
    """Write RGB uint8 without pulling in an image library."""
    import struct
    import zlib

    array = np.ascontiguousarray(image[:, :, :3].astype(np.uint8))
    height, width = array.shape[:2]
    raw = b"".join(b"\x00" + array[row].tobytes() for row in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def build_pack(
    manifest: str,
    out_dir: Path,
    *,
    trials: int = 24,
    parts: int = 522,
    seed: int = 1729,
    strata: int = 3,
    resolution: int = 384,
) -> dict:
    # Usefulness must be measured at the resolution a model is actually GIVEN, not at the
    # encoder's 96x96 training size — at 96x96 these features are barely resolvable and the
    # correlation would floor for want of legibility rather than for want of a relationship.
    # The oracle's `scale` component is resolution-coupled, so pixels and labels are
    # regenerated together at the viewing size instead of upscaling 96x96 renders.
    config = PixelCorpusConfig(
        parts=parts,
        resolution=(resolution, resolution),
        oracle_image_size=(resolution, resolution),
        cache_dir=Path(out_dir) / "corpus",
    )
    corpus = generate_pixel_corpus(config, workers=6, device="auto")
    catalog = build_question_catalog(range(parts), seed=seed)
    items = bind_pixel_corpus(catalog, corpus)
    if not items:
        raise RuntimeError("no benchmark items bound; check the manifest")

    rng = np.random.default_rng(seed)
    # One (item, candidate) pair per trial. Stratify on the frozen aggregate so the
    # sample spans low/medium/high inspectability instead of clustering.
    pool = [
        (item_index, cand_index, float(candidate.aggregate))
        for item_index, item in enumerate(items)
        for cand_index, candidate in enumerate(item.candidates)
    ]
    # Stratify by aggregate VALUE, not by rank. The candidate distribution is heavily
    # zero-inflated -- most random poses are simply bad -- so equal-count rank bands put
    # almost every trial at 0.000 and a correlation would be measuring noise for want of
    # spread. Value bands force the sample to span the range that actually exists.
    bands = ((0.0, 0.01), (0.01, 0.20), (0.20, 0.45), (0.45, 1.01))
    per_band = max(1, trials // len(bands))
    chosen: list[tuple[int, int, float]] = []
    for lo, hi in bands:
        members = [row for row in pool if lo <= row[2] < hi]
        if not members:
            continue
        take = min(per_band, len(members))
        picks = rng.choice(len(members), size=take, replace=False)
        chosen.extend(members[int(index)] for index in picks)
    if not chosen:
        raise RuntimeError("stratification produced no trials")

    out_dir.mkdir(parents=True, exist_ok=True)
    questions: list[dict] = []
    answers: list[dict] = []
    for trial_index, (item_index, cand_index, aggregate) in enumerate(sorted(chosen)):
        item = items[item_index]
        candidate = item.candidates[cand_index]
        name = f"trial_{trial_index:03d}.png"
        _write_png(out_dir / name, candidate.observation.image)
        questions.append(
            {
                "trial": trial_index,
                "image": name,
                "question_id": item.question.public.question_id,
                "kind": item.question.public.kind,
                "prompt": item.question.public.prompt,
                "choices": list(item.question.public.choices),
                "marker_labels": list(item.question.public.marker_labels),
            }
        )
        answers.append(
            {
                "trial": trial_index,
                "question_id": item.question.public.question_id,
                "kind": item.question.public.kind,
                "difficulty": item.question.difficulty,
                "truth": item.question.correct_answer,
                "aggregate": aggregate,
                "components": dict(candidate.oracle_components),
            }
        )

    (out_dir / "questions.json").write_text(
        json.dumps({"trials": questions}, indent=2), encoding="utf-8"
    )
    (out_dir / "answers.json").write_text(
        json.dumps({"trials": answers}, indent=2), encoding="utf-8"
    )
    return {
        "trials": len(questions),
        "out_dir": str(out_dir),
        "aggregate_min": min(row[2] for row in chosen),
        "aggregate_max": max(row[2] for row in chosen),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="scratch/viewpoint-pixels-large/manifest.json")
    parser.add_argument("--out", default="scratch/viewpoint-live-trials")
    parser.add_argument("--trials", type=int, default=24)
    parser.add_argument("--parts", type=int, default=522)
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--resolution", type=int, default=384)
    args = parser.parse_args()
    summary = build_pack(
        args.manifest, Path(args.out), trials=args.trials, parts=args.parts,
        seed=args.seed, resolution=args.resolution
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
