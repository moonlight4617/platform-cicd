# Architecture Decision Records (ADR)

社内共通CI/CD基盤（実験プロジェクト）における設計判断の記録。

**上位の文書**: [要件定義書](../requirements.md) / [設計書](../design.md)

| 文書 | 役割 |
|---|---|
| 要件定義書 | **何を**作るのか。要件ID（FR / NFR）の定義 |
| 設計書 | **どう**実現するのか。構造と結論 |
| ADR（この配下） | **なぜ**その判断をしたのか。却下した代替案とトレードオフ |

## 目的

「各アプリチームごとのデプロイ精度のバラツキをなくす」ため、プラットフォームの一貫として
全アプリ共通で使えるCI/CDを提供する。その過程で行った判断と、その理由・トレードオフを残す。

判断そのものより **なぜその判断に至ったか、何を捨てたか** を重視して記録する。
前提が変わったときに、どのADRを見直せばよいか分かる状態を保つことが目的。

## 一覧

| # | タイトル | 状態 |
|---|---|---|
| [0001](0001-repository-topology.md) | リポジトリ構成を1アプリ1リポジトリ + 中央リポジトリにする | 承認済み |
| [0002](0002-public-repository-and-secret-isolation.md) | リポジトリをpublicにし、機密値をリポジトリ外に隔離する | 承認済み |
| [0003](0003-approval-gates.md) | 承認ゲートをGitHub Environmentsで実装する | 承認済み |
| [0004](0004-ci-cd-separation.md) | CIとCDを分離し、trunk-based + 手動リリースとする | 承認済み |
| [0005](0005-build-once-promote-artifact.md) | 成果物は1回だけビルドし、環境差は実行時に注入する | 承認済み |
| [0006](0006-aws-auth-and-environment-isolation.md) | AWS認証はOIDC、環境分離は1アカウント内で行う | 承認済み |
| [0007](0007-terraform-only-deployment.md) | アプリのデプロイもTerraformに統一する | 承認済み |
| [0008](0008-static-analysis-and-vulnerability-gates.md) | 静的解析・脆弱性診断のツールとゲート基準 | 承認済み |
| [0009](0009-report-and-log-storage.md) | レポートと実行ログはprivate S3に保存する | 承認済み |
| [0010](0010-dynamodb-state-management.md) | 承認・リリース・擬似チケットの記録はDynamoDBで管理する | 承認済み |
| [0011](0011-emergency-release-path.md) | 緊急リリース経路を設け、事後報告を強制する | 承認済み |
| [0012](0012-terraform-drift-detection.md) | ドリフト検知は定期実行し、対応判断は人間が行う | 承認済み |
| [0013](0013-network-topology.md) | ALB 1台のパス振り分け + パブリックサブネット構成 | 承認済み |
| [0014](0014-cross-repository-interface.md) | リポジトリ間のインターフェースはSSM Parameter Store | 承認済み |
| [0015](0015-slack-notification.md) | Slack通知はIncoming Webhookのみ | 承認済み |
| [0016](0016-ai-assistance-deferred.md) | AI支援機能の方式決定は保留する | 保留 |

## 状態の定義

- **提案中** — 議論中。まだ実装してよい状態ではない
- **承認済み** — この決定に沿って実装する
- **保留** — 判断を意図的に先送りしている。先送りの理由をADRに書く
- **廃止** — 取りやめた。代替のADRがある場合はリンクする
- **置き換え済み** — 後続のADRが上書きした。リンク必須

## 書き方

[template.md](template.md) をコピーして使う。番号は連番、変更が必要になったら
既存ADRを書き換えるのではなく **新しいADRを追加して置き換える**。
過去の判断が間違っていた記録も、判断の履歴として価値がある。
