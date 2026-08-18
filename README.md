# human-agent-board

ユーザーと複数のAIエージェント（Claude, Codex等）の間で、依頼・判断要求をやり取りするための汎用の受け渡し基盤。

- ユーザー → エージェントへの作業依頼
- エージェント → ユーザーへの判断・確認・実行許可の要求

双方向とも、置き場（board）に溜まったものを定期的にポーリングして処理する対称的な構造を想定している。特定の連携先（Linear等）に依存しない、汎用インフラとして設計する。

## セットアップ

```
pip install -r requirements.txt
```

## データ構造

```
board/
  user-to-agent/     # ユーザー → エージェントへの作業依頼
  agent-to-user/     # エージェント → ユーザーへの判断・確認・許可要求
  status/current/    # エージェントのissue単位の現在状態（同じissueは上書き）
  status/history/    # 完了・失敗した作業の直近履歴
```

各依頼は1ファイル1YAML（`board/<direction>/{タイムスタンプ}_{ランダムID}.yaml`）。処理が完了したファイルは削除する（履歴はgitのコミット履歴に残る）。

```yaml
from: claude            # 依頼元。user-to-agent なら "user"、agent-to-user なら claude/codex 等
type: approval_request  # 自由文字列（task / decision_request / approval_request など）
title: "Deploy to prod?"
body: |
  詳細本文
related_links:          # 任意
  - https://github.com/example/project/pull/123
  - https://linear.app/example/issue/EX-123/example
created_at: "2026-08-15T09:30:00Z"
```

## CLI使用例

```
# 依頼を追加
python board.py add --direction agent-to-user --from claude --type approval_request \
  --title "Deploy to prod?" --body "詳細をここに書く" \
  --related-link "https://github.com/example/project/pull/123" \
  --related-link "https://linear.app/example/issue/EX-123/example"

# 未処理の依頼を一覧表示
python board.py list --direction agent-to-user

# 処理済みにする（ファイルを削除）
python board.py complete <filename>

# kobitoの現在状態と直近5件の完了・失敗を表示
python board.py status list --source kobito --recent 5
```

`board`のルートは既定でこのリポジトリ直下の`board/`。環境変数`HUMAN_AGENT_BOARD_ROOT`で変更できる（テスト用）。

Claude Code等から呼び出すためのスキルは別リポジトリで管理している。

## 通知（任意）

`agent-to-user`への追加時、環境変数`LINE_CHANNEL_ACCESS_TOKEN`・`LINE_NOTIFY_USER_ID`が設定されていれば、その項目をLINEへpush通知する。`related_links`があれば承認・却下ボタン付きテンプレートに続けて「判断材料」を送り、GitHub・Linearは種別名付き、それ以外は「関連資料」として全URLを表示する。複数のURLは`--related-link`を繰り返して指定する。承認要求には、変更内容を確認できるGitHub PRまたはdiffと、背景・完了条件を確認できるLinear issueを原則として含める。

`related_links`が無い場合は通常テキストのみを送り、承認・却下ボタンは表示しない。環境変数が未設定なら通知は行わない（オプトイン、CLI自体の動作には影響しない）。通知の送信に失敗しても`add`コマンド自体は成功し、boardへの項目追加は保持される。

ボタンからの承認/却下postbackの受信（`user-to-agent`への書き込み）は本リポジトリの責務ではなく、公開HTTPSエンドポイントを持つ別サービス側（例: `ai-gateway`）で実装する。

## 作業状況

長時間動作するエージェントは、issue単位の状態を`status set`で更新する。

```bash
python board.py status set \
  --source kobito --work-id FEZ-111 --state implementing \
  --title "kobitoの作業状況確認" \
  --summary "boardのstatusコマンドを実装中" \
  --next-action "テストを実行する" \
  --related-link "https://linear.app/example/issue/FEZ-111/example"
```

状態は`waiting`、`researching`、`implementing`、`verifying`、`decision_pending`、`pr_open`、`completed`、`failed`のいずれか。同じ`source`と`work-id`の進行中状態は上書きされるため、古い途中経過が一覧に残らない。`completed`と`failed`は現在状態から取り除かれ、`status/history/`へ保存される。履歴ファイルは自動削除せず保持し、一覧・LINEでは既定で直近5件だけを表示する（`--recent`で表示件数を変更可能）。

`--notify`を付けるとLINEにも状態を送る。状態通知には判断材料リンクを表示するが、承認・却下ボタンは付けない。通知過多を避けるため、通常は`decision_pending`、`pr_open`、`completed`、`failed`などユーザーに意味のある状態遷移だけで指定する。細かな途中経過はboardの状態だけを更新する。

現在状態と直近履歴は次で確認できる。

```bash
python board.py status list --source kobito --recent 5
```

現在状態はエージェントが更新するリアルタイム表示用のスナップショットであり、タスク・優先度・正式な完了状態の正本はLinear等のissueトラッカーとする。

## テスト

```
pytest
```

設計・実装の背景は https://github.com/fezzlk/pico の `projects/human-agent-board.md` を参照。
