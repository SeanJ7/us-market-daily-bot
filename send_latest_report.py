#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import concurrent.futures
import hashlib
import html
import json
import math
import os
import statistics
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo


DEFAULT_CHAT_LIMIT = 3800
USER_AGENT = "Mozilla/5.0 (Codex Market Report Bot)"
REPORT_TITLE_PREFIX = "美股收盘日报｜"
HTTP_TIMEOUT_SECONDS = 12

INDEX_SYMBOLS = {
    "Dow Jones": "^DJI",
    "S&P 500": "^GSPC",
    "Nasdaq Composite": "^IXIC",
    "Russell 2000": "^RUT",
    "VIX": "^VIX",
    "QQQ": "QQQ",
    "IWM": "IWM",
    "SOXX": "SOXX",
}

SECTOR_SYMBOLS = {
    "信息技术": "XLK",
    "通信服务": "XLC",
    "可选消费": "XLY",
    "金融": "XLF",
    "工业": "XLI",
    "医疗保健": "XLV",
    "必需消费": "XLP",
    "能源": "XLE",
    "公用事业": "XLU",
    "材料": "XLB",
    "房地产": "XLRE",
}

THEME_SYMBOLS = {
    "半导体": "SMH",
    "软件": "IGV",
    "网络安全": "CIBR",
    "云计算": "CLOU",
    "AI/自动化": "AIQ",
    "小盘成长": "IWO",
    "小盘价值": "IWN",
    "等权标普": "RSP",
    "大盘成长": "SCHG",
    "大盘价值": "VTV",
}

ASSET_SYMBOLS = {
    "DXY 美元指数": "DX-Y.NYB",
    "黄金": "GC=F",
    "WTI 原油": "CL=F",
    "Brent 原油": "BZ=F",
    "比特币": "BTC-USD",
    "以太坊": "ETH-USD",
}

MEGA_CAP_SYMBOLS = {
    "NVDA": "NVDA",
    "MSFT": "MSFT",
    "AAPL": "AAPL",
    "GOOGL": "GOOGL",
    "AMZN": "AMZN",
    "META": "META",
    "TSLA": "TSLA",
}

FOCUS_SYMBOLS = {
    "NVDA": "NVDA",
    "AMD": "AMD",
    "AVGO": "AVGO",
    "MRVL": "MRVL",
    "GOOGL": "GOOGL",
    "MSFT": "MSFT",
    "META": "META",
    "AMZN": "AMZN",
    "ORCL": "ORCL",
    "CRM": "CRM",
    "NOW": "NOW",
    "SNOW": "SNOW",
    "ADBE": "ADBE",
    "PANW": "PANW",
    "CRWD": "CRWD",
    "PLTR": "PLTR",
    "DDOG": "DDOG",
    "NET": "NET",
    "LITE": "LITE",
    "COHR": "COHR",
    "AAOI": "AAOI",
    "TSEM": "TSEM",
    "SIVE": "SIVE",
    "ANET": "ANET",
    "FLNC": "FLNC",
    "OKLO": "OKLO",
    "VST": "VST",
    "CEG": "CEG",
    "ETN": "ETN",
    "VRT": "VRT",
    "PWR": "PWR",
    "GEV": "GEV",
    "APLD": "APLD",
    "IREN": "IREN",
}

YIELD_SERIES = {
    "2Y": "DGS2",
    "10Y": "DGS10",
    "30Y": "DGS30",
}


@dataclass
class QuoteSnapshot:
    symbol: str
    name: str
    close: float
    previous_close: float
    high: Optional[float]
    low: Optional[float]
    history: List[Tuple[datetime, float]]

    @property
    def day_change_pct(self) -> Optional[float]:
        if (
            self.previous_close is None
            or self.close is None
            or math.isnan(self.previous_close)
            or math.isnan(self.close)
            or not self.previous_close
        ):
            return None
        return (self.close / self.previous_close - 1.0) * 100.0

    def trailing_return(self, sessions_back: int) -> Optional[float]:
        if len(self.history) <= sessions_back:
            return None
        earlier_close = self.history[-(sessions_back + 1)][1]
        if (
            earlier_close is None
            or self.close is None
            or math.isnan(earlier_close)
            or math.isnan(self.close)
            or not earlier_close
        ):
            return None
        return (self.close / earlier_close - 1.0) * 100.0


def unavailable_snapshot(name: str, symbol: str) -> QuoteSnapshot:
    return QuoteSnapshot(
        symbol=symbol,
        name=name,
        close=float("nan"),
        previous_close=float("nan"),
        high=None,
        low=None,
        history=[],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the latest US market close report and send it to Telegram."
    )
    parser.add_argument(
        "--env-file",
        default="",
        help="Optional path to a local env file.",
    )
    parser.add_argument(
        "--state-file",
        default=str(Path(__file__).with_name(".state") / "last_sent.json"),
        help="Path to the local state file.",
    )
    parser.add_argument(
        "--reports-dir",
        default=str(Path(__file__).with_name("reports")),
        help="Directory where Markdown and HTML copies of reports will be saved.",
    )
    parser.add_argument(
        "--send-test-message",
        action="store_true",
        help="Send a short connectivity test instead of a report.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Send even if the same report date was already sent before.",
    )
    parser.add_argument(
        "--schedule-guard",
        action="store_true",
        help="Only proceed when current Australia/Melbourne local time is within the expected schedule window.",
    )
    return parser.parse_args()


def load_env_file(path: Path) -> Dict[str, str]:
    values = {}
    if not path.exists():
        raise FileNotFoundError("Env file not found: {0}".format(path))
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def merged_env(env_file_arg: str) -> Dict[str, str]:
    values = dict(os.environ)
    if env_file_arg:
        env_path = Path(env_file_arg).expanduser()
        if env_path.exists():
            file_values = load_env_file(env_path)
            values.update(file_values)
    return values


def load_state(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(path: Path, payload: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def fetch_json(url: str) -> Dict:
    last_error = None
    for _ in range(1):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
    raise last_error


def fetch_text(url: str) -> str:
    last_error = None
    for _ in range(1):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return response.read().decode("utf-8")
        except Exception as exc:
            last_error = exc
    raise last_error


def fmt_num(value: Optional[float], digits: int = 2) -> str:
    if value is None or math.isnan(value):
        return "暂无可靠数据"
    return "{0:,.{1}f}".format(value, digits)


def fmt_pct(value: Optional[float], digits: int = 2) -> str:
    if value is None or math.isnan(value):
        return "暂无可靠数据"
    sign = "+" if value >= 0 else ""
    return "{0}{1:.{2}f}%".format(sign, value, digits)


def fetch_yahoo_chart(symbol: str, range_value: str = "3mo", interval: str = "1d") -> QuoteSnapshot:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        "?range={range_value}&interval={interval}&includePrePost=false&events=div,splits"
    ).format(symbol=urllib.parse.quote(symbol, safe=""), range_value=range_value, interval=interval)
    payload = fetch_json(url)
    result = payload["chart"]["result"][0]
    meta = result["meta"]
    timestamps = result.get("timestamp") or []
    closes = result["indicators"]["quote"][0]["close"]
    highs = result["indicators"]["quote"][0].get("high") or []
    lows = result["indicators"]["quote"][0].get("low") or []

    history = []
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        history.append((datetime.fromtimestamp(ts, tz=timezone.utc), float(close)))
    if not history:
        raise RuntimeError("No history returned for {0}".format(symbol))

    latest_close = history[-1][1]
    if len(history) < 2:
        raise RuntimeError("Not enough history returned for {0}".format(symbol))

    previous_close = history[-2][1]
    high = None
    low = None
    if highs and highs[-1] is not None:
        high = float(highs[-1])
    if lows and lows[-1] is not None:
        low = float(lows[-1])

    return QuoteSnapshot(
        symbol=symbol,
        name=meta.get("symbol", symbol),
        close=latest_close,
        previous_close=previous_close,
        high=high,
        low=low,
        history=history,
    )


def fetch_many(symbol_map: Dict[str, str]) -> Dict[str, QuoteSnapshot]:
    snapshots = {}
    def load_one(item: Tuple[str, str]) -> Tuple[str, QuoteSnapshot]:
        label, symbol = item
        try:
            snap = fetch_yahoo_chart(symbol)
            snap.name = label
            return label, snap
        except Exception:
            return label, unavailable_snapshot(label, symbol)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        for label, snap in executor.map(load_one, symbol_map.items()):
            snapshots[label] = snap
    return snapshots


def fetch_fred_latest(series_id: str) -> Tuple[Optional[datetime], Optional[float], Optional[datetime], Optional[float]]:
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={0}".format(series_id)
    try:
        text = fetch_text(url)
    except Exception:
        return None, None, None, None
    rows = list(csv.DictReader(text.splitlines()))
    entries = []
    for row in rows:
        value = row.get(series_id, ".")
        if not value or value == ".":
            continue
        try:
            dt = datetime.strptime(row["DATE"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            entries.append((dt, float(value)))
        except (KeyError, ValueError):
            continue
    if not entries:
        return None, None, None, None
    latest = entries[-1]
    previous = entries[-2] if len(entries) >= 2 else (None, None)
    return latest[0], latest[1], previous[0], previous[1]


def previous_trading_label(reference: datetime) -> str:
    return reference.astimezone(timezone.utc).date().isoformat()


def markdown_to_html(markdown_text: str) -> str:
    body = html.escape(markdown_text).replace("\n", "<br>\n")
    title_line = markdown_text.splitlines()[0] if markdown_text else "美股收盘日报"
    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"zh-CN\">\n"
        "<head>\n"
        "  <meta charset=\"utf-8\">\n"
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "  <title>{0}</title>\n"
        "  <style>\n"
        "    body {{ margin: 0; background: #f4f1ea; color: #1f2328; font-family: 'PingFang SC','Noto Sans SC',Arial,sans-serif; line-height: 1.65; }}\n"
        "    .page {{ max-width: 980px; margin: 0 auto; padding: 32px 20px 56px; }}\n"
        "    .card {{ background: #fffdf8; border: 1px solid #e7dfd1; border-radius: 18px; box-shadow: 0 12px 30px rgba(88,66,33,.08); overflow: hidden; }}\n"
        "    .header {{ padding: 24px 28px 12px; background: linear-gradient(135deg,#efe4d1 0%,#f8f5ee 100%); border-bottom: 1px solid #e7dfd1; }}\n"
        "    .eyebrow {{ font-size: 12px; letter-spacing: .08em; text-transform: uppercase; color: #8a6f45; margin-bottom: 8px; }}\n"
        "    h1 {{ margin: 0; font-size: 28px; line-height: 1.2; color: #3e2f1c; }}\n"
        "    .content {{ padding: 24px 28px 32px; font-size: 15px; word-break: break-word; }}\n"
        "    @media (max-width: 640px) {{ .page {{ padding: 16px 12px 28px; }} .header {{ padding: 20px 18px 10px; }} .content {{ padding: 18px 18px 24px; font-size: 14px; }} h1 {{ font-size: 22px; }} }}\n"
        "  </style>\n"
        "</head>\n"
        "<body>\n"
        "  <div class=\"page\">\n"
        "    <article class=\"card\">\n"
        "      <header class=\"header\">\n"
        "        <div class=\"eyebrow\">US Market Close Daily</div>\n"
        "        <h1>{1}</h1>\n"
        "      </header>\n"
        "      <section class=\"content\">{2}</section>\n"
        "    </article>\n"
        "  </div>\n"
        "</body>\n"
        "</html>\n"
    ).format(
        html.escape(title_line),
        html.escape(title_line),
        body,
    )


def split_message(text: str, limit: int = DEFAULT_CHAT_LIMIT) -> List[str]:
    if len(text) <= limit:
        return [text]

    chunks = []
    current = ""
    for block in text.split("\n\n"):
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = block
        while len(current) > limit:
            chunks.append(current[:limit])
            current = current[limit:]
    if current:
        chunks.append(current)
    return chunks


def telegram_api_request(token: str, method: str, payload: Dict[str, str]) -> Dict:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        url="https://api.telegram.org/bot{0}/{1}".format(token, method),
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    chunks = split_message(text)
    total = len(chunks)
    for index, chunk in enumerate(chunks, 1):
        body = chunk
        if total > 1:
            body = "[{0}/{1}]\n{2}".format(index, total, chunk)
        result = telegram_api_request(
            token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": body,
                "disable_web_page_preview": "true",
            },
        )
        if not result.get("ok"):
            raise RuntimeError("Telegram send failed: {0}".format(result))


def send_failure_alert(token: str, chat_id: str, error_text: str) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    body = (
        "美股收盘日报自动任务失败\n"
        "时间: {0}\n"
        "原因: {1}\n"
        "说明: 主日报未成功生成，请稍后手动检查。"
    ).format(timestamp, error_text[:1200])
    try:
        send_telegram_message(token, chat_id, body)
    except Exception:
        pass


def safe_change_label(day_change: Optional[float]) -> str:
    if day_change is None or math.isnan(day_change):
        return "暂无可靠数据"
    if day_change > 1:
        return "明显上涨"
    if day_change > 0.2:
        return "温和上涨"
    if day_change >= -0.2:
        return "窄幅震荡"
    if day_change >= -1:
        return "温和回落"
    return "明显下跌"


def should_run_for_schedule(tz_name: str = "Australia/Melbourne") -> bool:
    now_local = datetime.now(ZoneInfo(tz_name))
    weekday = now_local.weekday()
    if weekday not in (1, 2, 3, 4, 5):
        return False
    total_minutes = now_local.hour * 60 + now_local.minute
    target_minutes = 8 * 60 + 35
    return abs(total_minutes - target_minutes) <= 20


def derive_market_state(index_quotes: Dict[str, QuoteSnapshot], sector_quotes: Dict[str, QuoteSnapshot]) -> str:
    sp = index_quotes["S&P 500"].day_change_pct or 0.0
    ndx = index_quotes["Nasdaq Composite"].day_change_pct or 0.0
    russell = index_quotes["Russell 2000"].day_change_pct or 0.0
    soxx = index_quotes["SOXX"].day_change_pct or 0.0
    if soxx > 2 and ndx > sp and russell > 0:
        return "指数强、半导体领涨、风险偏好回升"
    if sp < 0 and ndx < 0 and russell < 0:
        return "指数普跌、风险偏好回落"
    if ndx > sp and soxx > sp:
        return "科技成长偏强、主线仍在 AI 硬件"
    return "结构性行情延续"


def ranking_lines(quotes: Dict[str, QuoteSnapshot], include_periods: bool = False) -> List[str]:
    ranked = sorted(
        quotes.values(),
        key=lambda item: item.day_change_pct if item.day_change_pct is not None else -999.0,
        reverse=True,
    )
    lines = []
    for index, snap in enumerate(ranked, 1):
        line = "{0}. {1} {2}".format(index, snap.name, fmt_pct(snap.day_change_pct))
        if include_periods:
            line += "｜近5日 {0}｜近1月 {1}".format(
                fmt_pct(snap.trailing_return(5)),
                fmt_pct(snap.trailing_return(21)),
            )
        lines.append(line)
    return lines


def compute_support_resistance(snapshot: QuoteSnapshot) -> Tuple[str, str]:
    prices = [value for _, value in snapshot.history[-50:]]
    if len(prices) < 10:
        return "暂无可靠数据", "暂无可靠数据"
    support = min(prices[-10:])
    resistance = max(prices[-10:])
    return fmt_num(support), fmt_num(resistance)


def trend_label(snapshot: QuoteSnapshot) -> str:
    ret_5 = snapshot.trailing_return(5)
    ret_21 = snapshot.trailing_return(21)
    if ret_5 is None or ret_21 is None:
        return "需要观察"
    if ret_5 > 3 and ret_21 > 8:
        return "短线过热"
    if ret_5 > 1 and ret_21 > 0:
        return "继续强势"
    if ret_5 < -3 and ret_21 < -8:
        return "破位风险"
    if ret_5 < 0 and ret_21 > 0:
        return "回踩支撑"
    if ret_5 > 0 and ret_21 < 0:
        return "低位修复"
    return "需要观察"


@dataclass
class ReportDataset:
    index_quotes: Dict[str, QuoteSnapshot]
    sector_quotes: Dict[str, QuoteSnapshot]
    theme_quotes: Dict[str, QuoteSnapshot]
    asset_quotes: Dict[str, QuoteSnapshot]
    mega_quotes: Dict[str, QuoteSnapshot]
    focus_quotes: Dict[str, QuoteSnapshot]
    yield_rows: Dict[str, Tuple[Optional[float], Optional[float]]]
    latest_yield_date: Optional[datetime]


def fetch_report_dataset() -> ReportDataset:
    index_quotes = fetch_many(INDEX_SYMBOLS)
    sector_quotes = fetch_many(SECTOR_SYMBOLS)
    theme_quotes = fetch_many(THEME_SYMBOLS)
    asset_quotes = fetch_many(ASSET_SYMBOLS)
    mega_quotes = fetch_many(MEGA_CAP_SYMBOLS)
    focus_quotes = fetch_many(FOCUS_SYMBOLS)

    yield_rows = {}
    latest_yield_date = None
    for label, series_id in YIELD_SERIES.items():
        latest_dt, latest_val, prev_dt, prev_val = fetch_fred_latest(series_id)
        yield_rows[label] = (latest_val, prev_val)
        if latest_dt and (latest_yield_date is None or latest_dt > latest_yield_date):
            latest_yield_date = latest_dt

    return ReportDataset(
        index_quotes=index_quotes,
        sector_quotes=sector_quotes,
        theme_quotes=theme_quotes,
        asset_quotes=asset_quotes,
        mega_quotes=mega_quotes,
        focus_quotes=focus_quotes,
        yield_rows=yield_rows,
        latest_yield_date=latest_yield_date,
    )


def build_report_messages(dataset: ReportDataset) -> Tuple[str, List[str]]:
    index_quotes = dataset.index_quotes
    sector_quotes = dataset.sector_quotes
    theme_quotes = dataset.theme_quotes
    asset_quotes = dataset.asset_quotes
    mega_quotes = dataset.mega_quotes
    focus_quotes = dataset.focus_quotes
    yield_rows = dataset.yield_rows

    report_date = previous_trading_label(index_quotes["S&P 500"].history[-1][0])
    title = "{0}{1}".format(REPORT_TITLE_PREFIX, report_date)

    strongest_sector = ranking_lines(sector_quotes)[0]
    weakest_sector = ranking_lines(sector_quotes)[-1]
    market_state = derive_market_state(index_quotes, sector_quotes)

    sp = index_quotes["S&P 500"]
    nasdaq = index_quotes["Nasdaq Composite"]
    dow = index_quotes["Dow Jones"]
    russell = index_quotes["Russell 2000"]
    soxx = index_quotes["SOXX"]
    qqq = index_quotes["QQQ"]
    iwm = index_quotes["IWM"]

    mega_ranked = ranking_lines(mega_quotes)
    focus_ranked = ranking_lines(focus_quotes)

    index_table = [
        "| 指数 | 收盘点位 | 涨跌幅 | 日内高低点 | 近5日 | 近1月 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name in ["Dow Jones", "S&P 500", "Nasdaq Composite", "QQQ", "Russell 2000", "IWM", "SOXX", "VIX"]:
        snap = index_quotes[name]
        high_low = "{0} / {1}".format(fmt_num(snap.high), fmt_num(snap.low))
        index_table.append(
            "| {0} | {1} | {2} | {3} | {4} | {5} |".format(
                snap.name,
                fmt_num(snap.close),
                fmt_pct(snap.day_change_pct),
                high_low,
                fmt_pct(snap.trailing_return(5)),
                fmt_pct(snap.trailing_return(21)),
            )
        )

    yield_table = [
        "| 项目 | 最新水平 | 前一交易日 | 日变化 |",
        "| --- | --- | --- | --- |",
    ]
    for label in ["2Y", "10Y", "30Y"]:
        latest_val, prev_val = yield_rows[label]
        delta = None
        if latest_val is not None and prev_val is not None:
            delta = (latest_val - prev_val) * 100
        yield_table.append(
            "| {0} 美债收益率 | {1}% | {2}% | {3}bp |".format(
                label,
                fmt_num(latest_val),
                fmt_num(prev_val),
                fmt_num(delta),
            )
        )
    latest_2y = yield_rows["2Y"][0]
    latest_10y = yield_rows["10Y"][0]
    latest_30y = yield_rows["30Y"][0]
    curve_2_10 = None
    curve_10_30 = None
    if latest_2y is not None and latest_10y is not None:
        curve_2_10 = (latest_10y - latest_2y) * 100
    if latest_10y is not None and latest_30y is not None:
        curve_10_30 = (latest_30y - latest_10y) * 100
    yield_table.append("| 2Y-10Y 利差 | {0}bp | 暂无可靠数据 | 暂无可靠数据 |".format(fmt_num(curve_2_10)))
    yield_table.append("| 10Y-30Y 利差 | {0}bp | 暂无可靠数据 | 暂无可靠数据 |".format(fmt_num(curve_10_30)))

    asset_table = [
        "| 资产 | 最新价格 | 涨跌幅 |",
        "| --- | --- | --- |",
    ]
    for name in ASSET_SYMBOLS:
        snap = asset_quotes[name]
        asset_table.append("| {0} | {1} | {2} |".format(name, fmt_num(snap.close), fmt_pct(snap.day_change_pct)))

    score_index = 4 if (sp.day_change_pct or 0) > 0 and (nasdaq.day_change_pct or 0) > 0 else 2
    score_breadth = 4 if (iwm.day_change_pct or 0) > 0 and (theme_quotes["等权标普"].day_change_pct or 0) > 0 else 2
    score_ai = 5 if (soxx.day_change_pct or 0) > 1 else 3
    score_software = 4 if (theme_quotes["软件"].day_change_pct or 0) > (sp.day_change_pct or 0) else 2
    score_rates = 2 if latest_10y is not None and latest_10y < 4.6 else 4
    stage = "强趋势上涨" if (sp.day_change_pct or 0) > 0 and (nasdaq.day_change_pct or 0) > 0 else "高位震荡"

    msg1 = "\n".join(
        [
            title,
            "",
            "🚦盘后总信号",
            "阶段：{0}".format(stage),
            "状态：{0}".format(market_state),
            "仓位温度：{0}".format("中高" if stage == "强趋势上涨" else "中"),
            "",
            "⚡1分钟核心结论",
            "1. 标普：{0}，纳指：{1}。".format(fmt_pct(sp.day_change_pct), fmt_pct(nasdaq.day_change_pct)),
            "2. QQQ：{0}，SOXX：{1}。".format(fmt_pct(qqq.day_change_pct), fmt_pct(soxx.day_change_pct)),
            "3. 小盘 IWM：{0}，宽度 {1}。".format(fmt_pct(iwm.day_change_pct), "改善" if score_breadth >= 4 else "一般"),
            "4. 10Y 美债：{0}%。".format(fmt_num(latest_10y)),
            "5. DXY：{0}，BTC：{1}。".format(fmt_pct(asset_quotes["DXY 美元指数"].day_change_pct), fmt_pct(asset_quotes["比特币"].day_change_pct)),
            "6. 最强板块：{0}".format(strongest_sector),
            "7. 最弱板块：{0}".format(weakest_sector),
            "8. 七巨头最强：{0}".format(mega_ranked[0]),
            "9. 七巨头最弱：{0}".format(mega_ranked[-1]),
            "",
            "🧭交易方向评分",
            "指数强度：{0}/5".format(score_index),
            "市场宽度：{0}/5".format(score_breadth),
            "AI主线：{0}/5".format(score_ai),
            "软件强弱：{0}/5".format(score_software),
            "利率压力：{0}/5".format(score_rates),
            "",
            "一句话：指数{0}、宽度{1}、AI主线{2}。".format(
                "偏强" if score_index >= 4 else "一般",
                "改善" if score_breadth >= 4 else "有限",
                "仍主导" if score_ai >= 5 else "可观察轮动",
            ),
        ]
    )

    msg2 = "\n".join(
        [
            "📊核心数据快照",
            "美股：",
            "- Dow {0}".format(fmt_pct(dow.day_change_pct)),
            "- S&P 500 {0}".format(fmt_pct(sp.day_change_pct)),
            "- Nasdaq {0}".format(fmt_pct(nasdaq.day_change_pct)),
            "- QQQ {0} / IWM {1}".format(fmt_pct(qqq.day_change_pct), fmt_pct(iwm.day_change_pct)),
            "- SOXX {0} / IGV {1}".format(fmt_pct(soxx.day_change_pct), fmt_pct(theme_quotes["软件"].day_change_pct)),
            "",
            "宏观：",
            "- 2Y {0}% / 10Y {1}% / 30Y {2}%".format(fmt_num(latest_2y), fmt_num(latest_10y), fmt_num(latest_30y)),
            "- DXY {0}".format(fmt_pct(asset_quotes["DXY 美元指数"].day_change_pct)),
            "- 黄金 {0} / WTI {1}".format(fmt_pct(asset_quotes["黄金"].day_change_pct), fmt_pct(asset_quotes["WTI 原油"].day_change_pct)),
            "- BTC {0} / ETH {1}".format(fmt_pct(asset_quotes["比特币"].day_change_pct), fmt_pct(asset_quotes["以太坊"].day_change_pct)),
            "",
            "板块与风格：",
            *["- {0}".format(line) for line in ranking_lines(sector_quotes, include_periods=False)[:5]],
            "- 软件 IGV {0}，等权 RSP {1}。".format(
                fmt_pct(theme_quotes["软件"].day_change_pct),
                fmt_pct(theme_quotes["等权标普"].day_change_pct),
            ),
            "",
            "重点股票：",
            *["- {0}".format(line) for line in focus_ranked[:10]],
        ]
    )

    spy = fetch_yahoo_chart("SPY")
    spy_support, spy_resistance = compute_support_resistance(spy)
    qqq_support, qqq_resistance = compute_support_resistance(qqq)
    smh_support, smh_resistance = compute_support_resistance(theme_quotes["半导体"])

    msg3 = "\n".join(
        [
            "🧩交易计划与风险",
            "多头更有利条件：",
            "- QQQ 继续强于 SPY",
            "- SOXX 继续强于 QQQ",
            "- IWM 与 RSP 同步走强",
            "- 10Y 不上破 4.60%",
            "- 软件 IGV 开始补涨",
            "",
            "风险条件：",
            "- 10Y 上冲 4.60%-4.70%",
            "- SOXX 转弱且弱于 QQQ",
            "- IWM 回落，宽度恶化",
            "- DXY 走强压制风险偏好",
            "- 高位半导体出现利好钝化",
            "",
            "关键位：",
            "- SPY 支撑/压力：{0} / {1}".format(spy_support, spy_resistance),
            "- QQQ 支撑/压力：{0} / {1}".format(qqq_support, qqq_resistance),
            "- SMH 支撑/压力：{0} / {1}".format(smh_support, smh_resistance),
            "",
            "明日观察清单：",
            "1. 10Y 美债是否再上 4.60%",
            "2. SOXX 是否继续强于 QQQ",
            "3. IGV 是否开始补涨",
            "4. IWM / RSP 是否继续同步走强",
            "5. NVDA / AVGO / MRVL / VRT / CEG 结构是否维持",
            "",
            "来源：",
            "- Yahoo Finance: https://finance.yahoo.com/",
            "- FRED: https://fred.stlouisfed.org/",
        ]
    )

    return title, [msg1, msg2, msg3]


def build_detailed_report(dataset: ReportDataset) -> Tuple[str, str]:
    index_quotes = dataset.index_quotes
    sector_quotes = dataset.sector_quotes
    theme_quotes = dataset.theme_quotes
    asset_quotes = dataset.asset_quotes
    mega_quotes = dataset.mega_quotes
    focus_quotes = dataset.focus_quotes
    yield_rows = dataset.yield_rows

    report_date = previous_trading_label(index_quotes["S&P 500"].history[-1][0])
    title = "{0}{1}".format(REPORT_TITLE_PREFIX, report_date)

    sp = index_quotes["S&P 500"]
    nasdaq = index_quotes["Nasdaq Composite"]
    dow = index_quotes["Dow Jones"]
    russell = index_quotes["Russell 2000"]
    qqq = index_quotes["QQQ"]
    iwm = index_quotes["IWM"]
    soxx = index_quotes["SOXX"]
    vix = index_quotes["VIX"]

    latest_2y = yield_rows["2Y"][0]
    latest_10y = yield_rows["10Y"][0]
    latest_30y = yield_rows["30Y"][0]
    curve_2_10 = None
    curve_10_30 = None
    if latest_2y is not None and latest_10y is not None:
        curve_2_10 = (latest_10y - latest_2y) * 100
    if latest_10y is not None and latest_30y is not None:
        curve_10_30 = (latest_30y - latest_10y) * 100

    strongest_sector = ranking_lines(sector_quotes)[0]
    weakest_sector = ranking_lines(sector_quotes)[-1]
    strongest_theme = ranking_lines(theme_quotes)[0]
    weakest_theme = ranking_lines(theme_quotes)[-1]
    market_state = derive_market_state(index_quotes, sector_quotes)

    spy = fetch_yahoo_chart("SPY")
    spy_support, spy_resistance = compute_support_resistance(spy)
    qqq_support, qqq_resistance = compute_support_resistance(qqq)
    smh_support, smh_resistance = compute_support_resistance(theme_quotes["半导体"])
    igv_support, igv_resistance = compute_support_resistance(theme_quotes["软件"])

    stage = "强趋势上涨" if (sp.day_change_pct or 0) > 0 and (nasdaq.day_change_pct or 0) > 0 else "高位震荡"
    score_index = 4 if (sp.day_change_pct or 0) > 0 and (nasdaq.day_change_pct or 0) > 0 else 2
    score_breadth = 4 if (iwm.day_change_pct or 0) > 0 and (theme_quotes["等权标普"].day_change_pct or 0) > 0 else 2
    score_ai = 5 if (soxx.day_change_pct or 0) > 1 else 3
    score_software = 4 if (theme_quotes["软件"].day_change_pct or 0) > (sp.day_change_pct or 0) else 2
    score_rates = 2 if latest_10y is not None and latest_10y < 4.6 else 4

    lines = [
        title,
        "",
        "## 1分钟结论",
        "- 指数表现：标普 {0}，纳指 {1}，小盘 IWM {2}。".format(
            fmt_pct(sp.day_change_pct), fmt_pct(nasdaq.day_change_pct), fmt_pct(iwm.day_change_pct)
        ),
        "- 主线状态：{0}。".format(market_state),
        "- 最强板块：{0}。".format(strongest_sector),
        "- 最弱板块：{0}。".format(weakest_sector),
        "- 利率与美元：10Y {0}%，DXY {1}。".format(
            fmt_num(latest_10y), fmt_pct(asset_quotes["DXY 美元指数"].day_change_pct)
        ),
        "- 七巨头分化：最强 {0}；最弱 {1}。".format(
            ranking_lines(mega_quotes)[0], ranking_lines(mega_quotes)[-1]
        ),
        "- 今日市场状态：{0}。".format("指数偏强但需看宽度跟进" if score_breadth < 4 else "指数与宽度共振，风险偏好改善"),
        "",
        "## 买方晨报评分卡",
        "- 指数强度：{0}/5".format(score_index),
        "- 市场宽度：{0}/5".format(score_breadth),
        "- AI主线强度：{0}/5".format(score_ai),
        "- 软件相对强弱：{0}/5".format(score_software),
        "- 利率压力：{0}/5".format(score_rates),
        "- 当前市场阶段：{0}".format(stage),
        "",
        "## 详细版",
        "",
        "### 0. 今日一句话总结",
        "美股收盘后，标普 {0}、纳指 {1}，半导体 {2}，整体呈现 {3}。".format(
            fmt_pct(sp.day_change_pct), fmt_pct(nasdaq.day_change_pct), fmt_pct(soxx.day_change_pct), market_state
        ),
        "",
        "### 1. 大盘表现总览",
        "- Dow Jones：{0}｜收盘 {1}｜高低 {2}/{3}".format(
            fmt_pct(dow.day_change_pct), fmt_num(dow.close), fmt_num(dow.high), fmt_num(dow.low)
        ),
        "- S&P 500：{0}｜收盘 {1}｜近5日 {2}｜近1月 {3}".format(
            fmt_pct(sp.day_change_pct), fmt_num(sp.close), fmt_pct(sp.trailing_return(5)), fmt_pct(sp.trailing_return(21))
        ),
        "- Nasdaq Composite：{0}｜收盘 {1}｜近5日 {2}｜近1月 {3}".format(
            fmt_pct(nasdaq.day_change_pct), fmt_num(nasdaq.close), fmt_pct(nasdaq.trailing_return(5)), fmt_pct(nasdaq.trailing_return(21))
        ),
        "- QQQ：{0}｜收盘 {1}".format(fmt_pct(qqq.day_change_pct), fmt_num(qqq.close)),
        "- Russell 2000：{0}｜收盘 {1}".format(fmt_pct(russell.day_change_pct), fmt_num(russell.close)),
        "- SOXX：{0}｜收盘 {1}".format(fmt_pct(soxx.day_change_pct), fmt_num(soxx.close)),
        "- VIX：{0}｜收盘 {1}".format(fmt_pct(vix.day_change_pct), fmt_num(vix.close)),
        "",
        "### 2. 盘中走势复盘",
        "- 指数方向：标普 {0}，纳指 {1}，说明科技相对{2}。".format(
            safe_change_label(sp.day_change_pct), safe_change_label(nasdaq.day_change_pct), "偏强" if (nasdaq.day_change_pct or 0) > (sp.day_change_pct or 0) else "一般"
        ),
        "- 小盘与等权：IWM {0}，RSP {1}，宽度{2}。".format(
            fmt_pct(iwm.day_change_pct), fmt_pct(theme_quotes["等权标普"].day_change_pct), "改善" if score_breadth >= 4 else "仍有限"
        ),
        "- 半导体与软件：SMH {0}，IGV {1}，风格上更像{2}。".format(
            fmt_pct(theme_quotes["半导体"].day_change_pct),
            fmt_pct(theme_quotes["软件"].day_change_pct),
            "AI硬件主导" if score_ai >= 5 else "向软件扩散"
        ),
        "",
        "### 3. 宏观环境",
        "- 2Y 美债：{0}%｜10Y：{1}%｜30Y：{2}%".format(fmt_num(latest_2y), fmt_num(latest_10y), fmt_num(latest_30y)),
        "- 曲线：2Y-10Y {0}bp｜10Y-30Y {1}bp".format(fmt_num(curve_2_10), fmt_num(curve_10_30)),
        "- DXY：{0}｜黄金：{1}｜WTI：{2}".format(
            fmt_pct(asset_quotes["DXY 美元指数"].day_change_pct),
            fmt_pct(asset_quotes["黄金"].day_change_pct),
            fmt_pct(asset_quotes["WTI 原油"].day_change_pct),
        ),
        "- BTC：{0}｜ETH：{1}".format(
            fmt_pct(asset_quotes["比特币"].day_change_pct),
            fmt_pct(asset_quotes["以太坊"].day_change_pct),
        ),
        "- FedWatch / 当日宏观数据：暂无可靠数据。",
        "",
        "### 4. 板块表现",
        "- 最强：{0}".format(strongest_sector),
        "- 最弱：{0}".format(weakest_sector),
        "- 近端领先板块：",
        *["  - {0}".format(line) for line in ranking_lines(sector_quotes, include_periods=True)[:5]],
        "",
        "### 5. 主题与风格表现",
        "- 最强主题：{0}".format(strongest_theme),
        "- 最弱主题：{0}".format(weakest_theme),
        "- 软件 IGV：{0}｜半导体 SMH：{1}｜等权 RSP：{2}".format(
            fmt_pct(theme_quotes["软件"].day_change_pct),
            fmt_pct(theme_quotes["半导体"].day_change_pct),
            fmt_pct(theme_quotes["等权标普"].day_change_pct),
        ),
        "",
        "### 6. 市场宽度与参与度",
        "- IWM：{0}｜RSP：{1}。".format(fmt_pct(iwm.day_change_pct), fmt_pct(theme_quotes["等权标普"].day_change_pct)),
        "- 宽度判断：{0}。".format("扩散中" if score_breadth >= 4 else "仍偏权重驱动"),
        "- 20/50/100/200 日参与度：暂无可靠数据。",
        "- 涨跌家数 / 新高新低：暂无可靠数据。",
        "",
        "### 7. 技术面分析",
        "- SPY 支撑/压力：{0} / {1}".format(spy_support, spy_resistance),
        "- QQQ 支撑/压力：{0} / {1}".format(qqq_support, qqq_resistance),
        "- SMH 支撑/压力：{0} / {1}".format(smh_support, smh_resistance),
        "- IGV 支撑/压力：{0} / {1}".format(igv_support, igv_resistance),
        "- 技术结论：若 QQQ 继续强于 SPY、SMH 继续强于 QQQ，则趋势延续；反之先看高位震荡。",
        "",
        "### 8. 重点个股新闻与异动",
        "- 七巨头排序：",
        *["  - {0}".format(line) for line in ranking_lines(mega_quotes)],
        "- 重点关注股前十：",
        *["  - {0}".format(line) for line in ranking_lines(focus_quotes)[:10]],
        "- 公司新闻 / 评级 / 财报：暂无可靠数据。",
        "",
        "### 9. 财报日历与财报解读",
        "- 已公布重点财报：暂无可靠数据。",
        "- 接下来 1-3 个交易日重点财报：暂无可靠数据。",
        "",
        "### 10. 机构观点与资金流",
        "- ETF 资金流 / 期权异动 / 大宗交易：暂无可靠数据。",
        "- 可跟踪官方来源：Yahoo Finance、Nasdaq、公司 IR、SEC。",
        "",
        "### 11. 板块轮动判断",
        "- 当前更像：{0}。".format(stage if score_ai >= 5 else "板块轮动"),
        "- 资金流入：{0}。".format("半导体、AI电力链、核心成长" if score_ai >= 5 else "软件与等权修复"),
        "- 资金流出：{0}。".format("防御板块" if (sector_quotes["公用事业"].day_change_pct or 0) < 0 else "相对弱势消费与地产"),
        "",
        "### 12. 我的重点关注股观察",
        *[
            "- {0}：{1}｜近5日 {2}｜近1月 {3}".format(
                name,
                trend_label(snap),
                fmt_pct(snap.trailing_return(5)),
                fmt_pct(snap.trailing_return(21)),
            )
            for name, snap in list(focus_quotes.items())[:15]
        ],
        "",
        "### 13. 明日交易计划 / 观察清单",
        "- 宏观：10Y 是否重新测试 4.60%-4.70%，DXY 是否继续走强。",
        "- 大盘：QQQ 是否继续强于 SPY，IWM / RSP 是否确认宽度。",
        "- 板块：SMH 是否继续强于 QQQ，IGV 是否补涨。",
        "- 个股：NVDA、AVGO、MRVL、VRT、CEG、MSFT、ORCL、CRM、NOW、CRWD。",
        "- 如果继续上涨：看 SOXX 与 QQQ 同步放量。",
        "- 如果回调：先看 QQQ {0}、SPY {1} 能否守住。".format(qqq_support, spy_support),
        "",
        "### 14. 风险提示",
        "- 美债收益率继续上行。",
        "- 半导体高位拥挤与利好钝化。",
        "- 指数强但内部宽度跟不上。",
        "- 软件或其他成长财报不及预期。",
        "- 美元走强压制风险资产。",
        "",
        "### 15. 最终结论",
        "- 今日市场结论：{0}。".format(market_state),
        "- 当前市场阶段：{0}。".format(stage),
        "- 我的操作倾向：不追高，优先等宽度和利率确认后再决定是否加仓。",
        "- 最值得关注的 5 个信号：",
        "  1. 10Y 是否上破 4.60%",
        "  2. SOXX 是否继续强于 QQQ",
        "  3. IGV 是否出现补涨",
        "  4. IWM 与 RSP 是否同步转强",
        "  5. NVDA / AVGO / MRVL 是否维持强势结构",
        "",
        "## 来源",
        "- Yahoo Finance: https://finance.yahoo.com/",
        "- FRED: https://fred.stlouisfed.org/",
    ]
    return title, "\n".join(lines).strip() + "\n"


def build_report_outputs() -> Tuple[str, List[str], str]:
    dataset = fetch_report_dataset()
    title, messages = build_report_messages(dataset)
    _, detailed_report = build_detailed_report(dataset)
    return title, messages, detailed_report


def save_report_files(report_text: str, reports_dir: Path, report_date: str) -> Tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = reports_dir / "{0}.md".format(report_date)
    html_path = reports_dir / "{0}.html".format(report_date)
    latest_md = reports_dir / "latest.md"
    latest_html = reports_dir / "latest.html"
    markdown_path.write_text(report_text, encoding="utf-8")
    html_path.write_text(markdown_to_html(report_text), encoding="utf-8")
    latest_md.write_text(report_text, encoding="utf-8")
    latest_html.write_text(markdown_to_html(report_text), encoding="utf-8")
    return markdown_path, html_path


def main() -> int:
    args = parse_args()
    env = merged_env(args.env_file)
    token = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID.")

    try:
        if args.schedule_guard and not should_run_for_schedule():
            print("Skipped: outside Melbourne scheduled window.")
            return 0

        if args.send_test_message:
            send_telegram_message(
                token,
                chat_id,
                "Telegram 测试成功\n时间: {0}".format(datetime.now().isoformat(timespec="seconds")),
            )
            return 0

        title, messages, detailed_report_text = build_report_outputs()
        report_date = title.replace(REPORT_TITLE_PREFIX, "", 1)
        reports_dir = Path(args.reports_dir).expanduser()
        markdown_path, html_path = save_report_files(detailed_report_text, reports_dir, report_date)

        state_path = Path(args.state_file).expanduser()
        state = load_state(state_path)
        report_id = hashlib.sha256(detailed_report_text.encode("utf-8")).hexdigest()

        if not args.force and state.get("last_sent_report_date") == report_date:
            print("Latest report for {0} already sent.".format(report_date))
            print("Markdown saved to {0}".format(markdown_path))
            print("HTML saved to {0}".format(html_path))
            return 0

        for message in messages:
            send_telegram_message(token, chat_id, message)
        save_state(
            state_path,
            {
                "last_sent_report_date": report_date,
                "last_sent_report_id": report_id,
                "last_sent_at": datetime.now().isoformat(timespec="seconds"),
                "report_markdown_path": str(markdown_path),
                "report_html_path": str(html_path),
            },
        )
        print("Sent report for {0}".format(report_date))
        print("Markdown saved to {0}".format(markdown_path))
        print("HTML saved to {0}".format(html_path))
        return 0
    except Exception as exc:
        send_failure_alert(token, chat_id, repr(exc))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
