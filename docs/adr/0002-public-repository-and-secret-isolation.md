# ADR-0002: リポジトリをpublicにし、機密値をリポジトリ外に隔離する

- **状態:** 承認済み
- **日付:** 2026-09-30
- **関連:** ADR-0003, ADR-0009

## 背景

実験は個人のGitHubアカウント（`moonlight4617`）・Free planで行う。
一方で「AWSアカウントIDがリポジトリ上で見えることは許容しない」という要件がある。

ここでGitHubのプラン制約により次の衝突が発生した。

| リポジトリ | プラン | Environmentsの承認者必須設定 |
|---|---|---|
| public | Free | **使える** |
| private | Free | 使えない（環境そのものが使えない） |
| private | Pro（月4ドル） | 環境は使えるが承認者必須は使えない |
| private | Enterprise | 使える |

承認ゲートは本実験の中心テーマである。privateにすると、有料プランに移行しても
標準機能では承認ゲートを構成できない。

## 決定

**リポジトリはpublicとし、機密値を1つもリポジトリに置かない設計にする。**

具体的な対策は以下の5点。すべて実装必須とする。

1. **アカウントID・ロールARN・S3バケット名はGitHub Secretsに置く。**
   publicリポジトリでもSecretsの値は公開されず、フォークからのPRには渡らない。

   ```yaml
   role-to-assume: ${{ secrets.AWS_ROLE_ARN_PROD }}
   ```

2. **Terraformのコードにアカウントを書かない。**
   必要な箇所は `data.aws_caller_identity.current.account_id` で動的に取得する。
   backendのバケット名は `-backend-config` でSecretsから渡し、tfファイルには残さない。

3. **各ジョブの先頭でアカウントIDをマスクする。**

   ```yaml
   - run: echo "::add-mask::${{ secrets.AWS_ACCOUNT_ID }}"
   ```

4. **terraform planの全文・スキャンレポート・実行ログはprivate S3にのみ置く（ADR-0009）。**
   publicリポジトリではActionsの実行ログもArtifactも誰でも閲覧・ダウンロードできるため、
   ここが最大の漏洩経路。PRコメントには件数などの要約とS3へのリンクのみを載せ、
   出力前にARNパターンを伏せるフィルタを通す。

5. **ローカル開発用の値はgit管理外にする。**
   `local/terraform.tfvars` を `.gitignore` に入れ、リポジトリには
   `terraform.tfvars.example` のみを置く。

さらにリポジトリ設定として以下を有効化する。

- Actionsの「外部コントリビューターのワークフロー実行に承認を要求する」
- Secret scanning、Push protection（publicリポジトリでは無料）
- CIジョブを「シークレット不要な検査（lint/SAST/SCA）」と「AWSに触る処理（terraform plan）」に
  分割し、フォークからのPRでは後者をスキップする

## 理由

- **承認ゲート・CodeQL・Actions実行時間無制限という、実験に必要な要素がすべて揃う。**
  privateのFree planでは承認ゲートが作れず、Actions実行時間も月2000分に制限される。
  承認待ちのポーリングを実装する場合（ADR-0003の代替案）は、この制限が現実的な障害になる。
- **「機密をリポジトリに置かない」はprivateでも守るべき原則。** この隔離設計は実務で
  internalリポジトリに移行してもそのまま正しい設計として残る。publicという制約が、
  結果的に正しい設計を強制している。
- Push protectionとSecret scanningがpublicで無料で使えるため、うっかりコミットに対する
  防御はprivate（Free plan）より厚くなる。

## 検討した代替案

| 案 | 内容 | 却下理由 |
|---|---|---|
| privateにして承認を自作 | Slack webhook + GitHub Issueコメント or DynamoDBをポーリング | 実装量が増え、承認待ちでActions時間を消費する。GitHubの承認履歴も使えない。実験の主目的が実装作業に埋もれる |
| GitHub Proに課金してprivate | 月4ドル | **承認者必須設定はEnterpriseが必要**なので問題が解決しない |
| platform-cicdのみpublic、アプリはprivate | 機密のないものだけ公開 | 承認ゲートを持つのはアプリ側のCDなので解決にならない |

## 結果とトレードオフ

**得たもの**

- 標準機能による承認ゲート、CodeQL、実行時間無制限
- 機密をリポジトリに置かない設計が強制される

**諦めたもの・新たな負担**

- **設定を1か所ミスするとログに出て公開される。** 対策3（マスク）と対策4（S3隔離）が
  二重の保険だが、リスクはゼロにはならない。特に新しいジョブを追加するときにマスク処理を
  忘れる危険がある。将来的にはcomposite actionの中で必ずマスクを通す形にして、
  忘れられない構造にすべき。
- terraform planの内容をPRコメントで気軽に確認できない。レビュー体験が落ちる。
- ソースコード自体が公開される。実験用のサンプルアプリなので問題ないが、実務への適用時は
  internalリポジトリ + GitHub Enterpriseが前提になる。

## 見直しの契機

- 実務適用が決まり、GitHub Enterprise（またはinternalリポジトリ）が使えるようになったとき
  → privateに切り替える。対策1〜5はそのまま維持する
- ログへの機密漏洩が実際に発生したとき → マスクを構造的に強制する仕組みへ変更
