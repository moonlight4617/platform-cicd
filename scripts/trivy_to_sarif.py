#!/usr/bin/env python3
"""TrivyのJSON出力をSARIFに変換する.

なぜ自前で変換するか:
    ADR-0008のゲート基準は「CRITICAL/HIGH かつ修正版が提供されているもの」。
    Trivyの組み込みSARIFテンプレートは FixedVersion を構造化された形で
    残さない（本文テキストに混ざる）ため、判定に使えない。

    かといって「Trivyだけ別形式」にすると、ADR-0008で統一したSARIFの契約が
    崩れ、判定ロジックがツール数に比例して増える。そのため
    「JSONで取得し、FixedVersion を properties に保持したSARIFへ変換する」
    形にして、下流はSARIFだけを見れば済む状態を保つ。

使い方:
    trivy_to_sarif.py --input trivy.json --output reports/trivy-fs.sarif \
                      --tool-name trivy-fs
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

# CVSSスコアを持たない結果のための、深刻度からのフォールバック
CVSS_BY_SEVERITY = {
    "CRITICAL": "9.5",
    "HIGH": "7.5",
    "MEDIUM": "5.5",
    "LOW": "2.0",
    "UNKNOWN": "0.0",
}

LEVEL_BY_SEVERITY = {
    "CRITICAL": "error",
    "HIGH": "error",
    "MEDIUM": "warning",
    "LOW": "note",
    "UNKNOWN": "note",
}


def _cvss_score(vuln: dict[str, Any]) -> str:
    """CVSS v3のスコアを取り出す。複数ベンダーの値があるため最大値を使う."""
    scores: list[float] = []
    for entry in (vuln.get("CVSS") or {}).values():
        for key in ("V3Score", "V4Score"):
            value = entry.get(key)
            if isinstance(value, (int, float)):
                scores.append(float(value))
    if scores:
        return str(max(scores))
    return CVSS_BY_SEVERITY.get((vuln.get("Severity") or "UNKNOWN").upper(), "0.0")


def _rule_id(item: dict[str, Any]) -> str:
    # 脆弱性はCVE等のID、設定不備（misconfig）はチェックのID
    return item.get("VulnerabilityID") or item.get("ID") or item.get("AVDID") or "TRIVY-UNKNOWN"


def convert(doc: dict[str, Any], tool_name: str) -> dict[str, Any]:
    rules: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []

    for target in doc.get("Results") or []:
        target_path = target.get("Target") or ""

        # --- 依存ライブラリ・OSパッケージの脆弱性 ---
        for vuln in target.get("Vulnerabilities") or []:
            rid = _rule_id(vuln)
            severity = (vuln.get("Severity") or "UNKNOWN").upper()
            fixed = (vuln.get("FixedVersion") or "").strip()

            if rid not in rules:
                rules[rid] = {
                    "id": rid,
                    "name": rid,
                    "shortDescription": {"text": (vuln.get("Title") or rid)[:200]},
                    "fullDescription": {"text": (vuln.get("Description") or "")[:2000]},
                    "helpUri": vuln.get("PrimaryURL") or "",
                    "defaultConfiguration": {"level": LEVEL_BY_SEVERITY.get(severity, "warning")},
                    "properties": {
                        "security-severity": _cvss_score(vuln),
                        "tags": ["vulnerability", severity],
                    },
                }

            pkg = vuln.get("PkgName") or "(不明)"
            installed = vuln.get("InstalledVersion") or "(不明)"
            text = f"{rid}: {pkg} {installed}"
            text += f" → 修正版 {fixed}" if fixed else " → 修正版なし"
            if vuln.get("Title"):
                text += f" / {vuln['Title']}"

            results.append(
                {
                    "ruleId": rid,
                    "level": LEVEL_BY_SEVERITY.get(severity, "warning"),
                    "message": {"text": text},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": target_path or "unknown"},
                                "region": {"startLine": 1},
                            }
                        }
                    ],
                    # ここが自前変換の目的。ゲート判定がこれを読む
                    "properties": {
                        "fixedVersion": fixed,
                        "severity": severity,
                        "package": pkg,
                        "installedVersion": installed,
                    },
                }
            )

        # --- IaCの設定不備（trivy config） ---
        for mis in target.get("Misconfigurations") or []:
            rid = _rule_id(mis)
            severity = (mis.get("Severity") or "UNKNOWN").upper()

            if rid not in rules:
                rules[rid] = {
                    "id": rid,
                    "name": rid,
                    "shortDescription": {"text": (mis.get("Title") or rid)[:200]},
                    "fullDescription": {"text": (mis.get("Description") or "")[:2000]},
                    "helpUri": mis.get("PrimaryURL") or "",
                    "defaultConfiguration": {"level": LEVEL_BY_SEVERITY.get(severity, "warning")},
                    "properties": {
                        "security-severity": CVSS_BY_SEVERITY.get(severity, "0.0"),
                        "tags": ["misconfiguration", severity],
                    },
                }

            start = ((mis.get("CauseMetadata") or {}).get("StartLine")) or 1
            results.append(
                {
                    "ruleId": rid,
                    "level": LEVEL_BY_SEVERITY.get(severity, "warning"),
                    "message": {
                        "text": f"{rid}: {mis.get('Title') or ''} / {mis.get('Resolution') or ''}".strip()
                    },
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": target_path or "unknown"},
                                "region": {"startLine": max(int(start), 1)},
                            }
                        }
                    ],
                    # 設定不備はコードを直す以外に手段がないため、
                    # 「修正版あり」として扱う（fixedVersion に印を入れる）
                    "properties": {"fixedVersion": "code-fix", "severity": severity},
                }
            )

        # --- コミットされたシークレット（trivy fs --scanners secret） ---
        for sec in target.get("Secrets") or []:
            rid = f"SECRET-{sec.get('RuleID') or 'UNKNOWN'}"
            severity = (sec.get("Severity") or "CRITICAL").upper()

            if rid not in rules:
                rules[rid] = {
                    "id": rid,
                    "name": rid,
                    "shortDescription": {"text": (sec.get("Title") or rid)[:200]},
                    "defaultConfiguration": {"level": "error"},
                    "properties": {
                        "security-severity": CVSS_BY_SEVERITY.get("CRITICAL"),
                        "tags": ["secret", severity],
                    },
                }

            # ADR-0002: 検出された秘密情報そのものはレポートに残さない。
            # publicリポジトリでは、レポートが漏れた時点で二次被害になる
            results.append(
                {
                    "ruleId": rid,
                    "level": "error",
                    "message": {"text": f"{rid}: シークレットの可能性がある文字列を検出（値は記録しない）"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": target_path or "unknown"},
                                "region": {"startLine": max(int(sec.get("StartLine") or 1), 1)},
                            }
                        }
                    ],
                    "properties": {"fixedVersion": "code-fix", "severity": severity},
                }
            )

    return {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": tool_name,
                        "informationUri": "https://github.com/aquasecurity/trivy",
                        "rules": list(rules.values()),
                    }
                },
                "results": results,
            }
        ],
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="TrivyのJSONをSARIFへ変換する")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--tool-name", default="trivy")
    args = ap.parse_args(argv)

    with open(args.input, encoding="utf-8") as fh:
        doc = json.load(fh)

    sarif = convert(doc, args.tool_name)

    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(sarif, fh, ensure_ascii=False, indent=2)

    runs = sarif["runs"][0]
    sys.stdout.write(
        f"{args.tool_name}: {len(runs['results'])} 件の検出を {args.output} に書き出しました\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
