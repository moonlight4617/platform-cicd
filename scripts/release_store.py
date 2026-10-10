#!/usr/bin/env python3
"""リリース記録の読み書き（ADR-0010）.

このモジュールが扱う記録は2種類ある。

    kind="build"  : mainマージ時に作られた成果物の記録。
                    「この digest が main 由来である」ことの根拠になる。
    kind="deploy" : 各環境へのデプロイの記録。

昇格チェーンは main → stage → prod であり、**devは含まない**（ADR-0004の補足）。
devへのデプロイも記録するが、昇格判定には使わない。
devはサンドボックスなので「devで動いた」ことはprodで動くことの根拠にならない。

書き込みは3回までリトライし、失敗した場合は異常終了する（ADR-0010の補足）。
記録のない成功を作ると、次のprodリリースが原因不明のまま止まるため。

サブコマンド:
    record-build      成果物の記録を作る（mainマージ時）
    record-deploy     デプロイの記録を作る
    record-approval   承認イベントを追記する
    precheck          リリースの事前チェック（FR-33）
    latest            指定アプリ・環境の最新リリースを表示（FR-54）
    postmortem        緊急リリースの事後報告を登録する（FR-35）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from typing import Any

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover
    sys.stderr.write("boto3 が必要です: pip install boto3\n")
    raise SystemExit(2)


WRITE_ATTEMPTS = 3
PROMOTION_SOURCE_REF = "refs/heads/main"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def app_key(service: str, app: str) -> str:
    return f"{service}#{app}"


def _table(env_var: str):
    name = os.environ.get(env_var, "").strip()
    if not name:
        raise SystemExit(f"環境変数 {env_var} が未設定です")
    return boto3.resource("dynamodb").Table(name)


def releases():
    return _table("RELEASES_TABLE")


def tickets():
    return _table("TICKETS_TABLE")


def approval_audit():
    return _table("APPROVAL_AUDIT_TABLE")


# ---------------------------------------------------------------------------
# 書き込み
# ---------------------------------------------------------------------------


def put_with_retry(table, item: dict[str, Any], what: str) -> None:
    """ADR-0010: 書き込み失敗はデプロイの失敗として扱う.

    記録が欠けた状態を成功として通すと、次のprodリリースで
    「stageを通っていない」と判定され、原因不明のまま止まる。
    その時点では、なぜ記録がないのかを追う手がかりが残っていない。
    """
    last_error: Exception | None = None

    for attempt in range(1, WRITE_ATTEMPTS + 1):
        try:
            table.put_item(Item=item)
            sys.stdout.write(f"{what} を記録しました（{attempt}回目で成功）\n")
            return
        except (ClientError, BotoCoreError) as exc:
            last_error = exc
            sys.stderr.write(f"::warning::{what} の記録に失敗しました（{attempt}/{WRITE_ATTEMPTS}）: {exc}\n")
            if attempt < WRITE_ATTEMPTS:
                time.sleep(2**attempt)

    sys.stderr.write(
        "::error::リリース記録の書き込みに失敗しました。"
        "AWSリソースは更新されている可能性がありますが、記録のない成功を作らないため"
        "ジョブを失敗させます（ADR-0010）\n"
    )
    raise SystemExit(f"記録の書き込みに失敗: {last_error}")


def cmd_record_build(args: argparse.Namespace) -> int:
    created_at = now_iso()
    item = {
        "release_id": f"{args.service}#{args.app}#build#{created_at}#{args.run_id}",
        "kind": "build",
        "app_key": app_key(args.service, args.app),
        "service": args.service,
        "app": args.app,
        "env": "build",
        "commit_sha": args.commit_sha,
        "image_digest": args.image_digest,
        "image_tag": args.image_tag,
        # 「main由来か」の判定に使う（FR-33）
        "source_ref": args.source_ref,
        "result": "success",
        "deployed_at": created_at,
        "actor": args.actor,
        "run_id": args.run_id,
        "run_url": args.run_url,
        "scan_report_s3": args.scan_report_s3,
    }
    put_with_retry(releases(), item, "成果物")
    return 0


def cmd_record_deploy(args: argparse.Namespace) -> int:
    deployed_at = now_iso()
    item = {
        "release_id": f"{args.service}#{args.app}#{args.env}#{deployed_at}#{args.run_id}",
        "kind": "deploy",
        "app_key": app_key(args.service, args.app),
        "service": args.service,
        "app": args.app,
        "env": args.env,
        "commit_sha": args.commit_sha,
        "image_digest": args.image_digest,
        "branch": args.branch,
        "result": args.result,
        "deployed_at": deployed_at,
        "actor": args.actor,
        "run_id": args.run_id,
        "run_url": args.run_url,
        "scan_report_s3": args.scan_report_s3,
        "ticket_ids": [t for t in (args.ticket_ids or "").split(",") if t.strip()],
        "git_tag": args.git_tag,
        "emergency": args.emergency,
        # 緊急リリースのみ事後報告の対象。通常リリースは最初から報告済み扱い
        "postmortem_filed": not args.emergency,
        "emergency_reason": args.emergency_reason,
        "incident_id": args.incident_id,
        "approvals": json.loads(args.approvals) if args.approvals else [],
    }
    put_with_retry(releases(), item, f"{args.env} へのデプロイ")

    # チケットへの紐づけ（FR-30）
    for ticket_id in item["ticket_ids"]:
        _link_ticket(ticket_id, item)

    return 0


def _link_ticket(ticket_id: str, release: dict[str, Any]) -> None:
    """擬似チケットにリリース情報を記録する（FR-30）.

    commit SHA・タグ・digestの3つすべてを残す。それぞれ用途が違うため
    （digestは同一性の保証、SHAはソースへの到達、タグは人間が読む識別子）。
    """
    table = tickets()
    entry = {
        "release_id": release["release_id"],
        "env": release["env"],
        "commit_sha": release["commit_sha"],
        "image_digest": release["image_digest"],
        "git_tag": release["git_tag"],
        "deployed_at": release["deployed_at"],
    }

    for attempt in range(1, WRITE_ATTEMPTS + 1):
        try:
            table.update_item(
                Key={"ticket_id": ticket_id},
                UpdateExpression=(
                    "SET releases = list_append(if_not_exists(releases, :empty), :entry), "
                    "updated_at = :now, "
                    "title = if_not_exists(title, :title), "
                    "#st = if_not_exists(#st, :status)"
                ),
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":empty": [],
                    ":entry": [entry],
                    ":now": now_iso(),
                    ":title": f"(自動作成) {ticket_id}",
                    ":status": "open",
                },
            )
            sys.stdout.write(f"チケット {ticket_id} にリリースを紐づけました\n")
            return
        except (ClientError, BotoCoreError) as exc:
            sys.stderr.write(f"::warning::チケット {ticket_id} の更新に失敗（{attempt}/{WRITE_ATTEMPTS}）: {exc}\n")
            if attempt < WRITE_ATTEMPTS:
                time.sleep(2**attempt)

    raise SystemExit(f"チケット {ticket_id} の更新に失敗しました")


def cmd_record_approval(args: argparse.Namespace) -> int:
    item = {
        "release_id": args.release_id,
        "occurred_at": now_iso(),
        "gate": args.gate,
        "persona": args.persona,
        "github_user": args.github_user,
        "event": args.event,
        "run_id": args.run_id,
    }
    put_with_retry(approval_audit(), item, f"承認イベント（{args.gate}）")
    return 0


def cmd_postmortem(args: argparse.Namespace) -> int:
    """緊急リリースの事後報告を登録する（FR-35）.

    報告されるまで、同一アプリの通常prodリリースはブロックされる。
    """
    for attempt in range(1, WRITE_ATTEMPTS + 1):
        try:
            releases().update_item(
                Key={"release_id": args.release_id},
                UpdateExpression=(
                    "SET postmortem_filed = :true, postmortem = :body, postmortem_at = :now"
                ),
                ConditionExpression="attribute_exists(release_id) AND emergency = :true",
                ExpressionAttributeValues={
                    ":true": True,
                    ":now": now_iso(),
                    ":body": {
                        "cause": args.cause,
                        "permanent_fix": args.permanent_fix,
                        "prevention": args.prevention,
                        "filed_by": args.filed_by,
                    },
                },
            )
            sys.stdout.write(f"事後報告を登録しました: {args.release_id}\n")
            return 0
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise SystemExit(
                    f"{args.release_id} は存在しないか、緊急リリースではありません"
                )
            sys.stderr.write(f"::warning::事後報告の登録に失敗（{attempt}/{WRITE_ATTEMPTS}）: {exc}\n")
            if attempt < WRITE_ATTEMPTS:
                time.sleep(2**attempt)

    raise SystemExit("事後報告の登録に失敗しました")


# ---------------------------------------------------------------------------
# 読み取り
# ---------------------------------------------------------------------------


def query_app(service: str, app: str, **kwargs) -> list[dict[str, Any]]:
    """GSI app-deployed-index でアプリの記録を新しい順に取得する."""
    from boto3.dynamodb.conditions import Key

    table = releases()
    items: list[dict[str, Any]] = []
    start_key = None

    while True:
        params: dict[str, Any] = {
            "IndexName": "app-deployed-index",
            "KeyConditionExpression": Key("app_key").eq(app_key(service, app)),
            "ScanIndexForward": False,
            **kwargs,
        }
        if start_key:
            params["ExclusiveStartKey"] = start_key

        res = table.query(**params)
        items.extend(res.get("Items", []))
        start_key = res.get("LastEvaluatedKey")
        if not start_key:
            break

    return items


def find_build(service: str, app: str, image_digest: str) -> dict[str, Any] | None:
    for item in query_app(service, app):
        if item.get("kind") == "build" and item.get("image_digest") == image_digest:
            return item
    return None


def find_build_by_commit(service: str, app: str, commit_sha: str) -> dict[str, Any] | None:
    """commit SHA から成果物の記録を引く.

    同じcommitを複数回ビルドした場合は最新のものを返す
    （GSIが新しい順に返すため先頭が最新）。
    """
    for item in query_app(service, app):
        if item.get("kind") == "build" and item.get("commit_sha") == commit_sha:
            return item
    return None


def cmd_resolve(args: argparse.Namespace) -> int:
    """リリース対象を解決する.

    commit SHA と digest のどちらかを指定すれば、もう一方を成果物の記録から引く。
    どちらも成果物の記録に存在しない場合は失敗させる。
    mainマージ時にビルドされた成果物以外はリリースできない（FR-33）。
    """
    build: dict[str, Any] | None = None

    if args.image_digest:
        build = find_build(args.service, args.app, args.image_digest)
        if build is None:
            raise SystemExit(
                f"digest {args.image_digest} の成果物記録が見つかりません。"
                "mainマージ時にビルドされた成果物のみリリースできます（FR-33）"
            )
    elif args.commit_sha:
        build = find_build_by_commit(args.service, args.app, args.commit_sha)
        if build is None:
            raise SystemExit(
                f"commit {args.commit_sha} の成果物記録が見つかりません。"
                "mainにマージされ、Build artifact ワークフローが成功している必要があります"
            )
    else:
        raise SystemExit("--commit-sha または --image-digest のどちらかを指定してください")

    resolved = {
        "commit_sha": build.get("commit_sha"),
        "image_digest": build.get("image_digest"),
        "image_tag": build.get("image_tag"),
        "source_ref": build.get("source_ref"),
        "built_at": build.get("deployed_at"),
    }

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            for key, value in resolved.items():
                fh.write(f"{key.replace('_', '-')}={value or ''}\n")

    json.dump(resolved, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


def find_successful_deploy(
    service: str, app: str, env: str, image_digest: str
) -> dict[str, Any] | None:
    for item in query_app(service, app):
        if (
            item.get("kind") == "deploy"
            and item.get("env") == env
            and item.get("image_digest") == image_digest
            and item.get("result") == "success"
        ):
            return item
    return None


def find_unreported_emergencies(service: str, app: str) -> list[dict[str, Any]]:
    return [
        item
        for item in query_app(service, app)
        if item.get("kind") == "deploy"
        and item.get("emergency") is True
        and item.get("postmortem_filed") is not True
    ]


def cmd_digest_for_plan(args: argparse.Namespace) -> int:
    """CIのplanに渡すdigestを決める.

    なぜ必要か:
        アプリ側のTerraformは stage / prod で image_digest を必須にしている
        （ADR-0005を変数のvalidationで強制）。CIのplanはデプロイではないため
        digestを持たないが、何も渡さないとvalidationでplanが失敗する。

        また適当な固定値を渡すと、デプロイ済みのdigestと食い違い、
        planに毎回タスク定義の差分が出る。plan差分の意味が薄れる。

    決め方（上から順に採用）:
        1. その環境に現在デプロイされている成果物のdigest（差分が出ない）
        2. 最新の成果物のdigest（まだデプロイされていない環境向け）
        3. 空（devはvalidationが無いため問題ない）
    """
    records = query_app(args.service, args.app)

    digest = ""
    source = "none"

    for item in records:
        if (
            item.get("kind") == "deploy"
            and item.get("env") == args.env
            and item.get("result") == "success"
            and item.get("image_digest")
        ):
            digest = str(item["image_digest"])
            source = f"{args.env} にデプロイ済みの成果物"
            break

    if not digest:
        for item in records:
            if item.get("kind") == "build" and item.get("image_digest"):
                digest = str(item["image_digest"])
                source = "最新の成果物（この環境には未デプロイ）"
                break

    # Lambdaの成果物はzipのハッシュなので、ECSのdigest形式でない場合は渡さない
    if digest and not digest.startswith("sha256:"):
        digest = ""
        source = "形式が合わないため渡さない"

    var_arg = f"-var=image_digest={digest}" if digest else ""

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(f"image-digest={digest}\n")
            fh.write(f"var-arg={var_arg}\n")

    sys.stdout.write(f"planに渡すdigest: {digest or '(なし)'} / 根拠: {source}\n")
    return 0


def cmd_unreported(args: argparse.Namespace) -> int:
    """事後報告が未完了の緊急リリースを一覧する（ADR-0011）.

    1件以上あれば終了コード1を返す。催促の判定に使う。
    通常prodリリースのブロックは precheck 側で行う。
    """
    items = find_unreported_emergencies(args.service, args.app)

    if not items:
        sys.stdout.write("未報告の緊急リリースはありません\n")
        return 0

    lines = [f"## 事後報告が未完了の緊急リリース: {len(items)} 件", ""]
    lines.append("| release_id | インシデント | 実行者 | 日時 |")
    lines.append("|---|---|---|---|")
    for item in items:
        lines.append(
            f"| `{item.get('release_id')}` | {item.get('incident_id', '-')} "
            f"| {item.get('actor', '-')} | {item.get('deployed_at', '-')} |"
        )
    lines.append("")
    lines.append("**このアプリの通常prodリリースはブロックされています。**")
    lines.append("`release_store.py postmortem --release-id <id> ...` で報告してください。")

    summary = "\n".join(lines) + "\n"
    sys.stdout.write(summary)

    if args.summary_out:
        with open(args.summary_out, "w", encoding="utf-8") as fh:
            fh.write(summary)

    return 1


def cmd_latest(args: argparse.Namespace) -> int:
    """「今この環境に何が入っているか」を表示する（FR-54）."""
    for item in query_app(args.service, args.app):
        if item.get("kind") == "deploy" and item.get("env") == args.env:
            out = {
                k: item.get(k)
                for k in (
                    "release_id",
                    "env",
                    "branch",
                    "commit_sha",
                    "image_digest",
                    "result",
                    "actor",
                    "deployed_at",
                    "git_tag",
                    "emergency",
                )
            }
            json.dump(out, sys.stdout, ensure_ascii=False, indent=2, default=str)
            sys.stdout.write("\n")
            return 0

    sys.stdout.write(f"{args.service}/{args.app} の {args.env} へのデプロイ記録はありません\n")
    return 0


# ---------------------------------------------------------------------------
# 事前チェック（FR-33）
# ---------------------------------------------------------------------------


def cmd_precheck(args: argparse.Namespace) -> int:
    """リリース前の検証。1つでも満たさなければ停止する.

    - 指定されたdigestがmain由来であること
    - prodの場合: 同一digestがstageで成功していること（昇格の保証）
    - 未報告の緊急リリースがないこと（事後報告の強制）
    """
    checks: list[dict[str, Any]] = []

    # --- 1. main由来か ---
    build = find_build(args.service, args.app, args.image_digest)
    if build is None:
        checks.append(
            {
                "name": "main由来の成果物であること",
                "passed": False,
                "detail": (
                    "この digest の成果物記録が見つかりません。"
                    "mainマージ時にビルドされた成果物のみリリースできます（FR-33）"
                ),
            }
        )
    elif build.get("source_ref") != PROMOTION_SOURCE_REF:
        checks.append(
            {
                "name": "main由来の成果物であること",
                "passed": False,
                "detail": f"成果物の出自が main ではありません: {build.get('source_ref')}",
            }
        )
    else:
        checks.append(
            {
                "name": "main由来の成果物であること",
                "passed": True,
                "detail": f"commit {str(build.get('commit_sha'))[:12]} / ビルド {build.get('deployed_at')}",
            }
        )

    # --- 2. prodならstageで成功済みか ---
    if args.env == "prod":
        stage = find_successful_deploy(args.service, args.app, "stage", args.image_digest)
        if stage is None:
            checks.append(
                {
                    "name": "同一成果物がstageで成功していること",
                    "passed": False,
                    "detail": (
                        "この digest の stage への成功記録がありません。"
                        "stageを飛ばしたリリースはできません。"
                        "なおdevは昇格チェーンに含まれないため、devでの成功は根拠になりません（ADR-0004）"
                    ),
                }
            )
        else:
            checks.append(
                {
                    "name": "同一成果物がstageで成功していること",
                    "passed": True,
                    "detail": f"stage: {stage.get('deployed_at')} by {stage.get('actor')}",
                }
            )

    # --- 3. 未報告の緊急リリースがないか ---
    if args.env in ("stage", "prod") and not args.emergency:
        unreported = find_unreported_emergencies(args.service, args.app)
        if unreported:
            ids = ", ".join(str(u.get("release_id")) for u in unreported[:5])
            checks.append(
                {
                    "name": "未報告の緊急リリースがないこと",
                    "passed": False,
                    "detail": (
                        f"事後報告が未完了の緊急リリースが {len(unreported)} 件あります: {ids}。"
                        "scripts/release_store.py postmortem で報告してください（ADR-0011）"
                    ),
                }
            )
        else:
            checks.append(
                {"name": "未報告の緊急リリースがないこと", "passed": True, "detail": "なし"}
            )

    failed = [c for c in checks if not c["passed"]]

    # ジョブサマリに出す。ログを掘らずに停止理由が分かるようにする
    lines = ["## リリース事前チェック", ""]
    lines.append("| 項目 | 結果 | 内容 |")
    lines.append("|---|---|---|")
    for c in checks:
        mark = "通過" if c["passed"] else "**停止**"
        lines.append(f"| {c['name']} | {mark} | {c['detail']} |")
    summary = "\n".join(lines) + "\n"

    if args.summary_out:
        with open(args.summary_out, "w", encoding="utf-8") as fh:
            fh.write(summary)

    sys.stdout.write(summary)

    if failed:
        for c in failed:
            sys.stderr.write(f"::error::事前チェック失敗: {c['name']} — {c['detail']}\n")
        return 1

    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="リリース記録の読み書き（ADR-0010）")
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--service", required=True)
        p.add_argument("--app", required=True)

    def meta(p: argparse.ArgumentParser) -> None:
        p.add_argument("--actor", default=os.environ.get("GITHUB_ACTOR", "unknown"))
        p.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", "local"))
        p.add_argument("--run-url", default="")
        p.add_argument("--scan-report-s3", default="")

    p = sub.add_parser("record-build", help="成果物の記録を作る")
    common(p)
    meta(p)
    p.add_argument("--commit-sha", required=True)
    p.add_argument("--image-digest", required=True)
    p.add_argument("--image-tag", default="")
    p.add_argument("--source-ref", default=os.environ.get("GITHUB_REF", ""))
    p.set_defaults(func=cmd_record_build)

    p = sub.add_parser("record-deploy", help="デプロイの記録を作る")
    common(p)
    meta(p)
    p.add_argument("--env", required=True, choices=["dev", "stage", "prod"])
    p.add_argument("--commit-sha", required=True)
    p.add_argument("--image-digest", default="")
    p.add_argument("--branch", default="")
    p.add_argument("--result", default="success", choices=["success", "failed", "rolled_back"])
    p.add_argument("--ticket-ids", default="")
    p.add_argument("--git-tag", default="")
    p.add_argument("--emergency", action="store_true")
    p.add_argument("--emergency-reason", default="")
    p.add_argument("--incident-id", default="")
    p.add_argument("--approvals", default="", help="JSON配列")
    p.set_defaults(func=cmd_record_deploy)

    p = sub.add_parser("record-approval", help="承認イベントを追記する")
    meta(p)
    p.add_argument("--release-id", required=True)
    p.add_argument("--gate", required=True)
    p.add_argument("--persona", default="")
    p.add_argument("--github-user", default="")
    p.add_argument("--event", default="approved", choices=["approved", "rejected", "timeout"])
    p.set_defaults(func=cmd_record_approval)

    p = sub.add_parser("resolve", help="commit SHA と digest を相互に解決する")
    common(p)
    p.add_argument("--commit-sha", default="")
    p.add_argument("--image-digest", default="")
    p.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT", ""))
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("precheck", help="リリースの事前チェック")
    common(p)
    p.add_argument("--env", required=True, choices=["stage", "prod"])
    p.add_argument("--image-digest", required=True)
    p.add_argument("--emergency", action="store_true")
    p.add_argument("--summary-out", default="")
    p.set_defaults(func=cmd_precheck)

    p = sub.add_parser("digest-for-plan", help="CIのplanに渡すdigestを決める")
    common(p)
    p.add_argument("--env", required=True, choices=["dev", "stage", "prod"])
    p.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT", ""))
    p.set_defaults(func=cmd_digest_for_plan)

    p = sub.add_parser("unreported", help="事後報告が未完了の緊急リリースを一覧する")
    common(p)
    p.add_argument("--summary-out", default="")
    p.set_defaults(func=cmd_unreported)

    p = sub.add_parser("latest", help="最新のデプロイを表示する")
    common(p)
    p.add_argument("--env", required=True, choices=["dev", "stage", "prod"])
    p.set_defaults(func=cmd_latest)

    p = sub.add_parser("postmortem", help="緊急リリースの事後報告を登録する")
    p.add_argument("--release-id", required=True)
    p.add_argument("--cause", required=True, help="原因")
    p.add_argument("--permanent-fix", required=True, help="恒久対応")
    p.add_argument("--prevention", required=True, help="再発防止")
    p.add_argument("--filed-by", default=os.environ.get("GITHUB_ACTOR", "unknown"))
    p.set_defaults(func=cmd_postmortem)

    return ap


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
