# MT5 EA オプティマイザー

MetaTrader 5（MT5）の EA（自動売買プログラム）を、Pythonの**遺伝的アルゴリズム（GA）**で自動最適化するスクリプトです。

---

## どんな仕組みか

```
Python スクリプト
  │
  ├── パラメータを少しランダムに変化させる（変異）
  │
  ├── .set ファイルを生成して MT5 Strategy Tester を自動起動
  │
  ├── バックテスト結果（HTMレポート）をスコアに変換
  │
  └── 高スコアの候補を親にして次の世代へ → これを繰り返す
```

### IS / FW 二段階評価で過学習を防ぐ

- **IS（In-Sample）**：学習期間（例: 2022〜2024年）でのテスト
- **FW（Forward Walk）**：検証期間（例: 2025年）でのテスト

IS でスクリーニングし、**上位候補だけ FW で評価**します。
IS で過学習したパラメータは FW で崩れるため、汎化性能の高い組み合わせだけが残ります。

最終スコア = IS スコア × 0.4 + FW スコア × 0.6

---

## スコアリング方式

| 項目 | 計算式 |
|------|--------|
| 基礎点 | `min(PF, 2.0) × 30 + log(1 + 利益) - DD × 1.5` |
| PF ペナルティ | PF < 1.0 のとき `(1.0 - PF) × 50` 減点 |
| トレード数ペナルティ | 120本未満のとき `(120 - trades) × 0.5` 減点 |
| DD ペナルティ | DD > 20% のとき `(DD - 20) × 3.0` 減点 |

**完全失格なし・ソフトペナルティ方式**：どんな候補も数値として比較できます。

---

## AI（Claude Pro）と組み合わせた改善ループ

```
Phase 0: GA でベースラインのパラメータを探す
   ↓
Round 1: Claude Pro に成績履歴 + MQ5ロジックを読ませてエントリー条件を改善
   ↓
候補ロジックを GA で再評価 → 旧ロジックより良ければ採用
   ↓
Round 2, 3 ... と繰り返す
```

### スキップ・ロールバックの安全設計

失敗しても壊れないよう、以下の動作が自動で行われます。

| ケース | スクリプトの動作 |
|--------|----------------|
| candidate スコアが採用閾値を上回った | 採用 → base ファイルを新しいものに更新 |
| candidate スコアが採用閾値以下だった | **不採用 → 自動で旧 base の MQ5/EX5 に戻す** |
| コンパイルが失敗した | **そのラウンドをスキップ → 旧 base に戻す** |

「採用されなかったら必ず元に戻る」ため、何度試しても動いている EA が壊れることはありません。

---

## どんな EA に向いているか

### そのまま使いやすい EA

以下のような「数値パラメータを調整して性能が変わる」タイプに最も向いています。

- MA 期間・ATR 倍率・閾値・フィルター値など **int / float 中心のパラメータ**
- `.set` ファイルで設定を渡せる標準的な MT5 EA
- PF・DD・トレード数・利益で良し悪しを判断しやすい戦略

**変更箇所は `Params` / `clamp()` / `mutate()` / `write_set_file()` の4か所だけで動きます。**

---

### 追加修正が必要になりやすい EA

| ケース | 対応が必要な箇所 |
|--------|----------------|
| `bool` や `enum` の切り替えが多い | `mutate()` に切り替えロジックを追加 |
| `FastPeriod < SlowPeriod` などの制約が多い | `clamp()` に制約を追記 |
| PF より別指標（シャープレシオ等）を重視したい | `score()` の計算式を変更 |
| 売買回数が極端に少ない戦略 | `score()` のペナルティ閾値を調整 |
| 特定時間帯・複数通貨・特殊な注文ロジックに依存する | `parse()` や評価ロジックを追加 |

---

### そのままでは難しい EA

- 最適化したい数値パラメータがほとんどなく、ロジック本体の影響が大きすぎる
- PF / DD / トレード数 / 利益だけでは性能を正しく評価できない
- MT5 の通常バックテスト結果から必要な指標が取れない
- 外部 DLL・特殊依存・複数チャートの連携が必要

このようなケースでは、`score()` や `parse()` の大幅な書き換えが必要になります。

---

## セットアップ

### 必要環境

- Python 3.10 以上
- MetaTrader 5 がインストール済み
- 対象 EA の `.ex5` がコンパイル済みで MT5 の Experts フォルダにあること

### 手順

1. `mt5_optimizer_template.py` の **「環境設定」セクション** を自分の環境に合わせて書き換える

   ```python
   INSTALL_DIR  = Path(r"C:\MT5")                      # MT5のインストール先
   MT5_DATA_DIR = Path(r"C:\Users\...\AppData\...")    # MT5のデータフォルダ
   LOGIN        = "your_login_id"
   PASSWORD     = "your_password"
   SERVER       = "YourBroker-Demo"
   SYMBOL       = "EURUSD"
   TIMEFRAME    = "M15"
   ```

2. **「★ EA差し替えゾーン」** を自分の EA に合わせて書き換える（詳細は下記）

3. MT5 を閉じた状態でスクリプトを実行

   ```bash
   python mt5_optimizer_template.py
   ```

---

## 操作方法

### 初回のみ：EX5 を手動コンパイルする

スクリプトを最初に動かす前に、EA の `.ex5`（コンパイル済みバイナリ）を用意する必要があります。

1. MetaEditor を開く（MT5 メニュー → ツール → MetaEditor、または `F4`）
2. 対象の `.mq5` ファイルを開く
3. **F7 キー** を押してコンパイル
4. 下部ログに `0 errors` が出たら成功
5. MT5 を閉じる

> EX5 が存在しない状態でスクリプトを実行するとエラーになります。

---

### 通常の実行フロー

```
python mt5_optimizer_template.py
```

を実行すると、以下の流れで自動・手動が交互に進みます。

#### Phase 0（全自動）

MT5 が自動で何度も起動・終了しながら、パラメータの組み合わせを探します。
**この間は MT5 に触らず待つだけです。**

- MT5 は最小化で起動するため、作業の邪魔になりません
- `GENERATIONS × CHILDREN_PER_GEN` 回テストが走ります（デフォルト 5×5 = 25 回）
- テンプレートが前回から変わっていない場合は Phase 0 をスキップして次へ進みます

#### Round 1〜N（手動介入あり）

**① Claude Pro への依頼（手動）**

スクリプトが一時停止し、以下のようなメッセージが表示されます：

```
以下の3点をclaude.aiに読み込ませてください:
  [1] GA履歴（全体傾向）:  runs\ga_history.txt
  [2] 現在のMQ5ロジック:   C:\MT5\MQL5\Experts\MyEA_template.mq5
  [3] 最良レポート（IS/FW）
```

claude.ai に 3 ファイルをアップロードして改善案をもらい、MQ5 ファイルを編集します。
編集が終わったら **Enter** を押して次へ進みます。

**② コンパイル（手動）**

MetaEditor が自動で起動します。

1. **F7 キー**を押してコンパイル
2. 下部ログに `0 errors` を確認
3. **Enter** を押して続行

> スクリプトが自動で `*_template.mq5 → *_working.mq5` にコピーしてから MetaEditor を開くので、編集したファイルが確実にコンパイルされます。

**③ candidate GA（全自動）**

新しいロジックでパラメータを再探索します。Phase 0 と同様に待つだけです。

**④ 採用判定（自動）**

- 新ロジックのスコアが採用閾値を超えたら **採用** → base を更新
- 超えなかったら **不採用** → 自動で旧 base に戻す

---

### 実行後の確認

| ファイル | 内容 |
|----------|------|
| `runs/best_score.json` | 全実行通算の最高スコアとパラメータ |
| `runs/ga_history.txt` | 全候補の成績履歴（次回の Claude Pro 依頼に使う） |
| `runs/final_best.set` | 最良パラメータの .set ファイル（MT5 に手動で読み込める） |
| `C:\MT5\terminal_logs\` | 実行ログ（エラー調査に使う） |

---

## 自分の EA に差し替える方法

変更箇所は**4ヵ所だけ**です。

### 1. `Params` クラス

EA の `input` パラメータ（チューニングしたい変数）をここに列挙します。

```python
@dataclass
class Params:
    MyPeriod:  int   = 14     # デフォルト値を設定
    MyFactor:  float = 1.5
```

### 2. `clamp()` 関数

各パラメータの最小値・最大値を設定します。

```python
def clamp(p: Params) -> Params:
    p.MyPeriod = max(5, min(50, p.MyPeriod))   # 5〜50の範囲に収める
    p.MyFactor = max(0.5, min(5.0, p.MyFactor))
    return p
```

### 3. `mutate()` 関数

1世代あたりの変化幅を設定します。値が大きいほど広く探索します。

```python
def mutate(p: Params) -> Params:
    c = Params(**asdict(p))
    c.MyPeriod += random.randint(-3, 3)         # ±3の範囲でランダムに変化
    c.MyFactor += random.uniform(-0.2, 0.2)
    return clamp(c)
```

### 4. `write_set_file()` 関数

`Params` のフィールド名 → EA の `input` 変数名のマッピングを書きます。

```python
def write_set_file(p: Params, name: str) -> Path:
    path = SET_DIR / f"{name}.set"
    lines = [
        # 書式: EA変数名=値||値||ステップ||最小||最大||false||小数桁数
        f"InpMyPeriod={p.MyPeriod}||{p.MyPeriod}||1||5||50||false||0",
        f"InpMyFactor={p.MyFactor:.2f}||{p.MyFactor:.2f}||0.1||0.5||5.0||false||2",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
```

---

## ファイル構成

```
mt5_optimizer_template.py  ← メインスクリプト（このリポジトリ）
README.md

実行時に自動生成されるもの:
  runs/
    ga_history.txt          ← GA 結果の全履歴（Claude Pro へのアップロード用）
    best_score.json         ← 全実行通算の最高スコアとパラメータ
    *.ini                   ← MT5 用の設定ファイル（自動生成・削除可）
  C:\MT5\reports\           ← バックテスト HTM レポート
  C:\MT5\terminal_logs\     ← 実行ログ（run_MMDD_HHmm.log）
```

---

## よくあるトラブル

| 症状 | 原因 | 対処 |
|------|------|------|
| レポートが作成されない | MT5がサーバーに接続できていない | 手動でMT5を起動してログイン確認 |
| EX5が見つからない | MQ5ファイルをコンパイルしていない | MetaEditorでF7コンパイル |
| best_score.json 読み込み失敗 | EA差し替え後にパラメータ構造が変わった | best_score.json を削除してリセット |

---

## ライセンス

MIT License
