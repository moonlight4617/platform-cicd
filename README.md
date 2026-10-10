# platform-cicd

全アプリ共通のCI/CD実装。各アプリリポジトリはここを呼び出すだけで、
処理は一切持たない（ADR-0001）。

設計判断の背景は [docs/adr](docs/adr/) を参照。

## 目的

各アプリチームごとのデプロイ精度のバラツキをなくすこと。
そのために「**アプリ側が処理を書けない（書かなくていい）構造**」にしている。
アプリ側に書かせるのは宣言だけで、実装差が生まれる余地をなくす。

## アプリ側に必要なもの

### 1. `.platform/app.yaml`（必須）

```yaml
service: serviceA
app: appA
runtime: java          # java / python / typescript / none
deploy: ecs            # ecs / lambda / infra
build_per_env: false   # ADR-0005 の例外。true にする場合は理由をコメントで残す
terraform_dir: terraform
```

不備があればCIの最初のジョブで落ちる。

### 2. `.github/workflows/ci.yml`（必須）

```yaml
name: CI
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
  id-token: write
  pull-requests: write
jobs:
  ci:
    uses: moonlight4617/platform-cicd/.github/workflows/ci.yml@v1
    secrets: inherit
```

### 3. CDのワークフロー（必須）

いずれも共通ワークフローを呼ぶだけ。詳細は
[serviceA-appA/.github/workflows](../serviceA-appA/.github/workflows/) を参照。

| ファイル | 契機 | 承認 |
|---|---|---|
| `build-artifact.yml` | mainマージ | なし（デプロイはしない） |
| `deploy-dev.yml` | 手動実行 | なし |
| `release-stage.yml` | 手動実行 | 1段 |
| `release-prod.yml` | 手動実行 | 3段 |
| `emergency-release.yml` | 手動実行 | 1段（SRE） |
| `drift-detection.yml` | 定期実行 | なし |

### 4. `.github/workflows/collect-logs.yml`（必須）

実行ログをS3へ格納する。GitHub Actionsのログは実行中のジョブからは
取得できないため、別ワークフローが必要（ADR-0009）。

```yaml
name: Collect logs
on:
  workflow_run:
    workflows: [CI]
    types: [completed]
permissions:
  contents: read
  actions: read
  id-token: write
jobs:
  collect:
    uses: moonlight4617/platform-cicd/.github/workflows/collect-logs.yml@v1
    secrets: inherit
```

### 5. `.platform/security-exceptions.yaml`（任意）

脆弱性の抑制。**理由と有効期限が必須**で、期限切れはそれ自体がCIの停止条件になる。

```yaml
exceptions:
  - id: CVE-2025-12345
    reason: 該当機能を使用していないため影響なし
    expires: 2026-12-31      # 最長180日先まで
    approved_by: sre
    paths: ["build.gradle.kts"]   # 任意
```

## アプリ側が守る必要がある規約

共通ワークフローがこれらを前提にしているため、守らないとCIが失敗する。

| 対象 | 規約 | 理由 |
|---|---|---|
| Java | `build.gradle` で SpotBugs の SARIF 出力を有効にする | 判定はSARIFのみを読む（ADR-0008） |
| TypeScript | `@microsoft/eslint-formatter-sarif` を devDependencies に入れる | 同上 |
| Python | 追加設定は不要（ruffを共通側で導入する） | — |
| Terraform | 環境別の値は `env/<env>.tfvars` に置く | workspaceを使わない（ADR-0007） |
| 全体 | リソース名を `<service>-<app>-<env>-*` にする | IAMの名前パターン制限（ADR-0006） |
| 全体 | 全リソースに `Environment` タグを付ける | IAMのタグ条件とガードレール |
| ECS | タスク定義の登録時にタグを付ける | `RegisterTaskDefinition` はリソース制限に非対応 |
| ALB | リスナールールの優先度はSSMの値を使う | 自前採番すると3環境で衝突する（ADR-0013） |

詳細は [platform-infra/README.md](../platform-infra/README.md) を参照。

## 必要なGitHub Secrets

`platform-infra` の `terraform output -json github_secrets` で取得できる。

| 名前 | 用途 |
|---|---|
| `AWS_REGION` | リージョン |
| `TFSTATE_BUCKET` | Terraform state のバケット |
| `REPORTS_BUCKET` | レポート用バケット |
| `ARTIFACTS_BUCKET` | Lambda成果物用バケット |
| `AWS_ROLE_ARN_CI` | CI（plan・レポート格納）用ロール |
| `AWS_ROLE_ARN_DEV` / `_STAGE` / `_PROD` | デプロイ用ロール |
| `RELEASES_TABLE` / `TICKETS_TABLE` / `APPROVAL_AUDIT_TABLE` | DynamoDB |
| `SLACK_WEBHOOK_URL` | Slack通知（ADR-0015） |

**ADR-0002により、これらの値はリポジトリにコミットしない。**

## 必要なGitHub Environments

| 環境 | 承認者 | 用途 |
|---|---|---|
| `ci` | なし | PRのplanとレポート格納。**AWSアクセスを環境で固定するため必須** |
| `dev` | なし | dev環境デプロイ |
| `stage` | dev_lead | stage環境デプロイ |
| `prod-approval-1` | dev_lead | prod 1段目 |
| `prod-approval-2` | qa_po | prod 2段目 |
| `prod-approval-3` | sre | prod 3段目 |
| `prod` | なし | prod環境デプロイ実行 |
| `prod-emergency` | sre | 緊急リリース（ADR-0011） |

**「自分のデプロイを自分でレビューさせない」設定はOFFにする。**
承認者が1アカウントのため（ADR-0003のトレードオフ）。実務では必ずON。

## 構成

```
platform-cicd/
├── .github/workflows/
│   ├── ci.yml                 共通CI（workflow_call）
│   ├── build-artifact.yml     成果物のビルド（mainマージ時。デプロイはしない）
│   ├── deploy-dev.yml         devへのデプロイ（手動・サンドボックス）
│   ├── release-stage.yml      stageへのリリース（承認1段）
│   ├── release-prod.yml       本番リリース（承認3段）
│   ├── emergency-release.yml  緊急リリース（承認1段・SRE）
│   ├── drift-detection.yml    ドリフト検知（定期実行）
│   ├── collect-logs.yml       実行ログのS3格納（workflow_run から呼ぶ）
│   └── self-test.yml          この基盤自身のCI
├── actions/
│   ├── aws-auth/            マスク + OIDCでのロール引き受け
│   ├── app-config/          app.yaml の読み込みと検証
│   ├── scan/                静的解析・脆弱性診断（SARIF出力）
│   ├── gate/                ゲート判定
│   ├── terraform-plan/      plan と要約
│   ├── terraform-apply/     apply
│   ├── build-image/         イメージのビルドとECRへのpush
│   ├── release-resolve/     commit SHA ↔ digest の解決
│   ├── release-precheck/    リリースの事前チェック（FR-33）
│   ├── release-scan-gate/   CI結果の参照 + イメージ再スキャン
│   ├── release-record/      リリース記録（DynamoDB）
│   ├── record-approval/     承認イベントの記録
│   ├── postmortem-check/    事後報告の未完了チェック
│   ├── drift-issue/         ドリフトのIssue作成
│   ├── upload-reports/      privateなS3へ格納（Kindタグ付与）
│   ├── pr-comment/          伏せ字処理のうえPRへコメント
│   └── notify-slack/        Incoming Webhook通知
├── config/
│   ├── policy.yaml          ゲート基準（アプリ側から変更不可）
│   ├── approvers.yaml       承認ペルソナとSlack IDの対応
│   └── tool-versions.env    スキャンツールのバージョン固定
├── scripts/
│   ├── gate.py              SARIF集約とゲート判定
│   ├── release_store.py     リリース記録と事前チェック
│   ├── trivy_to_sarif.py    TrivyのJSON → SARIF（FixedVersionを保持）
│   ├── read_app_config.py   app.yaml の検証
│   ├── summarize_plan.py    planの要約（機密を含めない）
│   ├── redact.py            伏せ字処理
│   └── notify_slack.py      Slack通知
├── tests/
│   ├── test_gate.py         ゲート判定のテスト（23項目）
│   ├── test_release_store.py リリース判定のテスト（12項目）
│   ├── check_yaml.py        YAML構文チェック
│   ├── check_config.py      設定の整合性チェック
│   └── fixtures/            テスト用データ
└── docs/
    ├── requirements.md      要件定義書
    ├── design.md            設計書
    └── adr/                 設計判断の記録
```

## 設計上の要点

### なぜSARIFに統一するのか

ツールごとに出力形式が違うと、判定ロジックがツールの数だけ増え、
ツール追加のたびに基盤の改修が必要になる（ADR-0008）。

ただしTrivyの組み込みSARIF出力は `FixedVersion` を構造化して残さないため、
ゲート基準（「CRITICAL/HIGH **かつ修正版が提供されているもの**」）を判定できない。
そのため `scripts/trivy_to_sarif.py` で自前変換し、`properties.fixedVersion` を
保持した状態でSARIFにしている。下流はSARIFだけを見れば済む。

### なぜ「修正版がないもの」で止めないのか

修正手段がないままリリースが永久に止まるのを防ぐため（ADR-0008）。
検出自体はレポートに残る。

ただしSASTの検出（Semgrepなど）には修正版の概念がないため、
判定不能なものは停止条件に含める（`policy.yaml` の `fix_availability.unknown: block`）。
コードを直す以外に解決手段がないため。

### なぜ失敗を「問題なし」として通さないのか

スキャンが失敗した、SARIFが壊れている、レポートが生成されなかった —
これらを通過として扱うと、**検査していないことに気づけない**。
各ステップでレポートの存在を確認し、なければ明示的に失敗させている。

### publicリポジトリでの機密の扱い

| 出力先 | 扱い |
|---|---|
| Actionsログ | 公開される。`::add-mask::` で伏せる（aws-auth actionが強制） |
| Artifact | **公開される。レポートもplan全文も置かない** |
| PRコメント | 公開される。要約のみ。`redact.py` を通す |
| private S3 | 全文を格納。閲覧にはAWSの権限が必要 |

## ローカルでのテスト

```bash
pip install pyyaml

python3 tests/test_gate.py           # ゲート判定（23項目）
python3 tests/test_release_store.py  # リリース判定（12項目）
python3 tests/check_yaml.py          # YAML構文
python3 tests/check_config.py        # 設定の整合性
```

## 運用コマンド（CLI）

```bash
export RELEASES_TABLE=... TICKETS_TABLE=... APPROVAL_AUDIT_TABLE=...

# 「今devに何が入っているか」（FR-54）
python3 scripts/release_store.py latest --service serviceA --app appA --env dev

# 事後報告が未完了の緊急リリースを確認する
python3 scripts/release_store.py unreported --service serviceA --app appA

# 事後報告を登録する（これをしないと通常prodリリースがブロックされ続ける）
python3 scripts/release_store.py postmortem \
  --release-id 'serviceA#appA#prod#...' \
  --cause '原因' --permanent-fix '恒久対応' --prevention '再発防止'
```

## 未解決の論点

| # | 内容 | 現状 |
|---|---|---|
| 1 | スキャンツールのバージョンが未固定 | `config/tool-versions.env` が空。初回CI実行後に実際のバージョンを書き込む。固定しないと「昨日は通ったのに今日落ちた」の原因がコードかツールか判別できない |
| 2 | 抑制の有効期限切れを事前に通知する仕組みがない | 期限切れの当日に、無関係な変更のCIが落ちる（ADR-0008のトレードオフ） |
| 3 | サードパーティactionをタグ参照している | `aws-actions/configure-aws-credentials@v4` など。サプライチェーンを厳格にするならcommit SHA固定が必要 |
| 4 | 共通ワークフローの `v1` タグ運用方針が未定 | 破壊的変更時の `v2` 移行手順（ADR-0001の未解決事項） |
| 5 | 事後報告が未完了のまま報告者が不在の場合の例外経路 | 未決（ADR-0011）。手動で記録を追加する経路を作ると昇格判定の抜け道になる |
| 6 | 承認の監査記録とリリース記録の突き合わせがrun_id経由 | `release_id` はデプロイ記録の作成時に確定するため、承認時点では確定していない |
