"""
mt5_optimizer_template.py
=========================
MT5 Strategy Tester × 遺伝的アルゴリズム による EA パラメータ最適化スクリプト。

【仕組みの概要】
 1. Python が MT5 Strategy Tester を自動起動し、バックテスト結果（HTMレポート）を取得する
 2. 取得した成績（PF・DD・トレード数・利益）をスコアに変換する
 3. 遺伝的アルゴリズム（GA）でパラメータを少しずつ変化させながら、高スコアの組み合わせを探す
 4. IS（In-Sample：学習期間）でスクリーニングし、上位のみ FW（Forward：検証期間）で評価する
    → IS で過学習した候補を FW で弾くことで、汎化性能を重視した最適化を行う
 5. AI（Claude Pro）がスコア履歴とロジックを読んでエントリー条件を改善し、さらに GA を回す

【他のEAに差し替える手順】
 ① 「★ EA差し替えゾーン」を開く（Ctrl+F で "★" を検索）
 ② Params クラスを自分の EA の input パラメータに合わせて書き換える
 ③ clamp() で各パラメータの最小値・最大値・制約を設定する
 ④ mutate() で1世代あたりの変化幅を設定する
 ⑤ write_set_file() で Params フィールド名 → EA の input 変数名の対応を書く
 ⑥ 「環境設定」セクションのパス・ログイン情報を自分の環境に合わせる

【前提条件】
 - MT5 がインストール済みで、対象 EA の .ex5 がコンパイル済みであること
 - .ex5 のパスは MT5_DATA_DIR/MQL5/Experts/ 配下に置くこと
 - MT5 を閉じた状態でこのスクリプトを実行すること
"""

import datetime
import hashlib
import json
import math
import random
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path


# =================================================================
#  環境設定（自分の環境に合わせて変更してください）
# =================================================================

# MT5 インストールフォルダ
INSTALL_DIR = Path(r"C:\MT5")
TERMINAL64  = INSTALL_DIR / "terminal64.exe"
METAEDITOR64 = INSTALL_DIR / "metaeditor64.exe"

# MT5 データフォルダ（AppData 以下）
# ターミナルID は人によって異なる。MT5 のフォルダアイコンを右クリック→「エクスプローラーで開く」で確認
MT5_DATA_DIR = Path(r"C:\Users\<ユーザー名>\AppData\Roaming\MetaQuotes\Terminal\<ターミナルID>")

# テンプレート MQ5（Claude Code が編集する元ファイル）
EA_TEMPLATE      = INSTALL_DIR / "MQL5" / "Experts" / "MyEA_template.mq5"
EA_TEMPLATE_BASE = INSTALL_DIR / "MQL5" / "Experts" / "MyEA_template_base.mq5"

# 実行用 MQ5/EX5（MT5 が読むファイル）
EA_WORKING = MT5_DATA_DIR / "MQL5" / "Experts" / "MyEA_working.mq5"
EX5_DATA   = MT5_DATA_DIR / "MQL5" / "Experts" / "MyEA_working.ex5"
EX5_BASE   = MT5_DATA_DIR / "MQL5" / "Experts" / "MyEA_working_base.ex5"

# .ini / レポート / ログの保存先
RUN_DIR          = Path(r"C:\Users\<ユーザー名>\Desktop\runs")
REPORT_DIR       = INSTALL_DIR / "reports"
TERMINAL_LOG_DIR = INSTALL_DIR / "terminal_logs"

RUN_DIR.mkdir(parents=True, exist_ok=True)
REPORT_DIR.mkdir(parents=True, exist_ok=True)
TERMINAL_LOG_DIR.mkdir(parents=True, exist_ok=True)

# .set ファイルはここに置く（MT5 がここを参照する）
SET_DIR = MT5_DATA_DIR / "MQL5" / "Profiles" / "Tester"
SET_DIR.mkdir(parents=True, exist_ok=True)

# MT5 ログイン情報
LOGIN    = "your_login_id"
PASSWORD = "your_password"
SERVER   = "YourBroker-Demo"

# テスト対象
SYMBOL    = "EURUSD"
TIMEFRAME = "M15"

# IS（学習期間） / FW（検証期間）
DATE_FROM    = "2022.01.01"
DATE_TO      = "2024.12.31"
FORWARD_FROM = "2025.01.01"
FORWARD_TO   = "2025.12.31"

# GA 設定
GENERATIONS      = 5   # 世代数
CHILDREN_PER_GEN = 5   # 1世代あたりの候補数（合計テスト回数 = GENERATIONS × CHILDREN_PER_GEN）
OUTER_LOOPS      = 1   # AI改善ループ回数（AI に EA を改善させる回数）
FW_TOP_N         = 2   # 各世代で IS 上位何件に FW を実施するか

# 実行ID（ファイル名の衝突を防ぐために使う）
RUN_ID = datetime.datetime.now().strftime("%m%d_%H%M")

# GA 履歴・全実行ベストスコアの保存先
GA_HISTORY_FILE = RUN_DIR / "ga_history.txt"
BEST_SCORE_FILE = RUN_DIR / "best_score.json"


# =================================================================
#  ★ EA差し替えゾーン（ここだけ書き換えれば別の EA に対応できます）
# =================================================================
#
# 【変更箇所は4つだけ】
#  1. Params クラス  → EA の input パラメータ名と型・デフォルト値
#  2. clamp()        → 各パラメータの有効範囲・相互制約
#  3. mutate()       → 1世代あたりの変化幅
#  4. write_set_file() → Params フィールド → .set ファイルの変数名マッピング
#
# 【.set ファイルの書式】
#  変数名=値||値||ステップ||最小||最大||最適化フラグ||小数桁数
#  └─ 最適化フラグは false 固定（Python 側で GA を回すため MT5 側の最適化は使わない）
#  └─ 最初の「値」だけが実際に使われる。残りは MT5 が UI 表示用に使う情報
#
# 【例: AutoEA（MA + RCI + ATR）の場合】

@dataclass
class Params:
    # ---- ここを自分の EA の input パラメータに合わせて書き換える ----
    # フィールド名は何でもよい。write_set_file() で EA 変数名と対応させる。

    # MA（トレンドフィルター）
    FastMAPeriod: int   = 21    # 短期 MA の期間
    SlowMAPeriod: int   = 80    # 長期 MA の期間

    # RCI（主シグナル）
    RciFast:      int   = 12    # 短期 RCI の期間
    RciMid:       int   = 48    # 中期 RCI の期間
    Rci12Floor:   float = -80.0 # 短期 RCI のロングエントリー閾値（この値を下から上抜けたらエントリー）
    Rci48Floor:   float = -70.0 # 中期 RCI のロングフィルター閾値

    # ATR（SL・トレーリング）
    ATRPeriod:    int   = 14    # ATR の計算期間
    SL_ATR:       float = 1.0   # 初期 SL = ATR × この値
    TrailFar:     float = 2.0   # トレーリング SL = ATR × この値


def clamp(p: Params) -> Params:
    """各パラメータを有効範囲内に収める。範囲を外れた値は境界値にクリップする。

    ここで相互制約（例: FastMA < SlowMA）も強制できる。
    """
    # ---- 各パラメータの min/max をここで設定 ----
    p.FastMAPeriod = max(5,    min(60,   p.FastMAPeriod))
    p.SlowMAPeriod = max(20,   min(200,  p.SlowMAPeriod))
    p.RciFast      = max(5,    min(30,   p.RciFast))
    p.RciMid       = max(20,   min(100,  p.RciMid))
    p.Rci12Floor   = max(-100, min(-40,  p.Rci12Floor))
    p.Rci48Floor   = max(-100, min(-30,  p.Rci48Floor))
    p.ATRPeriod    = max(5,    min(50,   p.ATRPeriod))
    p.SL_ATR       = max(0.5,  min(3.0,  p.SL_ATR))
    p.TrailFar     = max(1.0,  min(5.0,  p.TrailFar))

    # ---- 相互制約（例: FastMA は必ず SlowMA より小さい）----
    if p.FastMAPeriod >= p.SlowMAPeriod:
        p.SlowMAPeriod = p.FastMAPeriod + random.randint(10, 40)
    if p.RciFast >= p.RciMid:
        p.RciMid = p.RciFast + random.randint(10, 30)

    return p


def mutate(p: Params) -> Params:
    """親パラメータをランダムに少し変化させて子を生成する。

    変化幅が大きいほど探索が広くなるが収束が遅くなる。
    clamp() を最後に呼ぶことで範囲外に出ないようにする。
    """
    c = Params(**asdict(p))

    # ---- 各パラメータの変化幅をここで設定 ----
    c.FastMAPeriod += random.randint(-5, 5)
    c.SlowMAPeriod += random.randint(-10, 10)
    c.RciFast      += random.randint(-3, 3)
    c.RciMid       += random.randint(-8, 8)
    c.Rci12Floor   += random.uniform(-10, 10)
    c.Rci48Floor   += random.uniform(-10, 10)
    c.ATRPeriod    += random.randint(-3, 3)
    c.SL_ATR       += random.uniform(-0.2, 0.2)
    c.TrailFar     += random.uniform(-0.3, 0.3)

    return clamp(c)


def write_set_file(p: Params, name: str) -> Path:
    """.set ファイルを生成して MT5 に渡す。

    書式: EA変数名=値||値||ステップ||最小||最大||false||小数桁数
    └─ false = 最適化しない（Python の GA でパラメータを制御するため）

    ---- ここを自分の EA の input 変数名に合わせて書き換える ----
    左辺（InpXxx）: EA の input 変数名（MQ5 ファイルで定義されている名前）
    右辺（p.Xxx）:  上の Params クラスのフィールド名
    """
    path = SET_DIR / f"{name}.set"
    lines = [
        # int パラメータ: 小数桁数=0、ステップ=1
        f"InpSmaFastPeriod={p.FastMAPeriod}||{p.FastMAPeriod}||1||5||60||false||0",
        f"InpSmaSlowPeriod={p.SlowMAPeriod}||{p.SlowMAPeriod}||1||20||200||false||0",
        f"InpRciFast={p.RciFast}||{p.RciFast}||1||5||30||false||0",
        f"InpRciMid={p.RciMid}||{p.RciMid}||1||20||100||false||0",
        # float パラメータ: 小数桁数=1〜2、ステップは変化幅の最小単位
        f"InpRci12Floor={p.Rci12Floor:.1f}||{p.Rci12Floor:.1f}||1.0||-100.0||-40.0||false||1",
        f"InpRci48Floor={p.Rci48Floor:.1f}||{p.Rci48Floor:.1f}||1.0||-100.0||-30.0||false||1",
        f"InpATRPeriod={p.ATRPeriod}||{p.ATRPeriod}||1||5||50||false||0",
        f"InpATRInitialSL={p.SL_ATR:.2f}||{p.SL_ATR:.2f}||0.1||0.5||3.0||false||2",
        f"InpATRTrailFar={p.TrailFar:.2f}||{p.TrailFar:.2f}||0.1||1.0||5.0||false||2",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# =================================================================
#  エンジン部分（基本的に変更不要）
# =================================================================

# ---- ターミナルログの同時出力 ----

class _Tee:
    """指定したストリーム（stdout/stderr）とファイルに同時に書き出す。"""
    def __init__(self, stream, file):
        self._stream = stream
        self._file = file

    def write(self, obj):
        self._stream.write(obj)
        self._file.write(obj)
        self._file.flush()

    def flush(self):
        self._stream.flush()
        self._file.flush()


# ---- ログ補助 ----

def tail_text(path: Path, max_chars: int = 4000) -> str:
    if not path.exists():
        return f"(not found) {path}"
    for enc in ("utf-16", "utf-8", "cp932"):
        try:
            txt = path.read_text(encoding=enc, errors="ignore")
            return txt[-max_chars:]
        except Exception:
            pass
    return f"(unreadable) {path}"


def latest_log_text(log_dir: Path) -> str:
    if not log_dir.exists():
        return f"(log dir not found) {log_dir}"
    files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return f"(no log files) {log_dir}"
    return f"latest_log={files[0]}\n{tail_text(files[0])}"


# ---- レポート探索 ----

def _report_candidates(name: str) -> list[Path]:
    exts = (".htm", ".html", ".xml")
    candidates = []
    for ext in exts:
        candidates.append(MT5_DATA_DIR / f"{name}{ext}")
    for ext in exts:
        candidates.append(MT5_DATA_DIR / "reports" / f"{name}{ext}")
        candidates.append(INSTALL_DIR / f"{name}{ext}")
    return candidates


def find_report(name: str) -> Path | None:
    for p in _report_candidates(name):
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def _wait_file_stable(path: Path, checks: int = 3, interval: float = 1.0) -> None:
    """ファイルサイズが安定するまで待つ（書き途中の読み取りを防ぐ）。"""
    prev_size = -1
    for _ in range(checks):
        cur_size = path.stat().st_size if path.exists() else 0
        if cur_size == prev_size:
            break
        prev_size = cur_size
        time.sleep(interval)


# ---- MT5 テスト実行 ----

def run_test(name: str, start: str, end: str, set_file: Path | None = None, timeout_sec: int = 1200) -> Path:
    """MT5 Strategy Tester を起動し、HTM レポートを返す。

    INI ファイルで EA・期間・パラメータを指定して MT5 を subprocess 起動する。
    MT5 はテスト完了後に自動終了（ShutdownTerminal=1）する。
    レポートが生成されるまで最大 timeout_sec 秒ポーリングで待機する。
    """
    ini = (RUN_DIR / f"{name}.ini").resolve()
    report_path = name  # MT5 はファイル名のみ指定するとデータフォルダ直下に書き込む

    # 古いレポートを削除（前回の残骸を誤検知しないため）
    for old in _report_candidates(name):
        if old.exists():
            old.unlink()

    tester_lines = [
        "[Common]",
        f"Login={LOGIN}",
        f"Password={PASSWORD}",
        f"Server={SERVER}",
        "ProxyEnable=0",
        "CertInstall=0",
        "NewsEnable=0",
        "KeepPrivate=1",
        "",
        "[Tester]",
        f"Expert={EX5_DATA.stem}",   # ← EX5_DATA のファイル名から自動生成（拡張子なし）
        f"Symbol={SYMBOL}",
        f"Period={TIMEFRAME}",
        "Model=0",
        "ExecutionMode=0",
        "Optimization=0",
        f"FromDate={start}",
        f"ToDate={end}",
        "ForwardMode=0",
        "Deposit=100000",
        "Currency=JPY",
        "Leverage=1:100",
        "Visual=0",
        "ReplaceReport=1",
        "ShutdownTerminal=1",
        f"Report={report_path}",
        "UseLocal=1",
        "UseRemote=0",
        "UseCloud=0",
    ]
    if set_file is not None:
        idx = next(i for i, l in enumerate(tester_lines) if l.startswith("Report="))
        tester_lines.insert(idx + 1, f"ExpertParameters={set_file.name}")

    ini.write_text("\n".join(tester_lines), encoding="utf-8")

    print(f"テスト開始: {name}  ({start} → {end})")
    print(f"  レポート出力先: {MT5_DATA_DIR / name}.htm")

    # MT5 を最小化起動（SW_SHOWMINNOACTIVE: フォーカスを奪わず最小化で開く）
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 7
    proc = subprocess.Popen(
        [str(TERMINAL64), f"/config:{ini}"],
        cwd=str(INSTALL_DIR),
        startupinfo=si,
    )

    time.sleep(10)  # MT5 起動・接続待ち（最低10秒）

    started = time.time()
    while time.time() - started < timeout_sec:
        found = find_report(name)
        if found is not None:
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            _wait_file_stable(found)
            REPORT_DIR.mkdir(parents=True, exist_ok=True)
            dest = REPORT_DIR / found.name
            shutil.copy2(found, dest)
            print(f"  レポート作成完了: {dest}")
            return dest

        if proc.poll() is not None:
            for _ in range(20):
                time.sleep(3)
                found = find_report(name)
                if found is not None:
                    _wait_file_stable(found)
                    REPORT_DIR.mkdir(parents=True, exist_ok=True)
                    dest = REPORT_DIR / found.name
                    shutil.copy2(found, dest)
                    print(f"  レポート作成完了: {dest}")
                    return dest
            break

        time.sleep(3)

    try:
        proc.kill()
        proc.communicate(timeout=5)
    except Exception:
        pass

    terminal_log = latest_log_text(MT5_DATA_DIR / "logs")
    tester_log   = latest_log_text(MT5_DATA_DIR / "Tester" / "logs")

    raise FileNotFoundError(
        f"レポートが作成されませんでした: {name}\n"
        f"ini={ini}\n"
        f"探した場所={_report_candidates(name)}\n"
        f"\n--- TERMINAL LOG ---\n{terminal_log}\n"
        f"\n--- TESTER LOG ---\n{tester_log}\n"
    )


# ---- レポート解析 ----

def parse(report: Path) -> dict:
    """MT5 バックテスト HTM レポートから成績を抽出する。

    返り値: {"pf": float, "dd": float, "trades": float, "profit": float}
     - pf     : プロフィットファクター（勝ち/負け の比率。1.0以上が必要条件）
     - dd     : 資産相対ドローダウン（%）
     - trades : 総トレード数
     - profit : 純利益
    """
    if not report.exists():
        raise FileNotFoundError(f"report file not found: {report}")

    txt = ""
    for enc in ("utf-16", "utf-8", "cp932"):
        try:
            txt = report.read_text(encoding=enc, errors="ignore")
            if txt:
                break
        except Exception:
            pass

    txt = re.sub(r"<.*?>", " ", txt)
    txt = re.sub(r"\s+", " ", txt)

    def find(label: str) -> float:
        m = re.search(re.escape(label) + r"[:\s]+([-+]?[\d ]+(?:\.\d+)?)", txt, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1).replace(" ", ""))
            except ValueError:
                pass
        return 0.0

    def find_pct(label: str) -> float:
        m = re.search(re.escape(label) + r"[:\s]+([-+]?\d+(?:\.\d+)?)\s*%", txt, re.IGNORECASE)
        return float(m.group(1)) if m else 0.0

    return {
        "pf":     find("プロフィットファクター") or find("Profit Factor"),
        "dd":     find_pct("証拠金相対ドローダウン") or find_pct("Equity Drawdown Relative"),
        "trades": find("約定数") or find("Total Trades"),
        "profit": find("総損益") or find("Net Profit"),
    }


# ---- スコアリング ----

def score(s: dict) -> float:
    """成績辞書をスコアに変換する（加点方式＋ソフトペナルティ）。

    スコア式:
      基礎点 = min(PF, 2.0) × 30  … PF が高いほど良いが 2.0 で頭打ち
             + log(1 + profit)    … 利益の対数（大きな利益に対しては控えめに加算）
             - DD × 1.5           … ドローダウンは直接減点

    ソフトペナルティ（完全失格ではなく減点のみ）:
      PF < 1.0     → (1.0 - PF) × 50   減点（負けEAに強い罰則）
      trades < 120 → (120 - trades) × 0.5 減点（トレード不足＝過学習の疑い）
      DD > 20%     → (DD - 20) × 3.0   減点（高DDには厳しい罰則）
    """
    sc  = min(s["pf"], 2.0) * 30.0
    sc += math.log1p(max(s["profit"], 0.0))
    sc -= s["dd"] * 1.5

    if s["pf"] < 1.0:
        sc -= (1.0 - s["pf"]) * 50.0
    if s["trades"] < 120:
        sc -= (120 - s["trades"]) * 0.5
    if s["dd"] > 20:
        sc -= (s["dd"] - 20) * 3.0

    return sc


# ---- AI改善ステップ（Claude Pro 経由・手動） ----

def ai_improve_ea(
    top_results: list,
    improvement_history: list[str],
    global_is_report: "Path | None" = None,
    global_fw_report: "Path | None" = None,
) -> bool:
    """GA 結果を ga_history.txt に追記し、Claude Pro への相談とMQ5編集をユーザーに促す。"""
    round_num = len(improvement_history) + 1
    save_ga_history(top_results, f"round{round_num}_pre")

    if global_is_report and global_is_report.exists():
        best_is_report = global_is_report
        best_fw_report = global_fw_report
    else:
        best_is_report = top_results[0][5] if top_results else None
        best_fw_report = top_results[0][6] if top_results else None

    print()
    print("=" * 60)
    print(f"【Round {round_num}】Claude Proで改善してください")
    print()
    print(f"  以下の3点をclaude.aiに読み込ませてください:")
    print(f"  [1] GA履歴（全体傾向）:  {GA_HISTORY_FILE}")
    print(f"  [2] 現在のMQ5ロジック:   {EA_TEMPLATE}")
    print(f"  [3] 最良レポート（IS/FW）:")
    if best_is_report:
        print(f"      IS: {best_is_report}")
    if best_fw_report:
        print(f"      FW: {best_fw_report}")
    print()
    print(f"  依頼文（コピペ用）:")
    print(f"  「以下の3つをすべて読んでから改善案を出してください。")
    print(f"    1. ga_history.txt（パラメータと成績の履歴）")
    print(f"    2. MQ5ロジックファイル（現在のエントリー条件）")
    print(f"    3. IS/FW レポート（HTMファイル）")
    print(f"    改善案をもらったら MQ5 を直接修正してください。」")
    print()
    print(f"  編集完了したら Enter を押す（コンパイルは次の画面で案内）")
    print("=" * 60)
    input("  MQ5編集完了したら Enter を押してください > ")

    improvement_history.append(f"Round {round_num}: Claude Pro経由で改善")
    return True


_metaeditor_proc: subprocess.Popen | None = None


def compile_with_metaeditor() -> bool:
    """MetaEditor を起動してユーザーに手動コンパイル（F7）を求め、EX5の更新を確認する。

    MetaEditor を subprocess で /compile: オプション付きで呼んでも EX5 は生成されない。
    GUI 上で F7 を押す手動コンパイルが唯一の方法。

    【ファイルの流れ】
      AI が編集するのは EA_TEMPLATE（C:\\MT5\\MQL5\\Experts\\ 配下）。
      MT5 が実際に読むのは EA_WORKING（AppData 配下）。
      コンパイル前に EA_TEMPLATE → EA_WORKING を自動コピーして内容を一致させる。
    """
    global _metaeditor_proc

    # --- EA_TEMPLATE → EA_WORKING に同期（AI編集結果をコンパイル対象に反映）---
    if EA_TEMPLATE.exists():
        shutil.copy2(EA_TEMPLATE, EA_WORKING)
        print(f"同期: {EA_TEMPLATE.name} → {EA_WORKING.name}")
    else:
        print(f"【警告】EA_TEMPLATE が見つかりません: {EA_TEMPLATE}")

    mtime_before = EX5_DATA.stat().st_mtime if EX5_DATA.exists() else 0.0

    if _metaeditor_proc is not None and _metaeditor_proc.poll() is None:
        print("前回のMetaEditorを終了します...")
        _metaeditor_proc.terminate()
        try:
            _metaeditor_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _metaeditor_proc.kill()
            _metaeditor_proc.wait()
        _metaeditor_proc = None

    if METAEDITOR64.exists():
        _metaeditor_proc = subprocess.Popen(
            [str(METAEDITOR64), str(EA_WORKING)],
            cwd=str(METAEDITOR64.parent),
        )

    print()
    print("=" * 60)
    print("【手動コンパイルが必要です】")
    print(f"  ファイル: {EA_WORKING.name}")
    print("  手順: MetaEditor が開いたら F7 → 下部ログに「0 errors」→ Enter")
    print("=" * 60)
    input("  コンパイル完了したら Enter を押してください > ")

    if EX5_DATA.exists() and EX5_DATA.stat().st_mtime > mtime_before:
        print(f"コンパイル確認OK: {EX5_DATA.name} が更新されました")
        return True

    if not EX5_DATA.exists():
        print("【失敗】EX5が見つかりません。F7でコンパイルしてから再度Enterを押してください。")
    else:
        print("【失敗】EX5の更新時刻が変わっていません。F7を押してコンパイルしてください。")
    return False


# ---- GA 結果の保存 ----

def template_hash() -> str:
    """EA_TEMPLATE の MD5 ハッシュを返す。ファイルがなければ空文字。"""
    if not EA_TEMPLATE.exists():
        return ""
    return hashlib.md5(EA_TEMPLATE.read_bytes()).hexdigest()


def load_saved_template_hash() -> str:
    """best_score.json に保存されたテンプレートハッシュを返す。なければ空文字。"""
    if BEST_SCORE_FILE.exists():
        try:
            data = json.loads(BEST_SCORE_FILE.read_text(encoding="utf-8"))
            return data.get("template_hash", "")
        except Exception:
            pass
    return ""


def load_global_best() -> tuple[float, "Params | None", "Path | None", "Path | None"]:
    """過去実行の最高スコア・パラメータ・レポートパスを読み込む。"""
    if BEST_SCORE_FILE.exists():
        try:
            data = json.loads(BEST_SCORE_FILE.read_text(encoding="utf-8"))
            sc = float(data.get("best_score", -999999.0))
            params = None
            if "params" in data:
                known = {k: v for k, v in data["params"].items() if k in Params.__dataclass_fields__}
                params = Params(**known)
            is_report = Path(data["best_is_report"]) if "best_is_report" in data else None
            fw_report = Path(data["best_fw_report"]) if "best_fw_report" in data else None
            return sc, params, is_report, fw_report
        except Exception as e:
            print(f"best_score.json 読み込み失敗: {e}")
    return -999999.0, None, None, None


def save_global_best_score(sc: float, params: "Params | None" = None,
                           is_stats: dict | None = None, fw_stats: dict | None = None,
                           is_report: "Path | None" = None, fw_report: "Path | None" = None) -> None:
    data: dict = {
        "best_score": sc,
        "updated": datetime.datetime.now().isoformat(),
        "template_hash": template_hash(),
    }
    if params is not None:
        data["params"] = asdict(params)
    if is_stats is not None:
        data["IS"] = is_stats
    if fw_stats is not None:
        data["FW"] = fw_stats
    if is_report is not None:
        data["best_is_report"] = str(is_report)
    if fw_report is not None:
        data["best_fw_report"] = str(fw_report)
    BEST_SCORE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"全実行ベストスコア更新: {sc:.2f} → {BEST_SCORE_FILE}")


def save_ga_history(top3: list, label: str) -> None:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"\n=== {now}  [{label}] ==="]
    if not top3:
        lines.append("  （合格候補なし）")
    else:
        for rank, (sc, params, is_, fw, tag, is_report, fw_report) in enumerate(top3, 1):
            lines.append(
                f"  候補{rank}: score={sc:.2f}  tag={tag}"
                f"  IS: PF={is_['pf']:.2f} DD={is_['dd']:.1f}% trades={is_['trades']:.0f} profit={is_['profit']:.0f}"
                f"  FW: PF={fw['pf']:.2f} DD={fw['dd']:.1f}% trades={fw['trades']:.0f}"
            )
            lines.append(f"    params: {asdict(params)}")
            lines.append(f"    report: {is_report}  /  {fw_report}")
    text = "\n".join(lines) + "\n"
    with GA_HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(text)
    print(f"GA履歴を保存: {GA_HISTORY_FILE}")


# ---- base 保存 / 復元 ----

def save_as_base() -> None:
    """現在の working.ex5 と template.mq5 を採用済み base として保存する。"""
    if EX5_DATA.exists():
        shutil.copy2(EX5_DATA, EX5_BASE)
    if EA_TEMPLATE.exists():
        shutil.copy2(EA_TEMPLATE, EA_TEMPLATE_BASE)
    print(f"base保存: {EX5_BASE.name}, {EA_TEMPLATE_BASE.name}")


def restore_base() -> None:
    """candidate が不採用だった場合に base 版を working に復元する。"""
    if EX5_BASE.exists():
        shutil.copy2(EX5_BASE, EX5_DATA)
        print(f"base復元: {EX5_BASE.name} → {EX5_DATA.name}")
    if EA_TEMPLATE_BASE.exists():
        shutil.copy2(EA_TEMPLATE_BASE, EA_TEMPLATE)
        shutil.copy2(EA_TEMPLATE_BASE, EA_WORKING)
        print(f"base復元: {EA_TEMPLATE_BASE.name} → template / working")


# ---- 評価関数 ----

def evaluate_is(p: Params, gen: int, idx: int, label: str = ""):
    """IS テスト（学習期間）のみ実行。全候補のスクリーニングに使う。"""
    prefix = f"{label}_" if label else ""
    tag = f"{RUN_ID}_{prefix}g{gen}_c{idx}"
    set_file = write_set_file(p, tag)
    is_report = run_test(f"{tag}_is", DATE_FROM, DATE_TO, set_file=set_file)
    r1 = parse(is_report)
    s1 = score(r1)
    return s1, r1, tag, is_report, set_file


def evaluate_fw(tag: str, set_file: Path):
    """FW テスト（検証期間）のみ実行。IS 上位候補に対して実施する。"""
    fw_report = run_test(f"{tag}_fw", FORWARD_FROM, FORWARD_TO, set_file=set_file)
    r2 = parse(fw_report)
    s2 = score(r2)
    return s2, r2, fw_report


# ---- GA ループ ----

def run_ga_loop(label: str, seed: "Params | None" = None) -> tuple[list, tuple | None]:
    """GENERATIONS × CHILDREN_PER_GEN の GA を回し (top3, best) を返す。

    各世代の流れ:
      1. 全候補を IS のみで評価（高速スクリーニング）
      2. IS 上位 FW_TOP_N 件だけ FW を実施（過学習フィルター）
      3. IS×0.4 + FW×0.6 の複合スコアで次世代の親を決定
    """
    parent = seed if seed is not None else Params()
    loop_best: tuple | None = None
    all_results: list = []

    for g in range(GENERATIONS):
        gen_is: list = []
        for i in range(CHILDREN_PER_GEN):
            try:
                cand = mutate(parent)
                s1, r1, tag, is_report, set_file = evaluate_is(cand, g, i, label)
                print(f"[{label}-{g}-{i}] IS={s1:.2f}  PF={r1['pf']:.2f}  trades={r1['trades']:.0f}")
                gen_is.append((s1, cand, r1, tag, is_report, set_file))
            except Exception as e:
                print(f"ERROR IS [{label}-{g}-{i}]: {e}")

        if not gen_is:
            print(f"Generation {g}: 有効な結果なし")
            continue

        gen_is.sort(reverse=True, key=lambda x: x[0])
        gen_results: list = []
        for rank, (s1, cand, r1, tag, is_report, set_file) in enumerate(gen_is[:FW_TOP_N]):
            try:
                s2, r2, fw_report = evaluate_fw(tag, set_file)
                sc = s1 * 0.4 + s2 * 0.6
                print(f"  FW [{rank+1}/{FW_TOP_N}] score={sc:.2f}  PF={r1['pf']:.2f}  FW={r2['pf']:.2f}")
                gen_results.append((sc, cand, r1, r2, tag, is_report, fw_report))
                all_results.append((sc, cand, r1, r2, tag, is_report, fw_report))
            except Exception as e:
                print(f"ERROR FW [{label}-{g}-{rank}]: {e}")

        if not gen_results:
            print(f"Generation {g}: FW評価失敗")
            continue

        gen_results.sort(reverse=True, key=lambda x: x[0])
        parent = gen_results[0][1]

        if loop_best is None or gen_results[0][0] > loop_best[0]:
            loop_best = gen_results[0]
            print(f"★ NEW BEST [{label}]  score={loop_best[0]:.2f}")

    all_results.sort(reverse=True, key=lambda x: x[0])
    return all_results[:3], loop_best


# ---- メイン ----

def main():
    print(f"MT5データフォルダ: {MT5_DATA_DIR}")
    print(f"EX5パス: {EX5_DATA}")
    print(f".setフォルダ: {SET_DIR}")
    print()

    if not EX5_DATA.exists():
        print("=" * 60)
        print("【エラー】EX5が見つかりません。")
        print(f"  {EX5_DATA}")
        print("MT5またはMetaEditorで MQ5 を一度コンパイルしてから再実行してください。")
        print("=" * 60)
        return

    print(f"EX5確認OK: {EX5_DATA}")
    print()

    global_best: tuple | None = None
    base_params: Params | None = None
    improvement_history: list[str] = []
    score_updated_this_session = False

    global_high_score, prev_best_params, prev_best_is_report, prev_best_fw_report = load_global_best()
    if global_high_score > -999999.0:
        print(f"過去の最高スコアを引き継ぎ: {global_high_score:.2f}")

    # =========================================================
    # Phase 0 スキップ判定
    # テンプレートが前回から変わっていなければ Phase 0 を省略する
    # =========================================================
    _cur_hash = template_hash()
    skip_phase0 = (
        _cur_hash != ""
        and _cur_hash == load_saved_template_hash()
        and global_high_score > -999999.0
        and EX5_BASE.exists()
        and prev_best_params is not None
    )

    # =========================================================
    # Phase 0: 初回 base GA（採点基準を作る）
    # =========================================================
    if not skip_phase0:
        print(f"\n{'='*60}")
        print("【Phase 0】初回 base GA")
        print(f"{'='*60}")

        base_top3, base_best = run_ga_loop("base0", seed=prev_best_params)
        base_score = base_best[0] if base_best else -999999.0

        if base_best is not None:
            if global_best is None or base_score > global_best[0]:
                global_best = base_best
            print(f"base_score 確定: {base_score:.2f}")
            if base_score > global_high_score:
                global_high_score = base_score
                base_params = base_best[1]
                score_updated_this_session = True
                save_as_base()
                save_global_best_score(global_high_score, base_best[1], base_best[2], base_best[3], base_best[5], base_best[6])
                prev_best_is_report, prev_best_fw_report = base_best[5], base_best[6]
            else:
                base_params = prev_best_params
                print(f"  → 全体ベスト({global_high_score:.2f})未満のため base を維持")
        else:
            print("有効な結果が出ませんでした（候補が0件）。終了します。")
            return

        save_ga_history(base_top3, "base0")

    else:
        # Phase 0 スキップ: テンプレート未変更なので前回ベストをそのまま使用
        print(f"\n{'='*60}")
        print("【Phase 0】スキップ（テンプレート未変更）")
        print(f"  前回ベストスコア {global_high_score:.2f} をそのまま使用します")
        print(f"{'='*60}")
        base_score  = global_high_score
        base_params = prev_best_params
        base_top3   = []
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        with GA_HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(f"\n=== {now}  [base0] ===\n")
            f.write(f"  （Phase 0 スキップ: テンプレート未変更 / 前回ベスト {global_high_score:.2f} を継続）\n")

    # =========================================================
    # Phase 1〜N: AI改善 → candidate GA → 採用/不採用
    # =========================================================
    for round_num in range(OUTER_LOOPS):
        print(f"\n{'='*60}")
        print(f"【Round {round_num + 1}/{OUTER_LOOPS}】AI改善 → candidate GA → 採用判定")
        print(f"{'='*60}")

        improved = ai_improve_ea(base_top3, improvement_history, prev_best_is_report, prev_best_fw_report)
        if not improved:
            print("AI改善失敗。このラウンドをスキップします。")
            continue

        compiled = compile_with_metaeditor()
        if not compiled:
            print("コンパイル失敗。base版を復元してスキップします。")
            restore_base()
            continue

        print(f"\n--- candidate GA (round {round_num + 1}) ---")
        candidate_top3, candidate_best = run_ga_loop(f"cand{round_num + 1}", seed=base_params)
        candidate_score = candidate_best[0] if candidate_best else -999999.0

        adoption_threshold = max(base_score, global_high_score)
        print(f"\n採用判定: candidate={candidate_score:.2f} vs 閾値={adoption_threshold:.2f}")
        if candidate_score > adoption_threshold:
            print(f"→ 採用（+{candidate_score - adoption_threshold:.2f}）candidate を新 base に昇格")
            base_score = candidate_score
            base_top3 = candidate_top3
            if candidate_best:
                global_best = candidate_best
                base_params = candidate_best[1]
            save_as_base()
            global_high_score = candidate_score
            score_updated_this_session = True
            save_global_best_score(global_high_score, candidate_best[1], candidate_best[2], candidate_best[3], candidate_best[5], candidate_best[6])
            prev_best_is_report, prev_best_fw_report = candidate_best[5], candidate_best[6]
        else:
            diff = candidate_score - adoption_threshold
            print(f"→ 不採用（{diff:.2f}）base版を復元")
            restore_base()

    print("\n=== 完了 ===")
    if score_updated_this_session and global_best:
        print(f"最高スコア（今回更新）: {global_best[0]:.2f}")
        print(f"パラメータ: {global_best[1]}")
        best_set = write_set_file(global_best[1], "final_best")
        print(f"最終ベスト .set 保存: {best_set}")
    elif global_high_score > -999999.0:
        print(f"今回スコア更新なし。全実行ベスト: {global_high_score:.2f}")
        if prev_best_params:
            best_set = write_set_file(prev_best_params, "final_best")
            print(f"全実行ベスト .set 保存: {best_set}")


if __name__ == "__main__":
    log_path = TERMINAL_LOG_DIR / f"run_{RUN_ID}.log"
    with open(log_path, "w", encoding="utf-8") as _log_f:
        _orig_stdout = sys.stdout
        _orig_stderr = sys.stderr
        sys.stdout = _Tee(sys.stdout, _log_f)
        sys.stderr = _Tee(sys.stderr, _log_f)
        try:
            main()
        finally:
            sys.stdout = _orig_stdout
            sys.stderr = _orig_stderr
