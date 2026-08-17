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
```

各依頼は1ファイル1YAML（`board/<direction>/{タイムスタンプ}_{ランダムID}.yaml`）。処理が完了したファイルは削除する（履歴はgitのコミット履歴に残る）。

```yaml
from: claude            # 依頼元。user-to-agent なら "user"、agent-to-user なら claude/codex 等
type: approval_request  # 自由文字列（task / decision_request / approval_request など）
title: "Deploy to prod?"
body: |
  詳細本文
related_links:          # 任意
  - https://example.com
created_at: "2026-08-15T09:30:00Z"
```

## CLI使用例

```
# 依頼を追加
python board.py add --direction agent-to-user --from claude --type approval_request \
  --title "Deploy to prod?" --body "詳細をここに書く"

# 未処理の依頼を一覧表示
python board.py list --direction agent-to-user

# 処理済みにする（ファイルを削除）
python board.py complete <filename>
```

`board`のルートは既定でこのリポジトリ直下の`board/`。環境変数`HUMAN_AGENT_BOARD_ROOT`で変更できる（テスト用）。

Claude Code等から呼び出すためのスキルは別リポジトリで管理している。

## 通知（任意）

`agent-to-user`への追加時、環境変数`LINE_CHANNEL_ACCESS_TOKEN`・`LINE_NOTIFY_USER_ID`が設定されていれば、その項目をLINEへpush通知する（`related_links`があればボタン付きテンプレート、無ければ通常テキスト）。未設定なら何もしない（オプトイン、CLI自体の動作には影響しない）。通知の送信に失敗しても`add`コマンド自体は成功する。

ボタンからの承認/却下postbackの受信（`user-to-agent`への書き込み）は本リポジトリの責務ではなく、公開HTTPSエンドポイントを持つ別サービス側（例: `ai-gateway`）で実装する。

## テスト

```
pytest
```

設計・実装の背景は https://github.com/fezzlk/pico の `projects/human-agent-board.md` を参照。
