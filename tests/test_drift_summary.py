#!/usr/bin/env python3
"""ドリフト集計のテスト（ADR-0012の補足）.

実機で2つの問題が出た。

  1. `plan -refresh-only` のドリフトは resource_changes ではなく
     **resource_drift** に入る。resource_changes だけを見ていたため
     「ドリフトを検知しました」と言いながら件数が常に0だった。

  2. refreshでは読み取り専用の計算属性も差分として現れる。
     stage / prod の両方で毎回2件の誤検知が出ており、
     放置すると本物のドリフトが埋もれる。

ここではその両方を固定する。

    python3 tests/test_drift_summary.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIFT_IGNORE = os.path.join(ROOT, "config", "drift-ignore.yaml")

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        failures.append(name)


# 実機で得られた plan -refresh-only の構造を再現したもの
PLAN = {
    "format_version": "1.2",
    "resource_changes": [],
    "resource_drift": [
        {
            # 本物のドリフト（手動で desired_count を変更した）
            "address": "aws_ecs_service.app",
            "type": "aws_ecs_service",
            "change": {
                "actions": ["update"],
                "before": {"desired_count": 1, "name": "serviceA-appA-stage"},
                "after": {"desired_count": 2, "name": "serviceA-appA-stage"},
            },
        },
        {
            # 誤検知: aws_iam_role_policy_attachment との既知の競合
            "address": "aws_iam_role.execution",
            "type": "aws_iam_role",
            "change": {
                "actions": ["update"],
                "before": {"managed_policy_arns": []},
                "after": {
                    "managed_policy_arns": [
                        "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
                    ]
                },
            },
        },
        {
            # 誤検知: 読み取り専用の計算属性
            "address": "aws_lb_target_group.app",
            "type": "aws_lb_target_group",
            "change": {
                "actions": ["update"],
                "before": {"load_balancer_arns": []},
                "after": {"load_balancer_arns": ["arn:aws:elasticloadbalancing:..."]},
            },
        },
        {
            # 誤検知: tags_all は tags から計算される
            "address": "aws_ecs_task_definition.app",
            "type": "aws_ecs_task_definition",
            "change": {
                "actions": ["update"],
                "before": {"tags_all": {}},
                "after": {"tags_all": {"Environment": "stage"}},
            },
        },
    ],
}


def run(plan: dict, tmp: str, with_ignore: bool) -> tuple[str, dict[str, str]]:
    plan_path = os.path.join(tmp, "plan.json")
    with open(plan_path, "w", encoding="utf-8") as fh:
        json.dump(plan, fh)

    out_md = os.path.join(tmp, "summary.md")
    gh_out = os.path.join(tmp, "gh_output.txt")
    open(gh_out, "w", encoding="utf-8").close()

    cmd = [
        sys.executable,
        os.path.join(ROOT, "scripts", "summarize_plan.py"),
        "--plan-json", plan_path,
        "--env", "stage",
        "--out", out_md,
        "--github-output", gh_out,
    ]
    if with_ignore:
        cmd += ["--drift-ignore", DRIFT_IGNORE]

    subprocess.run(cmd, check=True, capture_output=True, encoding="utf-8", errors="replace")

    with open(out_md, encoding="utf-8") as fh:
        md = fh.read()
    outputs: dict[str, str] = {}
    with open(gh_out, encoding="utf-8") as fh:
        for line in fh:
            if "=" in line:
                k, v = line.rstrip("\n").split("=", 1)
                outputs[k] = v
    return md, outputs


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        print("resource_drift を読む（resource_changes は空）")
        md, out = run(PLAN, tmp, with_ignore=False)
        check(
            "**ドリフトを検知できる**（resource_changes が空でも）",
            out.get("drift") == "4",
            f"drift={out.get('drift')}",
        )
        check(
            "resource_changes ベースの件数は0のまま",
            out.get("add") == "0" and out.get("change") == "0",
            f"add={out.get('add')} change={out.get('change')}",
        )

        print()
        print("誤検知を除外する（drift-ignore.yaml）")
        md, out = run(PLAN, tmp, with_ignore=True)
        check(
            "**対応が必要なドリフトは1件だけ**（本物のみ）",
            out.get("drift") == "1",
            f"drift={out.get('drift')}",
        )
        check(
            "除外された誤検知が3件",
            out.get("drift-ignored") == "3",
            f"drift-ignored={out.get('drift-ignored')}",
        )
        check("has-drift が true", out.get("has-drift") == "true")
        check(
            "要約に本物のリソース種別が載る",
            "aws_ecs_service" in md,
        )
        check(
            "要約に乖離している属性名が載る",
            "desired_count" in md,
        )
        check(
            "**属性値は載らない**（ARNが含まれるため。ADR-0002）",
            "arn:aws" not in md,
            "ドリフトの属性値にはARNやバケット名が入る",
        )
        check(
            "誤検知のリソース種別は載らない",
            "aws_lb_target_group" not in md and "aws_iam_role" not in md,
        )

        print()
        print("誤検知のみの場合は「対応不要」と判定する")
        only_noise = {
            "resource_changes": [],
            "resource_drift": [r for r in PLAN["resource_drift"] if r["type"] != "aws_ecs_service"],
        }
        md, out = run(only_noise, tmp, with_ignore=True)
        check(
            "**誤検知だけならIssueを作らせない**（has-drift が false）",
            out.get("has-drift") == "false",
            f"has-drift={out.get('has-drift')}",
        )
        check("除外件数は報告される", out.get("drift-ignored") == "3")

        print()
        print("ドリフトがない場合")
        md, out = run({"resource_changes": [], "resource_drift": []}, tmp, with_ignore=True)
        check("drift=0 / has-drift=false", out.get("drift") == "0" and out.get("has-drift") == "false")

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
