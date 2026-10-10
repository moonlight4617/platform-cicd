#!/usr/bin/env python3
"""アプリリポジトリの .platform/app.yaml を読んで検証する（ADR-0001）.

アプリ側の記述を宣言だけに絞る設計のため、この宣言が壊れていると
後続のジョブが「何をすべきか」を決められない。設定の不備は
CIの最初の段階で、原因が分かる形で落とす。

使い方:
    read_app_config.py --path .platform/app.yaml --github-output "$GITHUB_OUTPUT"
"""

from __future__ import annotations

import argparse
import os
import sys

import yaml

VALID_RUNTIMES = {"java", "python", "typescript", "none"}
VALID_DEPLOYS = {"ecs", "lambda", "infra"}


def fail(message: str) -> None:
    # Actionsのアノテーションとして出すことで、どの行が悪いか分かりやすくする
    sys.stderr.write(f"::error file=.platform/app.yaml::{message}\n")
    raise SystemExit(1)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", required=True)
    ap.add_argument("--github-output", default=None)
    args = ap.parse_args(argv)

    if not os.path.exists(args.path):
        fail(
            f"{args.path} がありません。共通CI/CDに乗せるには、このファイルで "
            "service / app / runtime / deploy を宣言する必要があります（ADR-0001）"
        )

    with open(args.path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}

    if not isinstance(doc, dict):
        fail("app.yaml の内容がマップ形式ではありません")

    service = str(doc.get("service") or "").strip()
    app = str(doc.get("app") or "").strip()
    runtime = str(doc.get("runtime") or "").strip().lower()
    deploy = str(doc.get("deploy") or "").strip().lower()
    build_per_env = bool(doc.get("build_per_env", False))
    terraform_dir = str(doc.get("terraform_dir") or "terraform").strip()

    if not service:
        fail("service が未設定です")
    if runtime not in VALID_RUNTIMES:
        fail(f"runtime が不正です: '{runtime}'（指定可能: {', '.join(sorted(VALID_RUNTIMES))}）")
    if deploy not in VALID_DEPLOYS:
        fail(f"deploy が不正です: '{deploy}'（指定可能: {', '.join(sorted(VALID_DEPLOYS))}）")

    if deploy == "infra":
        if app:
            fail("deploy: infra のリポジトリでは app を指定しないでください")
    elif not app:
        fail("app が未設定です")

    # ADR-0005: 例外を使う場合は理由をコメントで残すことを求めているが、
    # コメントは機械的に検証できない。せめて宣言の存在は明示させる
    if build_per_env:
        sys.stderr.write(
            "::warning file=.platform/app.yaml::build_per_env: true が指定されています。"
            "ADR-0005の例外扱いとなり、prodビルドはstage成功済みのcommit SHAからのみ可能です\n"
        )

    name_prefix = f"{service}-{app}" if app else service

    outputs = {
        "service": service,
        "app": app,
        "runtime": runtime,
        "deploy": deploy,
        "build-per-env": "true" if build_per_env else "false",
        "terraform-dir": terraform_dir,
        "name-prefix": name_prefix,
        # ECRのリポジトリ名は小文字のみ許可される（正規表現
        # [a-z0-9]+((\.|_|__|-+)[a-z0-9]+)* を満たす必要がある）。
        # 命名規約の <service>-<app> は大文字を含みうるため、ECR用に小文字化する
        "ecr-repository": name_prefix.lower(),
    }

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            for key, value in outputs.items():
                fh.write(f"{key}={value}\n")

    for key, value in outputs.items():
        sys.stdout.write(f"{key}: {value}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
