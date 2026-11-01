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
  runs/current/      # エージェント実行の開始・heartbeat
  runs/history/      # 正常終了・異常終了した実行履歴
  vm/events.jsonl    # ai-gateway fallback VMの作成・削除履歴
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

# LINE等のUI向けに、判断待ち・依頼・通知・作業状況をJSONで取得
python board.py dashboard --json --recent 5

# 1件の内容を取得
python board.py get <filename> --json

# 判断要求へ回答し、元の要求を処理済みにする
python board.py respond <filename> --decision approval
python board.py respond <filename> --decision rejection

# 処理済みにする（ファイルを削除）
python board.py complete <filename>

# kobitoの現在状態と直近5件の完了・失敗を表示
python board.py status list --source kobito --recent 5

# kobito自体の生存状態・停止疑い・連続失敗を表示
python board.py run list --source kobito --recent 5

# Claude/Codexの利用量履歴・タスク別差分を取得
python board.py usage dashboard --hours 168

# ai-gateway fallback VMの直近10件の操作履歴を取得
python board.py vm list --recent 10
```

`ai-gateway`の`spin-up-fallback-vm.sh`と`teardown-fallback-vm.sh`は、成功・失敗を`vm record`で自動記録する。これはスクリプト経由の操作のみを対象とし、GCP Consoleや直接実行した`gcloud`の操作は記録しない。

Claudeは`scripts/claude_usage_statusline.py`をClaude CodeのstatusLineに設定すると、セッション中に5時間・7日枠を記録する。Codexは`scripts/collect_codex_usage.py`が公式app-serverプロトコルから現在値を記録する。`HUMAN_AGENT_WORK_ID=FEZ-123`を設定して実行するとissue単位の差分集計対象になる。Claudeの無操作期間など取得できない時間は推測で補完しない。

macOSでは次でClaude statuslineと15分間隔のCodex収集LaunchAgentを設定する。既存のClaude設定は保持し、`statusLine`だけを追加・更新する。

```bash
python3 scripts/install_usage_collectors.py
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.fezzlk.human-agent-board-usage.plist
```

`board`のルートは既定でこのリポジトリ直下の`board/`。環境変数`HUMAN_AGENT_BOARD_ROOT`で変更できる（テスト用）。

Claude Code等から呼び出すためのスキルは別リポジトリで管理している。

## 通知（任意）

### Project別の通知と受け付け

`add`と`status set`に`--project-id`を指定するか、実行環境の`KOBITO_PROJECT_ID`を継承すると、項目にProjectの所属が付く。その環境での`list`・`get`・`complete`・status一覧は他Projectを扱わない。無指定のCLI `list`はProject付き項目を表示しないため、従来の全体workerが新しいProjectの置き手紙を拾うことを防ぐ。全体の管理用一覧は明示的に`list --all-projects`を使う。

`HUMAN_AGENT_NOTIFICATION_BACKEND=command`と`HUMAN_AGENT_NOTIFICATION_COMMAND`を設定すると、通知は指定コマンドへJSONを標準入力で渡す。シェルを介して実行せず、本文をコマンド引数に埋め込まない。JSONは`{"item": {...}, "filename": "...yaml"}`。`line`が従来の既定で、`none`は通知を停止する。commandに失敗しても項目は保持し、LINEへ重複送信しない。

command通知は`state/notifications/`へ保存する。`notifications`で未送信JSONの一覧を取得し、配信成功後に`notification-complete <id>`で確認できる。kobitoの受付が定期的に再送するため、接続中断時にも計画・状態報告を失わない。Projectの対応表やSlackトークンはこのリポジトリへ保存しない。

Slack向けの実装はkobito側の`project_slack.py notify`が担当する。`--thread-ts`は元の会話スレッドを引き継ぐための値。`--external-id`は受信イベントの重複抑止用で、項目がcompleteされた後もreceiptを保持する。

Slackの承認は`respond --via Slack --project-id ... --expected-digest ... --response-id ...`を使い、現在の計画との一致を確認する。承認・却下・置き手紙の更新はプロセスロックとreceiptで直列化する。古い計画、別Project、同じIDで異なる判断を渡した操作は拒否する。詳細な導入手順はkobitoの`SLACK_SETUP.md`を参照。

### 従来のLINE通知

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
同じ`source`・`work-id`で直前と同じ`state`を再記録した場合は、履歴は保存するがLINE通知は重複送信しない。

現在状態と直近履歴は次で確認できる。

```bash
python board.py status list --source kobito --recent 5
```

現在状態はエージェントが更新するリアルタイム表示用のスナップショットであり、タスク・優先度・正式な完了状態の正本はLinear等のissueトラッカーとする。

## 実行監視

issue単位の`status`とは別に、定期実行そのものを`run`で記録する。`ai-gateway`はkobitoの受付時に`start`、実行中は60秒ごとに`heartbeat`、終了時に`finish`を自動実行する。

```bash
python board.py run start --source kobito --trigger cloud-scheduler
python board.py run heartbeat --source kobito --run-id <run-id> --phase working
python board.py run finish --source kobito --run-id <run-id> --outcome completed
python board.py run list --source kobito --json
```

既定ではheartbeatが15分以上途絶えた実行を`stale`、最終確認が4時間以上前なら`missing`と判定する。`completed`・`no_work`は正常、`failed`・`blocked`は異常終了として履歴と連続失敗回数を表示する。

同一障害のユーザー対応依頼には`add --dedupe-key <key>`を付ける。未解決の同じkeyがある場合は新規通知せず内容と更新時刻だけを更新する。復旧時は`resolve --direction agent-to-user --dedupe-key <key>`で解消する。

## テスト

```
pytest
```

設計・実装の背景は https://github.com/fezzlk/pico の `projects/human-agent-board.md` を参照。
