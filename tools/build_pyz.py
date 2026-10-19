"""Build the single-file artel.pyz: python tools/build_pyz.py --out dist/artel.pyz"""
import argparse
import shutil
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = "import sys\nfrom artel.cli import main\nsys.exit(main())\n"


def build(out: Path, staging: Path) -> Path:
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    shutil.copytree(ROOT / "artel", staging / "artel", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (staging / "__main__.py").write_text(MAIN, encoding="utf-8", newline="\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    zipapp.create_archive(staging, out, interpreter="/usr/bin/env python3", compressed=True)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build artel.pyz")
    parser.add_argument("--out", type=Path, default=Path("dist/artel.pyz"))
    parser.add_argument("--staging", type=Path, default=ROOT / "build" / "pyz-staging")
    args = parser.parse_args(argv)
    print(build(args.out.resolve(), args.staging.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
