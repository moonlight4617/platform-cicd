#!/usr/bin/env python3
"""設定ファイルの整合性を検証する.

config/policy.yaml と config/approvers.yaml は、それぞれ
gate.py と notify_slack.py が前提にしている構造を持つ必要がある。
構造が崩れていると、CIの実行時にはじめて失敗する。

特に approvers.yaml のペルソナ参照の崩れは危険:
    gates が未定義のペルソナを参照していても通知は送られてしまい、
    「誰にも通知が届かない承認待ち」が静かに発生する。

    python3 tests/check_config.py
"""

from __future__ import annotations

import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent

REQUIRED_POLICY_KEYS = ("severity", "gates", "exceptions", "fix_availability")
REQUIRED_MODES = ("pr", "dev", "stage", "prod")
REQUIRED_SEVERITY_KEYS = ("from_cvss", "from_level", "order")


def check_policy(errors: list[str]) -> None:
    path = ROOT / "config" / "policy.yaml"
    policy = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    for key in REQUIRED_POLICY_KEYS:
        if key not in policy:
            errors.append(f"policy.yaml に {key} がありません")

    for key in REQUIRED_SEVERITY_KEYS:
        if key not in (policy.get("severity") or {}):
            errors.append(f"policy.yaml の severity に {key} がありません")

    gates = policy.get("gates") or {}
    for mode in REQUIRED_MODES:
        if mode not in gates:
            errors.append(f"policy.yaml の gates に {mode} がありません")
            continue
        if "block_severities" not in gates[mode]:
            errors.append(f"policy.yaml の gates.{mode} に block_severities がありません")

    order = set((policy.get("severity") or {}).get("order") or [])
    for mode, gate in gates.items():
        for sev in gate.get("block_severities") or []:
            if sev not in order:
                errors.append(
                    f"policy.yaml の gates.{mode}.block_severities に未知の深刻度: {sev}"
                )

    unknown = (policy.get("fix_availability") or {}).get("unknown")
    if unknown not in ("block", "allow"):
        errors.append(f"policy.yaml の fix_availability.unknown が不正: {unknown}")

    # 抑制の上限が長すぎると、抑制が実質的に恒久化する（ADR-0008）
    max_days = (policy.get("exceptions") or {}).get("max_days_from_now")
    if not isinstance(max_days, int) or max_days <= 0:
        errors.append("policy.yaml の exceptions.max_days_from_now が正の整数ではありません")
    elif max_days > 365:
        errors.append(
            f"policy.yaml の exceptions.max_days_from_now が長すぎます（{max_days}日）。"
            "抑制の恒久化を防ぐ目的に反します"
        )


def check_approvers(errors: list[str]) -> None:
    path = ROOT / "config" / "approvers.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    personas = doc.get("personas") or {}
    if not personas:
        errors.append("approvers.yaml に personas がありません")
        return

    for name, persona in personas.items():
        if not (persona.get("github") or []):
            errors.append(f"approvers.yaml のペルソナ {name} に github アカウントがありません")
        if not (persona.get("slack") or []):
            # SlackのIDが未設定でも通知は送るが、メンションは届かない。
            # 黙って届かない状態を作らないよう警告として出す
            print(
                f"::warning::approvers.yaml のペルソナ {name} にSlackのIDが未設定です。"
                "承認待ち通知でメンションされません"
            )

    def check_refs(names: list[str] | None, where: str) -> None:
        for name in names or []:
            if name not in personas:
                errors.append(f"approvers.yaml の {where} が未定義のペルソナを参照: {name}")

    for gate_name, gate in (doc.get("gates") or {}).items():
        check_refs(gate.get("approvers"), f"gates.{gate_name}")
        for env in gate.get("environments") or []:
            check_refs(env.get("approvers"), f"gates.{gate_name}.{env.get('name')}")

    # ADR-0003: prodは3段階。1つの環境に複数人を登録すると
    # 「誰か1人の承認」で通ってしまい、多段承認にならない
    prod = (doc.get("gates") or {}).get("prod") or {}
    approval_envs = [
        env for env in (prod.get("environments") or []) if env.get("approvers")
    ]
    if len(approval_envs) != 3:
        errors.append(
            f"approvers.yaml の gates.prod は承認者を持つ環境が3つ必要です"
            f"（現在 {len(approval_envs)} つ。ADR-0003）"
        )
    for env in approval_envs:
        if len(env.get("approvers") or []) != 1:
            errors.append(
                f"approvers.yaml の {env.get('name')} は承認者を1ペルソナにしてください。"
                "1つの環境に複数登録すると、誰か1人の承認で通過します（ADR-0003）"
            )


def main() -> int:
    errors: list[str] = []
    check_policy(errors)
    check_approvers(errors)

    if errors:
        for e in errors:
            print(f"::error::{e}")
        print(f"\n設定の不備: {len(errors)} 件")
        return 1

    print("設定ファイルの整合性を確認しました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
