#!/usr/bin/env python3
"""terraform plan のJSONから要約を作る（ADR-0002, ADR-0009, ADR-0012）.

plan全文はprivateなS3にのみ置き、PRコメントには要約だけを載せる。
publicリポジトリではPRコメントもActionsログも公開されるため、
plan本文に含まれるARN・リソース名・属性値を出せない。

要約に載せるもの:
    - 追加 / 変更 / 削除 の件数
    - リソースの種別ごとの件数（アドレスの先頭要素だけ）
    - 削除が含まれる場合の警告
    - ドリフト（resource_drift）のリソース種別と**属性名**

要約に載せないもの:
    - リソースのアドレス全体（名前から構成が推測できる）
    - 属性値（ARN・バケット名・IPなどが入る）

ドリフトの扱い（ADR-0012の補足）:
    `plan -refresh-only` のドリフトは resource_changes ではなく
    **resource_drift** に入る。resource_changes だけを見ていると
    件数が常に0になる。

    また refresh では「読み取り専用の計算属性」も差分として現れるため、
    config/drift-ignore.yaml で無視する属性を指定する。
    指定しないと毎回誤検知が出て、本物のドリフトが埋もれる。

使い方:
    summarize_plan.py --plan-json plan.json --env prod --out summary.md \
                      --drift-ignore config/drift-ignore.yaml
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Any

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


def load_drift_ignore(path: str | None) -> tuple[dict[str, set[str]], set[str]]:
    """無視する属性を読む。指定がなければ空（すべて報告する）."""
    if not path:
        return {}, set()

    try:
        import yaml
    except ImportError:
        sys.stderr.write("::warning::PyYAMLがないため drift-ignore を読み込めません\n")
        return {}, set()

    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except OSError as exc:
        sys.stderr.write(f"::warning::drift-ignore の読み込みに失敗: {exc}\n")
        return {}, set()

    by_type: dict[str, set[str]] = {}
    for rtype, entries in (doc.get("by_resource_type") or {}).items():
        by_type[rtype] = {e["attribute"] for e in entries if e.get("attribute")}

    glob = {e["attribute"] for e in (doc.get("global") or []) if e.get("attribute")}
    return by_type, glob


def changed_attributes(change: dict[str, Any]) -> list[str]:
    """before と after を比べて、変わった属性名だけを返す（値は返さない）."""
    before = change.get("before") or {}
    after = change.get("after") or {}
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))


def summarize_drift(
    doc: dict[str, Any], by_type: dict[str, set[str]], glob: set[str]
) -> tuple[list[tuple[str, list[str]]], int]:
    """ドリフトを集計する.

    戻り値: ([(リソース種別, 変わった属性名), ...], 無視した件数)

    **属性値は返さない。** ARNやバケット名が含まれるため（ADR-0002）。
    """
    real: list[tuple[str, list[str]]] = []
    ignored = 0

    for item in doc.get("resource_drift") or []:
        rtype = item.get("type") or "(不明)"
        attrs = changed_attributes(item.get("change") or {})
        ignore = by_type.get(rtype, set()) | glob
        remaining = [a for a in attrs if a not in ignore]

        if not remaining:
            ignored += 1
            continue
        real.append((rtype, remaining))

    return real, ignored


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan-json", required=True)
    ap.add_argument("--env", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--github-output", default=None)
    ap.add_argument(
        "--drift-ignore",
        default=None,
        help="無視する属性の定義（config/drift-ignore.yaml）",
    )
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

    # --- ドリフト（-refresh-only のとき resource_drift に入る。ADR-0012） ---
    by_type, glob = load_drift_ignore(args.drift_ignore)
    drift, drift_ignored = summarize_drift(doc, by_type, glob)

    if drift or drift_ignored:
        lines.append("### ドリフト（コードとAWSの実態の乖離）")
        lines.append("")
        if drift:
            lines.append("| リソース種別 | 乖離している属性 |")
            lines.append("|---|---|")
            for rtype, attrs in drift:
                lines.append(f"| `{rtype}` | {', '.join(f'`{a}`' for a in attrs)} |")
        else:
            lines.append("対応が必要なドリフトはありません。")
        lines.append("")
        if drift_ignored:
            lines.append(
                f"<sub>読み取り専用の計算属性など、差分として扱わないもの {drift_ignored} 件を"
                "除外しています（platform-cicd/config/drift-ignore.yaml）。</sub>"
            )
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
            fh.write(f"drift={len(drift)}\n")
            fh.write(f"drift-ignored={drift_ignored}\n")
            fh.write(f"has-drift={'true' if drift else 'false'}\n")

    sys.stdout.write(
        f"plan要約: 追加 {add} / 変更 {change_n} / 削除 {destroy} / 再作成 {replace}\n"
    )
    sys.stdout.write(
        f"ドリフト: 対応要 {len(drift)} 件 / 除外 {drift_ignored} 件\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
