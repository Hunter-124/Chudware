from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .agreement import compare_with_engine
from .corpus import generate_pixel_corpus, generate_visibility_audit_corpus
from .schema import PixelCorpusConfig


def _summary(corpus) -> dict[str, object]:
    manifest = json.loads(corpus.manifest_path.read_text(encoding="utf-8"))
    return {
        "manifest": str(corpus.manifest_path),
        "renderer_version": corpus.renderer_version,
        "oracle_version": corpus.oracle_version,
        "oracle_backends": manifest.get("oracle_backends", []),
        "generation_seconds": corpus.generation_seconds,
        "cache_hits": corpus.cache_hits,
        "statistics": manifest.get("statistics", {}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the camera-limited viewpoint pixel corpus")
    parser.add_argument("--agreement", action="store_true", help="run the real-engine raster agreement probe")
    parser.add_argument("--visibility-audit", action="store_true", help="generate the frozen pooled 8x8 audit")
    parser.add_argument("--parts", type=int, default=522)
    parser.add_argument("--examples-per-part", type=int, default=6)
    parser.add_argument("--candidates", type=int, default=16)
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--width", type=int, default=96)
    parser.add_argument("--height", type=int, default=96)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--device", choices=("auto", "cuda", "cpu", "numpy"), default="auto")
    parser.add_argument("--cache-dir", type=Path, default=Path("scratch/viewpoint-pixels-large"))
    args = parser.parse_args(argv)
    if args.agreement:
        result = compare_with_engine(seed=args.seed, resolution=(args.width, args.height))
        print(json.dumps({**asdict(result), "engine_path": str(result.engine_path)}, indent=2))
        return 0
    if args.visibility_audit:
        corpus = generate_visibility_audit_corpus(seed=args.seed, device=args.device, cache_dir=args.cache_dir)
    else:
        corpus = generate_pixel_corpus(
            PixelCorpusConfig(
                parts=args.parts,
                examples_per_part=args.examples_per_part,
                candidates=args.candidates,
                seed=args.seed,
                resolution=(args.width, args.height),
                cache_dir=args.cache_dir,
            ),
            workers=args.workers,
            device=args.device,
        )
    print(json.dumps(_summary(corpus), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
