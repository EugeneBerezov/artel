"""Build the single-file artel.pyz: python tools/build_pyz.py --out dist/artel.pyz"""
import argparse
import shutil
import sys
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = "import sys\nfrom artel.cli import main\nsys.exit(main())\n"


MARKER = ".artel-pyz-staging"


def build(out: Path, staging: Path) -> Path:
    if staging.exists():
        if (staging / MARKER).is_file():
            shutil.rmtree(staging)
        elif not staging.is_dir() or any(staging.iterdir()):
            print(f"error: staging directory {staging} is not empty and is not an artel staging "
                  f"directory (no {MARKER}); refusing to delete it", file=sys.stderr)
            raise SystemExit(2)
    staging.mkdir(parents=True, exist_ok=True)
    (staging / MARKER).write_text("artel.pyz staging directory; safe to delete\n", encoding="utf-8", newline="\n")
    shutil.copytree(ROOT / "artel", staging / "artel", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (staging / "__main__.py").write_text(MAIN, encoding="utf-8", newline="\n")
    out.parent.mkdir(parents=True, exist_ok=True)
    zipapp.create_archive(staging, out, interpreter="/usr/bin/env python3", compressed=True)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build artel.pyz")
    parser.add_argument("--out", type=Path, default=Path("dist/artel.pyz"))
    parser.add_argument("--staging", type=Path, default=Path("build/pyz-staging"))
    args = parser.parse_args(argv)
    out, staging = (p if p.is_absolute() else ROOT / p for p in (args.out, args.staging))   # not the cwd
    print(build(out.resolve(), staging.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
