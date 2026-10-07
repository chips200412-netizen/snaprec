from __future__ import annotations

import argparse
import os
from pathlib import Path

from .bootstrap import build_pipeline
from .runtime_config import RuntimeConfigError, resolve_runtime_config
from .services.pipeline import PipelineError


def main() -> int:
    parser = argparse.ArgumentParser(description="Process one local media file.")
    parser.add_argument("media", type=Path)
    parser.add_argument("--database", type=Path, default=Path("var/video_notes.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path("video-note.md"))
    args = parser.parse_args()
    try:
        environment = dict(os.environ)
        environment["VIDEO_DB_PATH"] = str(args.database.absolute())
        project_root = Path(__file__).resolve().parents[2]
        config = resolve_runtime_config(environment, project_root)
        pipeline = build_pipeline(config)
        _, _, markdown = pipeline.process(args.media)
    except RuntimeConfigError as exc:
        parser.error(f"{exc.code}: {exc.field}")
    except PipelineError as exc:
        parser.error(f"{exc.code}: {exc}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(markdown, encoding="utf-8")
    print(f"completed: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
