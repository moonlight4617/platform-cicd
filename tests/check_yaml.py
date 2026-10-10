#!/usr/bin/env python3
"""ワークフロー・action・設定のYAML構文を検証する.

ワークフローのYAMLが壊れていると、GitHub側では
「ワークフローが存在しない」ように見えて原因が分かりにくい。
pushする前にローカルでも検出できるようにしておく。

    python3 tests/check_yaml.py
"""

from __future__ import annotations

import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent

PATTERNS = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    "actions/*/action.yml",
    "config/*.yaml",
)


def main() -> int:
    targets: list[pathlib.Path] = []
    for pattern in PATTERNS:
        targets.extend(sorted(ROOT.glob(pattern)))

    if not targets:
        print("検証対象のYAMLが見つかりません")
        return 1

    failed: list[str] = []
    for path in targets:
        rel = path.relative_to(ROOT)
        try:
            yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            print(f"  ERROR {rel}: {exc}")
            failed.append(str(rel))
        else:
            print(f"  OK    {rel}")

    print()
    if failed:
        print(f"YAMLの構文エラー: {len(failed)} 件")
        return 1

    print(f"{len(targets)} 件のYAMLを検証しました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
