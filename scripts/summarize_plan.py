#!/usr/bin/env python3
"""terraform plan のJSONから要約を作る（ADR-0002, ADR-0009）.

plan全文はprivateなS3にのみ置き、PRコメントには要約だけを載せる。
publicリポジトリではPRコメントもActionsログも公開されるため、
plan本文に含まれるARN・リソース名・属性値を出せない。

要約に載せるもの:
    - 追加 / 変更 / 削除 の件数
    - リソースの種別ごとの件数（アドレスの先頭要素だけ）
    - 削除が含まれる場合の警告

要約に載せないもの:
    - リソースのアドレス全体（名前から構成が推測できる）
    - 属性値（ARN・バケット名・IPなどが入る）

使い方:
    summarize_plan.py --plan-json plan.json --env prod --out summary.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

# plan.json の actions の組み合わせ
ACTION_LABELS = {
    ("create",): "add",
    ("update",): "change",
    ("delete",): "destroy",
    ("delete", "create"): "replace",
    ("create", "delete"): "replace",
    ("no-op",): "noop",
    ("read",): "read",
}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan-json", required=True)
    ap.add_argument("--env", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--github-output", default=None)
    args = ap.parse_args(argv)

    with open(args.plan_json, encoding="utf-8") as fh:
        doc = json.load(fh)

    counts: Counter[str] = Counter()
    by_type: Counter[str] = Counter()
    destroyed_types: Counter[str] = Counter()

    for change in doc.get("resource_changes") or []:
        actions = tuple(change.get("change", {}).get("actions") or [])
        label = ACTION_LABELS.get(actions, "other")
        if label in ("noop", "read"):
            continue
        counts[label] += 1

        # 種別だけを出す。リソース名は出さない
        rtype = change.get("type") or "(不明)"
        by_type[f"{rtype} ({label})"] += 1
        if label in ("destroy", "replace"):
            destroyed_types[rtype] += 1

    add = counts["add"]
    change_n = counts["change"]
    destroy = counts["destroy"]
    replace = counts["replace"]

    lines: list[str] = []
    lines.append(f"## terraform plan（環境: `{args.env}`）")
    lines.append("")

    if not counts:
        lines.append("差分はありません。")
    else:
        lines.append("| 操作 | 件数 |")
        lines.append("|---|---|")
        lines.append(f"| 追加 | {add} |")
        lines.append(f"| 変更 | {change_n} |")
        lines.append(f"| 削除 | {destroy} |")
        lines.append(f"| 再作成 | {replace} |")
        lines.append("")

        if destroy or replace:
            # 削除と再作成はダウンタイムや データ損失につながるため目立たせる
            types = ", ".join(f"`{t}` ×{n}" for t, n in destroyed_types.most_common())
            lines.append(f"> **削除または再作成が含まれています: {types}**")
            lines.append("> 意図した変更か確認してください。")
            lines.append("")

        lines.append("<details><summary>リソース種別ごとの件数</summary>")
        lines.append("")
        lines.append("| 種別 | 件数 |")
        lines.append("|---|---|")
        for name, n in by_type.most_common():
            lines.append(f"| `{name}` | {n} |")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    lines.append("<sub>plan全文はS3のレポートに格納しています。publicリポジトリのため、"
                 "リソース名と属性値はPRコメントに掲載していません（ADR-0002）。</sub>")

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(f"add={add}\n")
            fh.write(f"change={change_n}\n")
            fh.write(f"destroy={destroy}\n")
            fh.write(f"replace={replace}\n")

    sys.stdout.write(f"plan要約: 追加 {add} / 変更 {change_n} / 削除 {destroy} / 再作成 {replace}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
