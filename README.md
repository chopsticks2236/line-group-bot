# LINEグループ BOT（日付通知 + AI返答）

シンプルな **Python + Flask** のBOTです。

| 機能 | 内容 |
|------|------|
| **日付送信** | 毎月 **18日** … 出勤可能日の連絡依頼 / **月末** … 給与明細の不備確認 |
| **AI返答** | グループの会話ログを踏まえて回答（メンション or `/q 質問`） |
| **管理** | 文面は `messages.json` だけ編集すればOK |

AIキーがなくても **日付の自動送信は動きます**。AIは後から追加できます。

---

## フォルダ構成

```
line-group-bot/
  app.py              … 本体（Webhook + Cron）
  messages.json       … ★送る文面・AI設定（ここを主に編集）
  .env                … 秘密情報（自分で作る・Gitに上げない）
  .env.example        … 設定の見本
  bot/
    config.py         … 設定読み込み
    db.py             … 会話ログ（SQLite）
    line_api.py       … LINE送受信
    ai_reply.py       … AI返答
    scheduler.py      … 日付判定
  requirements.txt
  Procfile            … クラウド起動用
```

---

## 1. LINE Developers の準備

1. [LINE Developers](https://developers.line.biz/console/) にログイン  
2. プロバイダー作成 → **Messaging API** チャネル作成  
3. **Messaging API** タブで  
   - **Channel access token** を発行してコピー  
   - **Channel secret** をコピー  
4. 応答設定  
   - Webhook: **ON**  
   - あいさつ・自動応答メッセージ: **OFF** 推奨（二重送信防止）  
5. LINEアプリで公式アカウントを **グループに招待**

---

## 2. このPCで動かしてみる（動作確認）

```powershell
cd C:\Users\Owner\Projects\line-group-bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

`.env` をメモ帳で開き、最低限これを埋める:

```
LINE_CHANNEL_ACCESS_TOKEN=（コピーしたトークン）
LINE_CHANNEL_SECRET=（コピーしたシークレット）
CRON_SECRET=好きな合言葉（例: my-secret-1234）
```

起動:

```powershell
python app.py
```

ブラウザで `http://127.0.0.1:8000/` を開き、`"ok": true` ならOK。

### グループIDの取り方

1. BOTをグループに入れる  
2. グループで何か発言する  
3. 起動中のターミナルに  
   `>>> LINE_GROUP_ID に次を設定: Cxxxx...`  
   と出る  
4. その `C...` を `.env` の `LINE_GROUP_ID=` に貼る  
5. アプリを再起動  

### 日付送信のテスト（実際に送る）

```powershell
# 18日扱いでテスト
curl "http://127.0.0.1:8000/cron/daily?secret=あなたのCRON_SECRET&date=2026-04-18"

# 月末扱いでテスト（4月なら30日）
curl "http://127.0.0.1:8000/cron/daily?secret=あなたのCRON_SECRET&date=2026-04-30"
```

同じ月にもう一度送らないよう記録されます。

---

## 3. クラウドに置く（おすすめ・24時間）

PCを消しても動かすには、**Render** などが簡単です。

### Render の例

1. GitHub にこのフォルダを上げる（`.env` は上げない）  
2. [render.com](https://render.com) で **Web Service** 新規作成  
3. 設定例  
   - Runtime: Python  
   - Build: `pip install -r requirements.txt`  
   - Start: `gunicorn -b 0.0.0.0:$PORT app:app`  
4. Environment に `.env` の値を全部入れる  
5. デプロイ後のURL（例: `https://xxxx.onrender.com`）を控える  

### LINE の Webhook URL

LINE Developers → Messaging API → Webhook URL:

```
https://xxxx.onrender.com/callback
```

「検証」が成功すればOK。

### 毎日自動で日付チェック（Cron）

Renderの無料枠にCronが無い場合は [cron-job.org](https://cron-job.org) が簡単です。

- URL: `https://xxxx.onrender.com/cron/daily?secret=あなたのCRON_SECRET`  
- スケジュール: 毎日 **0 0 9 * * ***（日本時間 9:00）  
- タイムゾーン: Asia/Tokyo  

BOTが毎朝「今日は18日？月末？」を見て、該当すれば1通送ります。

---

## 4. 文面の編集（いちばん触る場所）

`messages.json` を編集します。

### 毎月18日（出勤可能日）

```json
{
  "id": "shift_availability_18",
  "day": 18,
  "text": "ここに好きな文章"
}
```

### 月末（給与明細の確認）

```json
{
  "id": "payslip_check_month_end",
  "day": "last",
  "text": "ここに好きな文章"
}
```

- `day: 18` … その月の18日  
- `day: "last"` … その月の最終日（28 / 30 / 31 を自動判定）  

クラウドでは **再デプロイ** すると文面が反映されます。

---

## 5. AI返答（キーは後からでOK）

### キーの取り方

1. [https://console.x.ai](https://console.x.ai) でアカウント作成  
2. APIキーを発行  
3. `.env`（または Render の Environment）に:

```
XAI_API_KEY=xai-...
XAI_MODEL=grok-4.5
```

### 使い方（グループ内）

- BOTを **メンション** して質問  
- または `/q シフトの提出期限は？`  

直近の会話ログを読んで答えます。  
キー未設定時は「キーを設定してください」と返します。

`messages.json` の `ai` で調整:

```json
"ai": {
  "reply_only_when_mentioned": true,
  "max_log_messages": 40,
  "system_prompt": "..."
}
```

- `true` … メンション / `/q` のときだけ返す（グループが荒れて迷惑になりにくい）  
- `false` … 「？」で終わる文などにも返す  

---

## 6. よくあるつまずき

| 症状 | 確認 |
|------|------|
| 日付が送られない | `LINE_GROUP_ID`・トークン・Cron URL の secret |
| Webhook検証失敗 | URLが `/callback` か、Channel secret が合っているか |
| 二重に返事が来る | LINE公式の「応答メッセージ」をOFF |
| AIが答えない | `XAI_API_KEY`、メンション or `/q` しているか |
| 同じ日に2回送られた | 通常はDBで防止。`data/` が消えると再送される（無料プランの再起動に注意） |

永続化を強くしたい場合は、Render の **Disk** を `data` にマウントするか、後からDBをクラウド用に変更できます。

---

## 7. ローカルだけで日付送信だけ試す

Webhookなしでも、トークンとグループIDがあれば:

```powershell
python -c "from bot.scheduler import run_daily_schedules; from datetime import date; print(run_daily_schedules(date(2026,4,18)))"
```

---

## まとめ

1. `messages.json` … 文面の管理  
2. `.env` … LINE / AI / 合言葉  
3. 毎朝 Cron が `/cron/daily` を叩く → 18日・月末に送信  
4. 普段の質問はメンション or `/q` → ログを見てAIが回答  

困ったら `http://あなたのURL/` の JSON（`line_configured` / `ai_configured` / `group_id_set`）を見て設定漏れを確認してください。
