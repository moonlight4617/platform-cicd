#!/usr/bin/env python3
"""publicな出力先（PRコメント・Actionsログ）へ出す前に機密値を伏せる.

ADR-0002の対策4に対する二重の保険。
`::add-mask::` を忘れた場合でも、PRコメントに機密が載らないようにする。

伏せる対象:
    - AWSアカウントID（12桁の数字）
    - ARN（アカウントIDを含むため全体を伏せる）
    - 環境変数で指定されたバケット名・name_prefix
    - IPアドレス（ALBの許可CIDRなど）

使い方:
    redact.py < input.md > output.md
    redact.py --in summary.md --out summary.redacted.md

環境変数（任意）:
    REDACT_EXTRA  カンマ区切りの追加文字列。完全一致で伏せる
"""

from __future__ import annotations

import argparse
import os
import re
import sys

MASK = "***"

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # ARN は丸ごと伏せる。アカウントIDとリソース名の両方を含む
    ("arn", re.compile(r"arn:aws[a-z-]*:[^\s\"'`,)\]]+")),
    # 12桁のアカウントID。前後が数字でない箇所のみ
    ("account_id", re.compile(r"(?<!\d)\d{12}(?!\d)")),
    # IPv4アドレス（CIDR表記を含む）
    ("ipv4", re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?(?![\d.])")),
]


def build_extra_patterns() -> list[tuple[str, re.Pattern[str]]]:
    """環境変数から追加の伏せ字対象を組み立てる."""
    extra: list[tuple[str, re.Pattern[str]]] = []

    # バケット名・name_prefix は値が分かっているので完全一致で伏せる
    for key in ("REPORTS_BUCKET", "ARTIFACTS_BUCKET", "TFSTATE_BUCKET", "NAME_PREFIX"):
        value = (os.environ.get(key) or "").strip()
        if len(value) >= 4:
            extra.append((key.lower(), re.compile(re.escape(value))))

    for raw in (os.environ.get("REDACT_EXTRA") or "").split(","):
        value = raw.strip()
        if len(value) >= 4:
            extra.append(("extra", re.compile(re.escape(value))))

    return extra


def redact(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    for name, pattern in PATTERNS + build_extra_patterns():
        text, n = pattern.subn(MASK, text)
        if n:
            counts[name] = counts.get(name, 0) + n
    return text, counts


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="機密値を伏せる")
    ap.add_argument("--in", dest="infile", default=None)
    ap.add_argument("--out", dest="outfile", default=None)
    ap.add_argument("--quiet", action="store_true", help="伏せた件数を標準エラーに出さない")
    args = ap.parse_args(argv)

    if args.infile:
        with open(args.infile, encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()

    redacted, counts = redact(text)

    if args.outfile:
        with open(args.outfile, "w", encoding="utf-8") as fh:
            fh.write(redacted)
    else:
        sys.stdout.write(redacted)

    if counts and not args.quiet:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        sys.stderr.write(f"伏せ字に置換しました: {detail}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
