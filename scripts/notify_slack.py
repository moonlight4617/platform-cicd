#!/usr/bin/env python3
"""Incoming WebhookでSlackに通知する（ADR-0015）.

ペルソナ名からSlackのユーザーIDを解決する。実アカウントの割り当ては
config/approvers.yaml にあり、ここは解決とメッセージ組み立てだけを行う。

本文に機密を含めないこと。Slackは転送されやすいため、ARN・アカウントID・
バケット名はリンク先（GitHub / AWSコンソール）で確認させる方針（ADR-0002）。

WebhookのURLは環境変数 WEBHOOK_URL から読む（引数に渡すとプロセス一覧に出る）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

import yaml

COLORS = {
    "success": "#2eb886",
    "failure": "#d93f0b",
    "warning": "#dbab09",
    "info": "#5b6fbf",
}

ICONS = {
    "success": ":white_check_mark:",
    "failure": ":x:",
    "warning": ":warning:",
    "info": ":information_source:",
}


def resolve_mentions(approvers_path: str, personas: str) -> tuple[str, list[str]]:
    """ペルソナ名をSlackのメンション文字列に変換する.

    戻り値: (メンション文字列, 未解決のペルソナ名)
    """
    names = [p.strip() for p in (personas or "").split(",") if p.strip()]
    if not names:
        return "", []

    with open(approvers_path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}

    defined = doc.get("personas") or {}
    mentions: list[str] = []
    unresolved: list[str] = []

    for name in names:
        persona = defined.get(name)
        if not persona:
            unresolved.append(f"{name}（未定義）")
            continue
        slack_ids = persona.get("slack") or []
        if not slack_ids:
            # SlackのIDが未設定でも通知自体は送る。誰に向けた通知かは本文に残す
            unresolved.append(name)
            continue
        mentions.extend(f"<@{sid}>" for sid in slack_ids)

    return " ".join(mentions), unresolved


def build_payload(args: argparse.Namespace, mention: str, unresolved: list[str]) -> dict:
    repo = os.environ.get("GITHUB_REPOSITORY", "(不明)")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_url = f"{server}/{repo}/actions/runs/{run_id}" if run_id else ""

    icon = ICONS.get(args.status, ICONS["info"])
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{icon} *{args.title}*"},
        }
    ]

    if mention:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"承認待ち: {mention}"}})

    if unresolved:
        # SlackのIDが未設定のペルソナは、メンションできないことを明示する。
        # 黙って通知が届かない状態が一番危ない
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f":bell: SlackのID未設定のためメンションできません: {', '.join(unresolved)}",
                    }
                ],
            }
        )

    if args.body:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": args.body}})

    context_parts = [f"`{repo}`"]
    if run_url:
        context_parts.append(f"<{run_url}|実行ログ>")
    blocks.append(
        {"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(context_parts)}]}
    )

    if args.link_url:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": args.link_text},
                        "url": args.link_url,
                        "style": "primary" if args.status != "failure" else "danger",
                    }
                ],
            }
        )

    return {
        "attachments": [
            {
                "color": COLORS.get(args.status, COLORS["info"]),
                "blocks": blocks,
            }
        ]
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--approvers", required=True)
    ap.add_argument("--status", default="info", choices=list(COLORS))
    ap.add_argument("--title", required=True)
    ap.add_argument("--body", default="")
    ap.add_argument("--mention-personas", default="")
    ap.add_argument("--link-url", default="")
    ap.add_argument("--link-text", default="GitHubで開く")
    ap.add_argument("--dry-run", action="store_true", help="送信せずpayloadを標準出力に出す")
    args = ap.parse_args(argv)

    mention, unresolved = resolve_mentions(args.approvers, args.mention_personas)
    payload = build_payload(args, mention, unresolved)

    if args.dry_run:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    webhook = os.environ.get("WEBHOOK_URL", "").strip()
    if not webhook:
        sys.stderr.write("::warning::WEBHOOK_URL が未設定のため通知をスキップします\n")
        return 0

    req = urllib.request.Request(
        webhook,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            body = res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # 通知の失敗でCI/CDを止めない。止めるとデプロイの成否と
        # 通知の成否が絡み合って原因が分かりにくくなる
        sys.stderr.write(f"::warning::Slack通知に失敗しました（HTTP {exc.code}）\n")
        return 0
    except urllib.error.URLError as exc:
        sys.stderr.write(f"::warning::Slack通知に失敗しました: {exc.reason}\n")
        return 0

    if body.strip() != "ok":
        sys.stderr.write(f"::warning::Slackの応答が想定外です: {body[:200]}\n")
        return 0

    sys.stdout.write("Slackへ通知しました\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
