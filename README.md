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
