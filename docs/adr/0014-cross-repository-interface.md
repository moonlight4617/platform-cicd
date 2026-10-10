# ADR-0014: リポジトリ間のインターフェースはSSM Parameter Storeとする

- **状態:** 承認済み
- **日付:** 2026-09-30
- **関連:** ADR-0001, ADR-0007, ADR-0013

## 背景

1アプリ1リポジトリ（ADR-0001）としたため、Terraformのstateがリポジトリ単位で分割される。
一方で、アプリは共通資材の情報を必要とする。

- `platform-infra` が持つもの: VPC ID、サブネットID、ALBのリスナーARN、
  ECSクラスター名、ECRリポジトリURL
- `serviceA-infra`（infraA）が持つもの: appA/B/Cで共通のリソース
  （共通のセキュリティグループ、共有のRDS、SNSトピック等）
- 各アプリが必要とするもの: 上記すべて

この値の受け渡し方法を決める必要がある。

## 決定

**共通資材はSSM Parameter Storeに出力し、アプリ側はそれを読む。**

### 命名規約

```
/platform/<env>/vpc_id
/platform/<env>/app_subnet_ids
/platform/<env>/app_security_group_id
/platform/<env>/alb_listener_arn
/platform/<env>/ecs_cluster_name
/platform/<env>/<service>-<app>/path_prefix
/platform/<env>/<service>-<app>/listener_rule_priority
/<service>/<env>/shared_sg_id
/<service>/<env>/db_endpoint
```

サブネットのパラメータ名は `app_subnet_ids` とする。ADR-0013でパブリックサブネットを
採用したため、`private_subnet_ids` のような配置を含む名前にすると、
実務移行でプライベートサブネットへ変えたときに名前と実体が食い違う。

`<service>-<app>/listener_rule_priority` は、ALB 1台を共有する構成（ADR-0013）で
優先度の衝突を防ぐため、platform-infra側で採番した値を配布するもの。
アプリ側が自分で採番すると必ず衝突する。

### 書き込み側

`platform-infra` と `serviceA-infra` が `aws_ssm_parameter` で出力する。

```hcl
resource "aws_ssm_parameter" "vpc_id" {
  name  = "/platform/${var.env}/vpc_id"
  type  = "String"
  value = aws_vpc.this.id
}
```

### 読み取り側

アプリ側のTerraformは `data "aws_ssm_parameter"` で読む。

```hcl
data "aws_ssm_parameter" "vpc_id" {
  name = "/platform/${var.env}/vpc_id"
}
```

- **`terraform_remote_state` は使わない。**
- **パイプライン上の依存関係は作らない。** platform-infraのデプロイ完了を
  アプリのCDが待つ、といった連携は行わない。
- 秘密情報はSecureStringとし、アプリの実行時はECSタスク定義の `secrets` で注入する。

### デプロイ順序

1. `platform-infra`（VPC, ALB, ECRなど）
2. `serviceA-infra`（infraA）
3. 各アプリ

この順序は **初回構築時のみ**必要。以降は各リポジトリが独立してデプロイできる。
参照先のパラメータが存在しない場合、アプリのplanが失敗することで検知される。

## 理由

- **`terraform_remote_state` はstateファイルを直接読むため、リポジトリ間の結合が強くなる。**
  読み取り側が書き込み側のstateの場所と内部構造を知ることになり、
  書き込み側のリファクタリング（モジュール構成の変更など）が読み取り側を壊す。
  SSMなら「パラメータ名」という明示的な契約だけが公開され、内部構造は隠蔽される。
- **`terraform_remote_state` は読み取り側にstateバケットへの読み取り権限を要求する。**
  アプリのデプロイロールがplatform-infraのstateを読めるようになり、
  そこには他の機密情報も含まれる。権限の粒度としてSSMの方が細かく絞れる。
- **パイプライン上の依存を作らないのは、リポジトリ横断のデプロイ順序制御が
  複雑さの主要な発生源だから。** 「platform-infraの変更を待ってからアプリをデプロイ」を
  自動化すると、待ち合わせの失敗・タイムアウト・部分適用の扱いが必要になる。
  パラメータが存在しなければplanが失敗するという形で、順序の問題は検知できる。
- SSM Parameter Storeは標準ティアなら無料で、実験のコストに影響しない。

## 検討した代替案

| 案 | 内容 | 却下理由 |
|---|---|---|
| `terraform_remote_state` | 他のstateを直接参照 | リポジトリ間の結合が強い。stateバケットへの読み取り権限が必要 |
| Terraform Cloudのworkspace間連携 | 出力の共有機能を使う | Terraform Cloudを使わない前提。ツール依存が増える |
| タグやリソース名からdata sourceで検索 | `data "aws_vpc"` にフィルタを付ける | 命名やタグの一致に暗黙依存する。名前変更で壊れる |
| 値をtfvarsに直書き | 各アプリが値を持つ | 変更時に全リポジトリの修正が必要。値の重複が発生 |
| パイプラインでデプロイ順序を制御 | リポジトリ間の待ち合わせ | 待ち合わせの失敗処理が複雑。得られるものが少ない |

## 結果とトレードオフ

**得たもの**

- リポジトリ間の契約がパラメータ名という明示的な形になる
- 書き込み側の内部構造を変えても読み取り側が壊れない
- 各リポジトリが独立してデプロイできる
- 権限を必要な範囲に絞れる

**諦めたもの・新たな負担**

- **パラメータ名の規約が暗黙の契約になる。** 名前を変更すると読み取り側が壊れるため、
  実質的にパラメータ名はAPIとして扱う必要がある。しかし型チェックもコンパイル時の
  検証もないため、**壊れるのはapply時（あるいはplan時）**になる。
  どのアプリがどのパラメータを参照しているかを一覧する手段がない。
- **値がSSMとstateの2箇所に存在する。** 書き込み側でリソースを作り直したとき、
  SSMの更新とアプリ側の読み取りにタイミングのずれが生じうる。
- **初回構築の順序が人間の手順として残る。** 自動化していないため、
  順序を知らない人が構築すると失敗する。READMEへの明記が必要。
- SSM Parameter Storeのスロットリング（標準ティア）に注意が必要。
  全アプリのplanが同時に走ると制限に当たる可能性がある。

## 見直しの契機

- パラメータの参照関係が追えなくなったとき
  → 参照一覧を生成する仕組み、またはパラメータの命名を検証するCIを追加する
- SSMのスロットリングが実際に発生したとき → 高度ティアへの変更、または取得のキャッシュ
- 共通資材の変更でアプリが壊れる事故が頻発したとき
  → 契約の変更に対する互換性維持の運用ルールを定める
