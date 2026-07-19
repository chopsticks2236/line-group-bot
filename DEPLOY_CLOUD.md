# クラウド運用（PC・バッチ不要）

PCをつけなくても、**毎月15日** と **月末** にかめ子がグループへ送ります。

```
LINEグループ
    ↑ 送信
Render（BOT常駐）
    ↑ 毎日1回チェック
cron-job.org（無料の目覚まし）
```

---

## 全体の流れ（所要 20〜40分）

1. GitHub にコードを上げる  
2. Render に Webサービスを作る  
3. 環境変数（トークンなど）を入れる  
4. LINE の Webhook を Render のURLに変更  
5. cron-job.org で毎朝チェックを登録  

---

## 1. GitHub に上げる

### 初めての場合

1. [https://github.com](https://github.com) でアカウント作成  
2. **New repository** → 名前例: `line-group-bot` → **Public** で作成（Privateでも可）  
3. ローカルで（PowerShell）:

```powershell
cd C:\Users\Owner\Projects\line-group-bot
git init
git add .
git commit -m "LINE kameko bot for cloud"
git branch -M main
git remote add origin https://github.com/（自分のユーザー名）/line-group-bot.git
git push -u origin main
```

※ `.env` は `.gitignore` 済み。**絶対にGitHubに上げない**（トークン漏洩防止）。

---

## 2. Render で公開する

1. [https://render.com](https://render.com) で無料登録（GitHub連携が楽）  
2. **Dashboard** → **New** → **Web Service**  
3. GitHub の `line-group-bot` を選ぶ  
4. 設定:

| 項目 | 値 |
|------|-----|
| Name | `line-kameko-bot` など |
| Region | Singapore など近い場所 |
| Runtime | Python |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn -b 0.0.0.0:$PORT app:app` |
| Instance | **Free** |

5. **Environment** に次を追加（ローカルの `.env` と同じ値）:

| Key | 値 |
|-----|-----|
| `LINE_CHANNEL_ACCESS_TOKEN` | 長期トークン |
| `LINE_CHANNEL_SECRET` | シークレット |
| `LINE_GROUP_ID` | `C...` のグループID |
| `CRON_SECRET` | 自分で決めた合言葉 |
| `TZ` | `Asia/Tokyo` |

AIは使わないなら `XAI_API_KEY` は空でOK。

6. **Create Web Service** → デプロイ完了を待つ（数分）  
7. 表示されたURLをメモ  

例: `https://line-kameko-bot.onrender.com`

8. ブラウザで開く:

```
https://（あなたのURL）/
```

`"ok": true` と `"group_id_set": true` なら成功。

---

## 3. LINE の Webhook をクラウドに向ける

1. [LINE Developers](https://developers.line.biz/console/)  
2. かめ子のチャネル → **Messaging API設定**  
3. Webhook URL:

```
https://（あなたのRenderのURL）/callback
```

例:

```
https://line-kameko-bot.onrender.com/callback
```

4. **更新** → Webhook **オン** → **検証** 成功  

※ これで **ngrok も PC のバッチも不要** です。

---

## 4. 毎朝の自動チェック（cron-job.org）

Render 無料枠は「眠る」ことがあるので、**外から毎日起こしてチェック**します。

1. [https://cron-job.org](https://cron-job.org) で無料登録  
2. **Create cronjob**  
3. 設定例:

| 項目 | 値 |
|------|-----|
| Title | `kameko daily` |
| URL | `https://（RenderのURL）/cron/daily?secret=（CRON_SECRET）` |
| Schedule | 毎日 **0 0 9 * * ***（= 毎日9:00） |
| Timezone | **Asia/Tokyo** |
| Request method | GET |

4. 保存して有効化  

### 動作

- **毎日9時** にURLが叩かれる  
- 今日が **15日** → シフトメッセージ送信  
- 今日が **月末** → 給与明細メッセージ送信  
- それ以外 → 何も送らず終了  

---

## 5. テスト送信（クラウド経由）

ブラウザ or 別PCから（secret は自分の値）:

```
https://（RenderのURL）/cron/daily?secret=あなたのCRON_SECRET&date=2026-04-15
```

月末テスト:

```
https://（RenderのURL）/cron/daily?secret=あなたのCRON_SECRET&date=2026-04-30
```

`"status": "sent"` とグループに届けばOK。  
同じ月の2回目は `skip_already_sent` になります。

---

## 運用後のイメージ

| やること | PCバッチ | クラウド |
|----------|----------|----------|
| BOT起動 | 毎回 start-line-bot | **不要** |
| ngrok | 必要 | **不要** |
| 15日・月末送信 | 手動 or タスク | **自動** |
| PC電源 | 必要 | **不要** |

文面変更は `messages.json` を直して GitHub に push → Render が自動再デプロイ。

---

## 注意（無料枠）

| こと | 内容 |
|------|------|
| 初回アクセスが遅い | 眠っていると起こすのに十数秒かかることがある |
| 再デプロイ | 送済み記録（SQLite）が消えると、同じ月に再送されることがある |
| 二重送信が気になる | Render の有料＋Disk、または送った月を別管理に変更可能 |

月2通だけなら、まず無料で十分なことが多いです。

---

## トラブル

| 症状 | 確認 |
|------|------|
| `/` が開かない | Render の Deploy ログ・Build 成功か |
| Webhook検証失敗 | URL末尾 `/callback`、Secret、Deploy完了か |
| 日付が送られない | cron URL の secret、`LINE_GROUP_ID`、Renderが落ちていないか |
| 401 | `CRON_SECRET` が Render と cron-job で一致しているか |

---

## チェックリスト

- [ ] GitHub にコードがある（`.env` は含まない）  
- [ ] Render で `"ok": true`  
- [ ] 環境変数4つ以上入っている  
- [ ] LINE Webhook が Render の `/callback`  
- [ ] cron-job.org が毎日9時（JST）  
- [ ] テストURLでグループに届いた  

ここまでできたら **PCオフでも毎月送れます。**
