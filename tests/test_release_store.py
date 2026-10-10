#!/usr/bin/env python3
"""リリース記録と事前チェックのテスト（ADR-0010, ADR-0011, FR-33）.

ここで検証するのは「止まるべきリリースが止まるか」である。
誤ると次のどちらかが起きる。

    - stageを通っていない成果物がprodに出る（FR-33の破れ）
    - 正しいリリースが止まる

DynamoDBには接続せず、query_app を差し替えて判定ロジックだけを検証する。

    python3 tests/test_release_store.py
"""

from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

# boto3 が無い環境でも判定ロジックを検証できるようにする
try:
    import boto3  # noqa: F401
except ImportError:  # pragma: no cover
    fake = types.ModuleType("boto3")
    fake.resource = lambda *a, **k: None  # type: ignore[attr-defined]
    sys.modules["boto3"] = fake
    exc = types.ModuleType("botocore.exceptions")

    class _Err(Exception):
        pass

    exc.BotoCoreError = _Err  # type: ignore[attr-defined]
    exc.ClientError = _Err  # type: ignore[attr-defined]
    sys.modules["botocore"] = types.ModuleType("botocore")
    sys.modules["botocore.exceptions"] = exc

import release_store  # noqa: E402

failures: list[str] = []

DIGEST_OK = "sha256:" + "a" * 64
DIGEST_NO_BUILD = "sha256:" + "b" * 64
DIGEST_DEV_ONLY = "sha256:" + "c" * 64


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        failures.append(name)


def fake_records() -> list[dict]:
    """GSIは新しい順に返すため、新しいものを先頭に並べる."""
    return [
        # devへのデプロイ（昇格の根拠にはならない）
        {
            "kind": "deploy",
            "env": "dev",
            "image_digest": DIGEST_DEV_ONLY,
            "result": "success",
            "deployed_at": "2026-10-09T10:00:00Z",
            "branch": "feature/x",
            "actor": "dev1",
        },
        # stageで成功したデプロイ
        {
            "kind": "deploy",
            "env": "stage",
            "image_digest": DIGEST_OK,
            "result": "success",
            "deployed_at": "2026-10-08T10:00:00Z",
            "actor": "lead",
        },
        # main由来の成果物
        {
            "kind": "build",
            "env": "build",
            "image_digest": DIGEST_OK,
            "commit_sha": "a1b2c3d4e5f6",
            "image_tag": "a1b2c3d4e5f6",
            "source_ref": "refs/heads/main",
            "deployed_at": "2026-10-08T09:00:00Z",
        },
        # main以外から作られた成果物（リリース不可）
        {
            "kind": "build",
            "env": "build",
            "image_digest": DIGEST_DEV_ONLY,
            "commit_sha": "ffffffffffff",
            "source_ref": "refs/heads/feature/x",
            "deployed_at": "2026-10-09T09:00:00Z",
        },
    ]


def run_precheck(env: str, digest: str, emergency: bool = False) -> int:
    import argparse

    args = argparse.Namespace(
        service="serviceA",
        app="appA",
        env=env,
        image_digest=digest,
        emergency=emergency,
        summary_out="",
    )
    return release_store.cmd_precheck(args)


def main() -> int:
    records = fake_records()
    release_store.query_app = lambda service, app, **kw: records  # type: ignore[assignment]

    print("FR-33: stageを通っていない成果物をprodへ出せないこと")

    check(
        "stage成功済みのdigestはprodへ出せる",
        run_precheck("prod", DIGEST_OK) == 0,
    )
    check(
        "**devでのみ成功したdigestはprodへ出せない**（devは昇格チェーン外）",
        run_precheck("prod", DIGEST_DEV_ONLY) != 0,
        "devでの成功を昇格の根拠にしてはいけない（ADR-0004）",
    )
    check(
        "成果物の記録がないdigestはリリースできない",
        run_precheck("prod", DIGEST_NO_BUILD) != 0,
    )
    check(
        "main以外から作られた成果物はstageへも出せない",
        run_precheck("stage", DIGEST_DEV_ONLY) != 0,
    )
    check(
        "main由来の成果物はstageへ出せる",
        run_precheck("stage", DIGEST_OK) == 0,
    )

    print()
    print("ADR-0011: 未報告の緊急リリースがあると通常リリースがブロックされること")

    with_emergency = records + [
        {
            "kind": "deploy",
            "env": "prod",
            "image_digest": "sha256:" + "d" * 64,
            "result": "success",
            "deployed_at": "2026-10-09T23:00:00Z",
            "emergency": True,
            "postmortem_filed": False,
            "incident_id": "INC-001",
            "release_id": "serviceA#appA#prod#2026-10-09T23:00:00Z#1",
            "actor": "sre1",
        }
    ]
    release_store.query_app = lambda service, app, **kw: with_emergency  # type: ignore[assignment]

    check(
        "未報告の緊急リリースがあると通常prodリリースが止まる",
        run_precheck("prod", DIGEST_OK) != 0,
        "催促は無視できるが、ブロックは無視できない（ADR-0011）",
    )
    check(
        "未報告があってもstageリリースは止まる（同一アプリの通常リリース）",
        run_precheck("stage", DIGEST_OK) != 0,
    )
    check(
        "緊急リリース経路は未報告があっても使える（障害の連鎖時に復旧手段を封じない）",
        run_precheck("prod", DIGEST_OK, emergency=True) == 0,
    )

    reported = [
        dict(r, postmortem_filed=True) if r.get("emergency") else r for r in with_emergency
    ]
    release_store.query_app = lambda service, app, **kw: reported  # type: ignore[assignment]
    check(
        "事後報告を登録すると通常リリースが再開できる",
        run_precheck("prod", DIGEST_OK) == 0,
    )

    print()
    print("未報告の一覧（催促に使う）")

    release_store.query_app = lambda service, app, **kw: with_emergency  # type: ignore[assignment]
    unreported = release_store.find_unreported_emergencies("serviceA", "appA")
    check("未報告を1件検出", len(unreported) == 1, str(len(unreported)))
    check("インシデントIDを保持している", unreported[0].get("incident_id") == "INC-001")

    print()
    print("対象の解決（commit SHA ↔ digest）")

    release_store.query_app = lambda service, app, **kw: records  # type: ignore[assignment]
    build = release_store.find_build_by_commit("serviceA", "appA", "a1b2c3d4e5f6")
    check("commit SHAからdigestを引ける", build is not None and build["image_digest"] == DIGEST_OK)
    check(
        "存在しないcommitでは引けない",
        release_store.find_build_by_commit("serviceA", "appA", "deadbeef") is None,
    )

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
