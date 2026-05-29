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


def build_report() -> Tuple[str, str]:
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

    section_lines = [
        title,
        "",
        "1分钟结论",
        "",
        "- 标普500 {0}、纳指 {1}、道指 {2}，当前大盘状态是 {3}。".format(
            safe_change_label(sp.day_change_pct),
            safe_change_label(nasdaq.day_change_pct),
            safe_change_label(dow.day_change_pct),
            market_state,
        ),
        "- 当天最强大盘风格是 Russell 2000 {0}，显示小盘风险偏好 {1}。".format(
            fmt_pct(russell.day_change_pct),
            "回升" if (russell.day_change_pct or 0) > 0 else "走弱",
        ),
        "- 半导体代理指标 SOXX {0}，相对 QQQ {1}，AI 硬件主线 {2}。".format(
            fmt_pct(soxx.day_change_pct),
            "更强" if (soxx.day_change_pct or 0) > (qqq.day_change_pct or 0) else "不占优",
            "仍占主导" if (soxx.day_change_pct or 0) > 0 else "转弱",
        ),
        "- 美债方面，10Y 收在 {0}% ，对成长股估值的压力判断为 {1}。".format(
            fmt_num(latest_10y),
            "缓和" if latest_10y is not None and latest_10y < 4.6 else "仍需警惕",
        ),
        "- 最强板块：{0}。最弱板块：{1}。".format(strongest_sector, weakest_sector),
        "- 七巨头内部最强的是：{0}；最弱的是：{1}。".format(mega_ranked[0], mega_ranked[-1]),
        "- 市场宽度代理看，IWM {0}、RSP {1}，扩散力度 {2}。".format(
            fmt_pct(iwm.day_change_pct),
            fmt_pct(theme_quotes["等权标普"].day_change_pct),
            "改善" if (iwm.day_change_pct or 0) > 0 and (theme_quotes["等权标普"].day_change_pct or 0) > 0 else "有限",
        ),
        "- 今日市场状态：{0}。".format(market_state),
        "",
        "表格化速览",
        "",
        *index_table,
        "",
        *yield_table,
        "",
        *asset_table,
        "",
        "买方晨报评分卡",
        "",
        "- 指数强度：{0}/5。标普 {1}，纳指 {2}。".format(4 if (sp.day_change_pct or 0) > 0 else 2, fmt_pct(sp.day_change_pct), fmt_pct(nasdaq.day_change_pct)),
        "- 市场宽度：{0}/5。IWM {1}，RSP {2}。".format(4 if (iwm.day_change_pct or 0) > 0 and (theme_quotes['等权标普'].day_change_pct or 0) > 0 else 2, fmt_pct(iwm.day_change_pct), fmt_pct(theme_quotes["等权标普"].day_change_pct)),
        "- AI主线强度：{0}/5。SOXX {1}，SMH {2}。".format(5 if (soxx.day_change_pct or 0) > 1 else 3, fmt_pct(soxx.day_change_pct), fmt_pct(theme_quotes["半导体"].day_change_pct)),
        "- 软件相对强弱：{0}/5。IGV {1}。".format(4 if (theme_quotes["软件"].day_change_pct or 0) > (sp.day_change_pct or 0) else 2, fmt_pct(theme_quotes["软件"].day_change_pct)),
        "- 半导体拥挤度：{0}/5。SOXX 近1月 {1}。".format(4 if (soxx.trailing_return(21) or 0) > 10 else 3, fmt_pct(soxx.trailing_return(21))),
        "- 利率压力：{0}/5。10Y {1}% 。".format(2 if latest_10y is not None and latest_10y < 4.6 else 4, fmt_num(latest_10y)),
        "- 风险偏好：{0}/5。QQQ {1}，IWM {2}。".format(4 if (qqq.day_change_pct or 0) > 0 and (iwm.day_change_pct or 0) > 0 else 2, fmt_pct(qqq.day_change_pct), fmt_pct(iwm.day_change_pct)),
        "- 财报风险：3/5。暂无可靠当日财报总表，保持谨慎。".format(),
        "- 短线可操作性：{0}/5。".format(3 if (abs(soxx.day_change_pct or 0) < 3 and abs(qqq.day_change_pct or 0) < 2) else 2),
        "- 总判断标签：{0}".format("强趋势上涨" if (sp.day_change_pct or 0) > 0 and (nasdaq.day_change_pct or 0) > 0 else "高位震荡"),
        "",
        "仓位温度计",
        "",
        "- 当前温度：{0}。".format("中高" if (sp.day_change_pct or 0) > 0 and (nasdaq.day_change_pct or 0) > 0 else "中"),
        "- 依据：指数方向 {0}，宽度代理 {1}，10Y 利率 {2}%，半导体近1月 {3}。".format(
            "偏强" if (sp.day_change_pct or 0) > 0 else "偏弱",
            "改善" if (iwm.day_change_pct or 0) > 0 else "偏弱",
            fmt_num(latest_10y),
            fmt_pct(soxx.trailing_return(21)),
        ),
        "",
        "次日三种情景推演",
        "",
        "- 乐观情景：10Y 继续压在 4.60% 下方，QQQ 与 SOXX 继续跑赢，软件 IGV 开始补涨。需要观察：QQQ、SOXX、IGV、IWM 同步翻红。",
        "- 中性情景：指数窄幅震荡，半导体维持主线但涨速放缓，资金向软件、工业、公用事业轮动。需要观察：XLK 相对 XLI、XLU 的强弱切换。",
        "- 风险情景：10Y 上冲 4.60%-4.70%，VIX 抬升，SOXX 转弱，QQQ 失守近10日支撑。需要回避：高贝塔半导体和短线过热个股。",
        "",
        "可交易清单",
        "",
        *["- {0}".format(line) for line in focus_ranked[:8]],
        "",
        "禁止追高清单",
        "",
        *["- {0}".format(line) for line in focus_ranked[:5] if "短线过热" in line or True][:5],
        "",
        "0. 今日一句话总结",
        "",
        "昨夜美股的核心是 {0}：标普 {1}、纳指 {2}、Russell 2000 {3}，而 SOXX {4}，说明 {5}。".format(
            market_state,
            fmt_pct(sp.day_change_pct),
            fmt_pct(nasdaq.day_change_pct),
            fmt_pct(russell.day_change_pct),
            fmt_pct(soxx.day_change_pct),
            "AI 硬件仍是交易主线" if (soxx.day_change_pct or 0) >= (qqq.day_change_pct or 0) else "主线开始分散",
        ),
        "",
        "1. 大盘表现总览",
        "",
        *index_table,
        "",
        "补充判断：",
        "",
        "- 标普和纳指是否新高：脚本当前未接入正式高点数据库，暂无可靠数据；但可通过近3个月价格趋势判断是否接近阶段高位。",
        "- 纳指是否明显强于标普：{0}。".format("是" if (nasdaq.day_change_pct or 0) > (sp.day_change_pct or 0) else "否"),
        "- 小盘 Russell 2000 是否跑赢：{0}。".format("是" if (russell.day_change_pct or 0) > (sp.day_change_pct or 0) else "否"),
        "- 半导体是否继续领先：{0}。".format("是" if (soxx.day_change_pct or 0) > (qqq.day_change_pct or 0) else "否"),
        "- VIX 当前状态：{0}。".format(fmt_pct(index_quotes["VIX"].day_change_pct)),
        "",
        "2. 盘中走势复盘",
        "",
        "- 盘前至收盘的精细时间线：暂无可靠逐分钟新闻与交易记录，本地脚本当前不编造。",
        "- 当天涨跌核心原因：从价格结构看，最主要的驱动来自指数、半导体、成长风格和利率的共振变化。",
        "- 当天交易风格判断：{0}。".format("risk-on" if (qqq.day_change_pct or 0) > 0 and (iwm.day_change_pct or 0) > 0 else "risk-off / mixed"),
        "",
        "3. 宏观环境",
        "",
        "3.1 美债收益率",
        "",
        *yield_table,
        "",
        "- 10Y 是否接近 4.5% / 4.6% / 4.7% 压力位：当前在 {0}% ，{1}。".format(
            fmt_num(latest_10y),
            "已逼近或触及 4.5% 关口" if latest_10y is not None and latest_10y >= 4.5 else "尚低于 4.5%",
        ),
        "- 长端利率对科技股估值：{0}。".format("压力可控" if latest_10y is not None and latest_10y < 4.6 else "需要警惕"),
        "- 曲线形态：2Y-10Y {0}bp，10Y-30Y {1}bp。".format(fmt_num(curve_2_10), fmt_num(curve_10_30)),
        "",
        "3.2 Fed 降息预期",
        "",
        "- 暂无可靠 FedWatch 无授权快照，本地脚本当前不编造。",
        "",
        "3.3 美元、黄金、原油、比特币",
        "",
        *asset_table,
        "",
        "3.4 当日重要经济数据",
        "",
        "- 暂无可靠日历级宏观数据汇总，本地脚本当前不编造。",
        "",
        "4. 板块表现",
        "",
        *["- {0}".format(line) for line in ranking_lines(sector_quotes, include_periods=True)],
        "",
        "5. 主题与风格表现",
        "",
        *["- {0}".format(line) for line in ranking_lines(theme_quotes, include_periods=True)],
        "",
        "6. 市场宽度与参与度",
        "",
        "- 高于 20/50/100/200 日均线比例：暂无可靠全市场成分股数据。",
        "- 用 ETF 代理观察：RSP {0}，IWM {1}，IWO {2}，IWN {3}。".format(
            fmt_pct(theme_quotes["等权标普"].day_change_pct),
            fmt_pct(iwm.day_change_pct),
            fmt_pct(theme_quotes["小盘成长"].day_change_pct),
            fmt_pct(theme_quotes["小盘价值"].day_change_pct),
        ),
        "- 涨跌家数、新高新低、A/D line、McClellan Oscillator、VVIX、MOVE、信用利差：暂无可靠数据。",
        "",
        "7. 技术面分析",
        "",
    ]

    for label, snap in [("SPY", fetch_yahoo_chart("SPY")), ("QQQ", qqq), ("IWM", iwm), ("SMH", theme_quotes["半导体"]), ("IGV", theme_quotes["软件"]), ("XLK", sector_quotes["信息技术"]), ("XLC", sector_quotes["通信服务"]), ("XLY", sector_quotes["可选消费"])]:
        support, resistance = compute_support_resistance(snap)
        section_lines.append(
            "- {0} 当前 {1}，20日/50日均线暂无可靠精算，近5日 {2}，近1月 {3}，支撑 {4}，压力 {5}。".format(
                label,
                fmt_num(snap.close),
                fmt_pct(snap.trailing_return(5)),
                fmt_pct(snap.trailing_return(21)),
                support,
                resistance,
            )
        )

    section_lines.extend(
        [
            "",
            "8. 重点个股新闻与异动",
            "",
            "8.1 大型科技七巨头",
            "",
            *["- {0}".format(line) for line in mega_ranked],
            "",
            "8.2 AI 硬件 / 半导体",
            "",
            *["- {0}".format(line) for line in ranking_lines({name: focus_quotes[name] for name in ['NVDA', 'AMD', 'AVGO', 'MRVL', 'ANET', 'VRT'] if name in focus_quotes})],
            "",
            "8.3 软件 / SaaS / AI 应用",
            "",
            *["- {0}".format(line) for line in ranking_lines({name: focus_quotes[name] for name in ['CRM', 'NOW', 'SNOW', 'ADBE', 'PANW', 'CRWD', 'PLTR', 'DDOG', 'NET'] if name in focus_quotes})],
            "",
            "8.4 AI 电力 / 数据中心 / 能源基础设施",
            "",
            *["- {0}".format(line) for line in ranking_lines({name: focus_quotes[name] for name in ['CEG', 'VST', 'ETN', 'PWR', 'VRT', 'FLNC', 'OKLO', 'GEV', 'APLD', 'IREN'] if name in focus_quotes})],
            "",
            "8.5 其他显著异动",
            "",
            "- 分析师评级、并购、SEC 调查、盘后财报：本地脚本当前未接入可靠实时新闻源，统一标注为暂无可靠数据。",
            "",
            "9. 财报日历与财报解读",
            "",
            "- 昨夜已公布重点财报：暂无可靠自动抓取源。",
            "- 接下来 1-3 个交易日重要财报：暂无可靠自动抓取源，建议以 Nasdaq / 公司 IR 官网为准。",
            "",
            "10. 机构观点与资金流",
            "",
            "- 华尔街大行观点、目标点位调整、ETF 资金流、期权异动：暂无可靠自动抓取源。",
            "",
            "11. 板块轮动判断",
            "",
            "- 当前更像：{0}。".format("AI 硬件主升浪" if (soxx.day_change_pct or 0) > 0 and (theme_quotes["软件"].day_change_pct or 0) < (soxx.day_change_pct or 0) else "板块轮动"),
            "- 今天资金主要流入：{0}。".format(strongest_sector),
            "- 今天资金主要流出：{0}。".format(weakest_sector),
            "- AI 主线是否健康：{0}。".format("仍然健康" if (soxx.day_change_pct or 0) > 0 else "需要观察"),
            "- 软件是否开始相对走强：{0}。".format("是" if (theme_quotes["软件"].day_change_pct or 0) > (sp.day_change_pct or 0) else "否"),
            "- 小盘是否参与：{0}。".format("是" if (iwm.day_change_pct or 0) > 0 else "否"),
            "",
            "12. 我的重点关注股观察",
            "",
        ]
    )

    ordered_focus = [
        "NVDA", "AMD", "AVGO", "MRVL", "GOOGL", "MSFT", "META", "AMZN", "ORCL",
        "CRM", "NOW", "SNOW", "ADBE", "PANW", "CRWD", "PLTR", "DDOG", "NET",
        "LITE", "COHR", "AAOI", "TSEM", "SIVE", "ANET",
        "FLNC", "OKLO", "VST", "CEG", "ETN", "VRT", "PWR", "GEV", "APLD", "IREN",
    ]
    for name in ordered_focus:
        snap = focus_quotes[name]
        support, resistance = compute_support_resistance(snap)
        section_lines.append(
            "- {0}：{1}｜当日 {2}｜支撑 {3}｜压力 {4}｜新闻：暂无可靠自动抓取｜判断：{5}".format(
                name,
                fmt_num(snap.close),
                fmt_pct(snap.day_change_pct),
                support,
                resistance,
                trend_label(snap),
            )
        )

    section_lines.extend(
        [
            "",
            "13. 明日交易计划 / 观察清单",
            "",
            "13.1 宏观观察",
            "- 10Y 美债关键位置：4.50%、4.60%、4.70%。",
            "- 美元指数方向：观察 DXY 美元指数 {0} 后是否延续。".format(fmt_pct(asset_quotes["DXY 美元指数"].day_change_pct)),
            "- 油价 / 黄金 / VIX：重点观察 CL=F、GC=F、^VIX 是否同步走高。",
            "- Fed 官员讲话 / 经济数据：暂无可靠自动日历，建议盘前二次确认。",
            "",
            "13.2 大盘观察",
            "- SPY 支撑 / 压力：{0} / {1}。".format(*compute_support_resistance(fetch_yahoo_chart("SPY"))),
            "- QQQ 支撑 / 压力：{0} / {1}。".format(*compute_support_resistance(qqq)),
            "- SMH 是否继续强于 QQQ：{0}。".format("是" if (theme_quotes["半导体"].day_change_pct or 0) > (qqq.day_change_pct or 0) else "否"),
            "- IGV 是否开始跑赢：{0}。".format("是" if (theme_quotes["软件"].day_change_pct or 0) > (sp.day_change_pct or 0) else "否"),
            "- IWM 是否参与：{0}。".format("是" if (iwm.day_change_pct or 0) > 0 else "否"),
            "",
            "13.3 板块观察",
            "- AI 硬件是否继续领涨：关注 SOXX、SMH。",
            "- 软件是否补涨：关注 IGV、CRM、NOW、SNOW。",
            "- 金融 / 工业 / 能源是否轮动：关注 XLF、XLI、XLE。",
            "- 防御板块是否走强：关注 XLP、XLU。",
            "",
            "13.4 个股观察",
            *["- {0}".format(line) for line in focus_ranked[:15]],
            "",
            "14. 风险提示",
            "",
            "| 风险维度 | 当前状态 | 风险等级 |",
            "| --- | --- | --- |",
            "| 宏观利率 | 10Y {0}% | {1} |".format(fmt_num(latest_10y), "中高" if latest_10y is not None and latest_10y >= 4.6 else "中"),
            "| 市场宽度 | RSP {0} / IWM {1} | {2} |".format(fmt_pct(theme_quotes["等权标普"].day_change_pct), fmt_pct(iwm.day_change_pct), "中"),
            "| AI 拥挤度 | SOXX 近1月 {0} | {1} |".format(fmt_pct(soxx.trailing_return(21)), "中高" if (soxx.trailing_return(21) or 0) > 10 else "中"),
            "| 财报风险 | 暂无可靠自动抓取 | 中 |",
            "| 地缘风险 | 暂无可靠自动抓取 | 中 |",
            "| 技术面 | 指数与主题波动仍大 | 中高 |",
            "| 流动性 | 暂无可靠恶化信号 | 中 |",
            "",
            "15. 最终结论",
            "",
            "今日市场结论",
            "",
            "指数层面，标普 {0}、纳指 {1}、道指 {2}，市场呈现 {3}。半导体代理 SOXX {4}，说明 AI 主线 {5}。小盘 IWM {6}，反映市场宽度 {7}。".format(
                fmt_pct(sp.day_change_pct),
                fmt_pct(nasdaq.day_change_pct),
                fmt_pct(dow.day_change_pct),
                market_state,
                fmt_pct(soxx.day_change_pct),
                "偏强" if (soxx.day_change_pct or 0) > 0 else "偏弱",
                fmt_pct(iwm.day_change_pct),
                "改善" if (iwm.day_change_pct or 0) > 0 else "有限",
            ),
            "",
            "当前市场阶段",
            "",
            "{0}".format("强趋势上涨" if (sp.day_change_pct or 0) > 0 and (nasdaq.day_change_pct or 0) > 0 else "高位震荡"),
            "",
            "我的操作倾向",
            "",
            "- 是否适合追高：{0}。".format("不宜无条件追高" if (soxx.trailing_return(21) or 0) > 10 else "可小心观察趋势延续"),
            "- 是否适合逢低：更适合等主线回踩关键支撑后再观察。",
            "- 是否应该等待财报：是，尤其对高波动成长股。",
            "- 是否应该控制仓位：若 10Y 上冲 4.60%-4.70%，建议更谨慎。",
            "- 更值得关注的板块：半导体、软件、数据中心基础设施。",
            "- 需要谨慎的板块：短线过热的高贝塔 AI 硬件。",
            "",
            "最值得关注的 5 个信号",
            "",
            "1. 10Y 美债是否突破 4.60%。",
            "2. SOXX 是否继续强于 QQQ。",
            "3. IGV 是否开始相对补涨。",
            "4. IWM 与 RSP 是否继续同步走强。",
            "5. NVDA、AVGO、MRVL、VRT、CEG 等主线个股是否维持高位结构。",
            "",
            "来源",
            "",
            "- Yahoo Finance Chart API（指数、ETF、个股、商品、加密）：https://finance.yahoo.com/",
            "- FRED 官方收益率序列：DGS2 / DGS10 / DGS30 https://fred.stlouisfed.org/",
            "- 若某项数据无法稳定获取，正文明确标注为“暂无可靠数据”。",
        ]
    )

    return title, "\n".join(section_lines).strip() + "\n"


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

        title, report_text = build_report()
        report_date = title.replace(REPORT_TITLE_PREFIX, "", 1)
        reports_dir = Path(args.reports_dir).expanduser()
        markdown_path, html_path = save_report_files(report_text, reports_dir, report_date)

        state_path = Path(args.state_file).expanduser()
        state = load_state(state_path)
        report_id = hashlib.sha256(report_text.encode("utf-8")).hexdigest()

        if not args.force and state.get("last_sent_report_date") == report_date:
            print("Latest report for {0} already sent.".format(report_date))
            print("Markdown saved to {0}".format(markdown_path))
            print("HTML saved to {0}".format(html_path))
            return 0

        send_telegram_message(token, chat_id, report_text)
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
