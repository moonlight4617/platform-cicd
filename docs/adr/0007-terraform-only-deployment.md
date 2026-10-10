# ADR-0007: アプリのデプロイもTerraformに統一する

- **状態:** 承認済み
- **日付:** 2026-09-30
- **関連:** ADR-0005, ADR-0012

## 背景

ECSやLambdaは、インフラ（サービス定義、IAM、ネットワーク）と成果物（イメージ、zip）の
両方で構成される。このとき「タスク定義を誰が更新するか」が問題になる。

Terraformでタスク定義を管理しつつ、デプロイを `aws ecs update-service` などで行うと、
両者が同じリソースを互いに上書きし合う。次回の `terraform plan` に常に差分が出る状態
（恒久的なドリフト）になり、ドリフト検知（ADR-0012）が機能しなくなる。

## 決定

**アプリのデプロイもTerraformに統一する。** イメージのdigestをvarとして渡す。

```
CI:  docker build → ECRへpush（タグ = commit SHA）→ digestを取得
CD:  terraform apply -var="image_digest=sha256:..."
     → タスク定義の新リビジョン作成 → サービス更新 → 安定するまで待機
```

- **ECS:** `aws_ecs_task_definition` のimageをdigestで指定し、`aws_ecs_service` を更新する。
  **`deployment_circuit_breaker` を有効にし、`rollback = true` とする。**
- **Lambda:** zipをS3に置き、`s3_key` と `source_code_hash` をvarで渡す。
- **state構成:** アプリ×環境ごとに分離する。

  ```
  s3://<tfstate-bucket>/<service>/<app>/<env>/terraform.tfstate
  ```

  ロックはS3のネイティブロック（`use_lockfile`）を使い、DynamoDBのロックテーブルは作らない。
- 環境ごとの差分は `env/dev.tfvars` などのtfvarsファイルで表現する。
  `workspace` は使わない。

## 理由

- **リソースの所有者を1つにしないと、恒久的なドリフトが発生する。** デプロイツールと
  Terraformが同じタスク定義を書き換える構成では、`terraform plan` の差分が常態化し、
  「本当のドリフト」と「デプロイによる差分」を区別できなくなる。ドリフト検知を
  要件に含めている以上、所有者の一元化は必須。
- **デプロイとインフラ変更が1つの操作になるため、承認ゲートを通す対象が1つに絞れる。**
  デプロイ経路が2つあると、承認を通すべき操作も2つになる。
- **`terraform apply` はECSサービスの安定を待つため、デプロイ完了の判定を自前で書かなくてよい。**
  ただし失敗時の挙動は明示的に決める必要があるため、サーキットブレーカーによる
  自動ロールバックを必須とする（applyのタイムアウトを待つのではなく、ECS側で戻す）。
- **stateをアプリ×環境で分けるのは、影響範囲を閉じるため。** 1つのstateに全環境を入れると、
  devの変更のplanにprodのリソースが含まれ、誤applyの危険が生じる。
- **workspaceを使わない理由は、環境の切り替えがCLIの状態（暗黙の値）になるため。**
  誤ったworkspaceでのapplyを防ぐには、backendのキーとtfvarsを明示的に指定する方が安全。

## 検討した代替案

| 案 | 内容 | 却下理由 |
|---|---|---|
| インフラはTerraform、デプロイは `aws ecs update-service` | タスク定義を `ignore_changes` で除外 | ドリフト検知が機能しなくなる。承認対象の経路が2つになる |
| CodeDeployのBlue/Green | ECSのB/Gデプロイ | 実験の範囲を超える。TerraformとCodeDeployの責務分割が複雑 |
| Terraform workspace で環境分離 | `terraform workspace select prod` | 環境がCLIの暗黙状態になり、誤applyの危険がある |
| 1つのstateに全環境 | 環境をモジュールで分ける | devのplanにprodが含まれる。影響範囲が閉じない |

## 結果とトレードオフ

**得たもの**

- リソースの所有者が一元化され、ドリフト検知が意味を持つ
- デプロイ経路が1つに絞られ、承認ゲートの対象が明確
- デプロイ完了判定を自作しなくてよい

**諦めたもの・新たな負担**

- **アプリのデプロイにTerraformの実行権限が必要になる。** デプロイのためにIAM・ネットワークまで
  変更できるロールを使うことになり、権限が必要以上に広い。本来なら
  「デプロイだけできる権限」に絞りたいが、この構成では分離できない。
- **デプロイの所要時間がTerraformのplan/apply分だけ長くなる。** イメージのタグ更新だけの
  変更でも、stateの読み込みと全リソースの差分計算が走る。
- **ロールバックがTerraformの再適用（過去のdigestでapply）になる。** デプロイツール特有の
  「1つ前に戻す」操作がないため、戻す対象のdigestを記録から引く必要がある（ADR-0010）。
- S3ネイティブロックはTerraform 1.10以降が必要。バージョンを固定して運用する。

## 見直しの契機

- デプロイの所要時間が問題になったとき
- デプロイ権限とインフラ変更権限の分離が要件になったとき
  → その時点でデプロイのみ別経路にし、`ignore_changes` とドリフト検知の除外設定を
    セットで導入する
- Blue/Greenやカナリアリリースが要件になったとき
