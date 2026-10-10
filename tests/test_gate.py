#!/usr/bin/env python3
"""ゲート判定のテスト（ADR-0008）.

ゲートはCI/CDを止める判断を行う。ここが誤ると
「止まるべきものが通る」か「通るべきものが止まる」のどちらかが起きるため、
判定の境界をテストで固定する。

pytestに依存せず、標準ライブラリだけで動かす（CIの前提を増やさないため）:
    python3 tests/test_gate.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
POLICY = os.path.join(ROOT, "config", "policy.yaml")

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        failures.append(f"{name} {detail}")


def prepare_reports(tmp: str) -> str:
    """SARIFを1つのディレクトリに集める（CIのscan actionと同じ状態を作る）."""
    reports = os.path.join(tmp, "reports")
    os.makedirs(reports, exist_ok=True)

    shutil.copy(os.path.join(FIXTURES, "semgrep.sarif"), reports)

    subprocess.run(
        [
            sys.executable,
            os.path.join(ROOT, "scripts", "trivy_to_sarif.py"),
            "--input",
            os.path.join(FIXTURES, "trivy-fs.json"),
            "--output",
            os.path.join(reports, "trivy-fs.sarif"),
            "--tool-name",
            "trivy-fs",
        ],
        check=True,
        capture_output=True,
    )
    return reports


def run_gate(reports: str, tmp: str, mode: str, exceptions: str | None) -> tuple[int, dict]:
    summary_json = os.path.join(tmp, f"summary-{mode}-{bool(exceptions)}.json")
    cmd = [
        sys.executable,
        os.path.join(ROOT, "scripts", "gate.py"),
        "--sarif-dir",
        reports,
        "--policy",
        POLICY,
        "--mode",
        mode,
        "--summary-md",
        os.path.join(tmp, "summary.md"),
        "--summary-json",
        summary_json,
    ]
    if exceptions:
        cmd += ["--exceptions", exceptions]

    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    with open(summary_json, encoding="utf-8") as fh:
        return proc.returncode, json.load(fh)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        reports = prepare_reports(tmp)
        exceptions = os.path.join(FIXTURES, "security-exceptions.yaml")

        # -------------------------------------------------------------------
        print("Trivyの変換: FixedVersion が properties に残ること")
        with open(os.path.join(reports, "trivy-fs.sarif"), encoding="utf-8") as fh:
            sarif = json.load(fh)
        results = sarif["runs"][0]["results"]
        by_rule = {r["ruleId"]: r for r in results}

        check(
            "CVE-2025-11111 の fixedVersion が 1.0.4",
            by_rule["CVE-2025-11111"]["properties"]["fixedVersion"] == "1.0.4",
        )
        check(
            "CVE-2025-22222 の fixedVersion が空（修正版なし）",
            by_rule["CVE-2025-22222"]["properties"]["fixedVersion"] == "",
        )
        check(
            "CVSSは複数ベンダーの最大値を採る（9.8）",
            sarif["runs"][0]["tool"]["driver"]["rules"][0]["properties"]["security-severity"] == "9.8",
        )

        # -------------------------------------------------------------------
        print("mode=pr: CRITICALかつ修正版ありのみ停止させる")
        code, summary = run_gate(reports, tmp, "pr", exceptions)
        blocking = {f["rule_id"] for f in summary["blocking_findings"]}

        check("停止する（exit 1）", code == 1, f"exit={code}")
        check("CVE-2025-11111（修正版あり）が停止対象", "CVE-2025-11111" in blocking)
        check(
            "CVE-2025-22222（修正版なし）は停止対象でない",
            "CVE-2025-22222" not in blocking,
            "修正版がない脆弱性でリリースが永久に止まるのを防ぐ（ADR-0008）",
        )
        check(
            "CVE-2025-44444（有効な抑制）は停止対象でない",
            "CVE-2025-44444" not in blocking,
        )
        check("抑制により1件除外されている", summary["excepted"] == 1, str(summary["excepted"]))
        check(
            "HIGHのSAST検出はPRでは停止対象でない",
            "java.lang.security.audit.sqli.jdbc-sqli" not in blocking,
        )

        # -------------------------------------------------------------------
        print("抑制の不備: 期限切れ・必須項目欠け・期限が長すぎるものを検出する")
        issues = {i["exception_id"]: i["kind"] for i in summary["exception_issues"]}
        check("3件の不備を検出", len(issues) == 3, str(issues))
        check("期限切れを検出", issues.get("CVE-2025-99999") == "expired_exception")
        check("必須項目欠けを検出", issues.get("CVE-2025-88888") == "malformed_exception")
        check(
            "期限が上限超えを検出（実質的な恒久化を防ぐ）",
            issues.get("CVE-2025-77777") == "malformed_exception",
        )

        # -------------------------------------------------------------------
        print("mode=stage: CRITICALとHIGHを停止させる")
        code, summary = run_gate(reports, tmp, "stage", exceptions)
        blocking = {f["rule_id"] for f in summary["blocking_findings"]}

        check("停止する（exit 1）", code == 1, f"exit={code}")
        check(
            "HIGHのSAST検出が停止対象（修正版の概念がないものはfail closed）",
            "java.lang.security.audit.sqli.jdbc-sqli" in blocking,
        )
        check("CVE-2025-11111 が停止対象", "CVE-2025-11111" in blocking)
        check(
            "CVE-2025-22222（修正版なし）は stage でも停止対象でない",
            "CVE-2025-22222" not in blocking,
        )

        # -------------------------------------------------------------------
        print("mode=dev: 警告のみで通過する（ADR-0008）")
        code, summary = run_gate(reports, tmp, "dev", None)
        check("通過する（exit 0）", code == 0, f"exit={code}")
        check("verdict が passed", summary["verdict"] == "passed")
        check("検出自体は記録される", summary["total_findings"] > 0, str(summary["total_findings"]))

        # -------------------------------------------------------------------
        print("壊れたSARIFを「問題なし」として通さない")
        broken_dir = os.path.join(tmp, "broken")
        os.makedirs(broken_dir, exist_ok=True)
        with open(os.path.join(broken_dir, "broken.sarif"), "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")

        proc = subprocess.run(
            [
                sys.executable,
                os.path.join(ROOT, "scripts", "gate.py"),
                "--sarif-dir",
                broken_dir,
                "--policy",
                POLICY,
                "--mode",
                "pr",
                "--summary-md",
                os.path.join(tmp, "b.md"),
                "--summary-json",
                os.path.join(tmp, "b.json"),
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            text=True,
        )
        check(
            "読み込み失敗で異常終了する",
            proc.returncode != 0,
            "スキャン失敗を通過として扱うと、検査していないことに気づけない",
        )

        # -------------------------------------------------------------------
        print("redact.py: 機密値を伏せる")
        sample = "arn:aws:iam::123456789012:role/foo と 10.20.0.5/32 と 123456789012"
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "redact.py")],
            input=sample,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            text=True,
        )
        out = proc.stdout
        check("ARNが伏せられる", "arn:aws:iam" not in out, out)
        check("アカウントIDが伏せられる", "123456789012" not in out, out)
        check("IPが伏せられる", "10.20.0.5" not in out, out)

    print()
    if failures:
        print(f"失敗 {len(failures)} 件:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("すべてのテストが通過しました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
