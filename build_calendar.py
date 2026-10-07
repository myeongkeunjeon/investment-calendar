"""투자 일정 캘린더(.ics) 생성기.

하는 일:
  1. 위키백과에서 S&P 500 구성종목 목록(티커·회사명·섹터)과 그중 S&P 100 종목을 가져온다.
  2. 나스닥 실적 캘린더 API에서 지난 100일~앞으로 90일의 실적 발표 일정을 가져와
     S&P 500 종목만 고른다. 발표가 끝난 일정에는 실제 EPS와 예상 대비 결과를 붙인다. 아직 확정되지 않은 다음 분기는 13주 간격으로
     '예상일'을 계산해 약 6개월 앞까지 채운다.
  3. 연준(Fed) 홈페이지에서 FOMC 회의 일정을 가져온다.
  4. 한국어 설명(테마/섹터, 한국시간 발표 시간대)과 영어 원문 링크를 붙여
     docs/calendar.ics 로 저장한다.
  5. 웹앱용으로 미국 상장사 전체 실적 일정(docs/market.json)과
     달러/원 환율(docs/fx.json)도 함께 저장한다.

어느 한 곳에서 가져오기에 실패해도, 지난번에 저장해 둔 데이터를 그대로 써서
캘린더가 비지 않도록 한다. 개인 보유 종목 같은 비공개 정보는 다루지 않는다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar, Event, vDuration

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"

WIKI_URL = "https://en.wikipedia.org/wiki/S%26P_100"
WIKI_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NASDAQ_SURPRISE_API = "https://api.nasdaq.com/api/company/{symbol}/earnings-surprise"
SEC_FILINGS_PAGE = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={symbol}"
                    "&type=8-K&dateb=&owner=include&count=10")
RESULT_LOOKBACK_DAYS = 14   # 발표 후 며칠까지 결과(실제 EPS)를 따로 찾아볼지
RESULT_LOOKUP_LIMIT = 60    # 한 번 실행에 결과를 따로 찾아보는 최대 종목 수
NASDAQ_API = "https://api.nasdaq.com/api/calendar/earnings?date={day}"
NASDAQ_EARNINGS_PAGE = "https://www.nasdaq.com/market-activity/stocks/{symbol}/earnings"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"  # 미국 기준금리(목표 범위) 공식 통계
FED_BASE = "https://www.federalreserve.gov"
FEDWATCH_URL = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"
FX_URLS = [  # 유럽중앙은행(ECB) 기준환율을 제공하는 무료 공개 API (키 필요 없음)
    "https://api.frankfurter.dev/v1/{start}..{end}?base=USD&symbols=KRW",
    "https://api.frankfurter.app/{start}..{end}?from=USD&to=KRW",
]
FX_PAGE = "https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html"

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")

DAYS_AHEAD = 90        # 오늘부터 며칠 뒤까지 나스닥에서 확정 실적 일정을 조회할지
DAYS_BACK = 100        # 직전 분기 발표일을 알기 위해 며칠 전까지 거꾸로 조회할지
ESTIMATE_DAYS = 183    # 확정 전 '예상일'을 오늘부터 며칠 뒤(약 6개월)까지 보여 줄지
QUARTER_DAYS = 91      # 분기 간격 13주. 7의 배수라 요일이 그대로 유지된다
KEEP_PAST_DAYS = 120   # 지난 일정을 며칠까지 캘린더에 남겨 둘지

# 실적 발표 시각은 나스닥이 "장 전/장 후"로만 알려 준다. 정확한 시각을 지어내지 않고,
# 대부분의 회사가 발표하는 시간대(범위)로 안내한다. 서머타임은 자동 반영된다.
PRE_MARKET_ET = ((6, 0), (8, 30))     # 장 시작 전: 미 동부 06:00~08:30
AFTER_HOURS_ET = ((16, 0), (17, 0))   # 장 마감 후: 미 동부 16:00~17:00
MARKET_OPEN_ET = (9, 30)              # 미국 정규장 개장
FOMC_STATEMENT_ET = (14, 0)  # FOMC 성명 발표: 미 동부 14:00 (기자회견 14:30)

USER_AGENT = (
    "investment-calendar/1.0 (https://github.com/myeongkeunjeon/investment-calendar; "
    "public schedule aggregator)"
)
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}

SECTOR_KO = {
    "Information Technology": "정보기술",
    "Health Care": "헬스케어",
    "Financials": "금융",
    "Consumer Discretionary": "경기소비재",
    "Consumer Staples": "필수소비재",
    "Communication Services": "커뮤니케이션 서비스",
    "Industrials": "산업재",
    "Energy": "에너지",
    "Utilities": "유틸리티",
    "Real Estate": "부동산",
    "Materials": "소재",
}

WEEKDAY_KO = "월화수목금토일"

MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        start=1,
    )
}


@dataclass
class Item:
    """캘린더에 들어갈 일정 하나."""

    uid: str
    kind: str          # "earnings"(확정 실적), "estimate"(실적 예상일), "fomc"
    us_date: str       # 미국 현지 날짜(YYYY-MM-DD)
    title: str
    description: str
    url: str
    start_utc: str | None = None  # 시각이 정확히 정해진 일정(FOMC)만 UTC 시각(ISO), 나머지는 종일 일정
    minutes: int = 30
    symbol: str = ""   # 실적 일정의 티커 (예상일 계산에 사용)
    timing: str = ""   # "pre"(장 전), "after"(장 후), ""(미정)
    sector: str = ""   # 한국어 섹터 (웹앱 필터에 사용)
    kst_date: str = "" # 한국시간 기준 날짜(종일 일정을 놓을 날). 비면 us_date
    note: str = ""        # 목록에 보여 줄 짧은 요약 (FOMC: 현재 금리·위원 전망·결과)
    actual_eps: str = ""  # 발표된 실제 EPS (예: "$5.20")
    surprise: str = ""    # 예상 대비 차이 % (예: "3.8", "-1.2")


# ---------------------------------------------------------------- 공통 도구

def http_get(url: str, headers: dict | None = None, tries: int = 3, timeout: int = 30) -> requests.Response:
    last_err: Exception | None = None
    for attempt in range(tries):
        try:
            resp = requests.get(url, headers=headers or {"User-Agent": USER_AGENT}, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as err:
            last_err = err
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url} 가져오기 실패: {last_err}")


def warn(msg: str) -> None:
    # GitHub Actions 화면에 노란 경고로 표시된다.
    print(f"::warning::{msg}")


def norm_symbol(symbol: str) -> str:
    """BRK.B / BRK/B / BRK-B 를 같은 종목으로 보기 위한 정규화."""
    return re.sub(r"[^A-Z0-9]", "", symbol.upper())


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def kst_phrase(dt_utc: datetime) -> str:
    """예: '10월 15일(목) 오전 5:05'"""
    k = dt_utc.astimezone(KST)
    ampm = "오전" if k.hour < 12 else "오후"
    hour12 = k.hour % 12 or 12
    return f"{k.month}월 {k.day}일({WEEKDAY_KO[k.weekday()]}) {ampm} {hour12}:{k.minute:02d}"


def et_to_utc(day: date, hm: tuple[int, int]) -> datetime:
    return datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=ET).astimezone(timezone.utc)


def kst_clock(dt_utc: datetime) -> str:
    """예: '저녁 7:00', '새벽 5:00', '밤 10:30'"""
    k = dt_utc.astimezone(KST)
    part = ("새벽" if k.hour < 6 else "오전" if k.hour < 12 else "오후" if k.hour < 18
            else "저녁" if k.hour < 21 else "밤")
    return f"{part} {k.hour % 12 or 12}:{k.minute:02d}"


def kst_window(us_day: date, window: tuple[tuple[int, int], tuple[int, int]]) -> tuple[date, str]:
    """미 동부 시간대 범위를 한국시간으로. 반환: (한국 날짜, '10월 13일(화) 저녁 7:00~9:30')"""
    a, b = et_to_utc(us_day, window[0]), et_to_utc(us_day, window[1])
    ka = a.astimezone(KST)
    end = kst_clock(b).split(" ")[1]
    return ka.date(), f"{ka.month}월 {ka.day}일({WEEKDAY_KO[ka.weekday()]}) {kst_clock(a)}~{end}"


def timing_info(us_day: date, timing: str) -> tuple[str, str, list[str]]:
    """발표 구분별 (한국 날짜, 제목 꼬리표, 한국시간 안내 줄들)."""
    if timing == "pre":
        kday, window = kst_window(us_day, PRE_MARKET_ET)
        opens = kst_clock(et_to_utc(us_day, MARKET_OPEN_ET))
        return kday.isoformat(), "장 전·저녁", [
            f"한국시간: {window} 사이 (미국 장 시작 전 발표)",
            f"→ 이 실적이 주가에 처음 반영되는 미국 정규장 개장: 한국시간 {opens}",
        ]
    if timing == "after":
        kday, window = kst_window(us_day, AFTER_HOURS_ET)
        return kday.isoformat(), "장 후·새벽", [f"한국시간: {window} 사이 (미국 장 마감 후 발표)"]
    return us_day.isoformat(), "시각 미정", ["한국시간: 발표 시각 미정 (회사가 아직 장 전/장 후를 알리지 않음)"]


TIME_NOTE = ("※ 나스닥은 '장 전/장 후'만 알려 줘요. 위 시간은 대부분의 회사가 발표하는 시간대이고, "
             "정확한 시각은 회사 IR 공지를 확인하세요.")


# ---------------------------------------------------------------- 1. S&P 500 / S&P 100 목록

def parse_index_table(html: str) -> list[dict]:
    """위키백과의 지수 구성종목 표(티커·회사명·섹터)를 읽는다. S&P 100, S&P 500 공통."""
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.select("table.wikitable"):
        headers = [th.get_text(" ", strip=True).lower() for th in table.select("tr th")]
        if not any("symbol" in h for h in headers) or not any("sector" in h for h in headers):
            continue
        first_row = table.find("tr")
        cols = [c.get_text(" ", strip=True).lower() for c in first_row.find_all(["th", "td"])]
        i_sym = next(i for i, h in enumerate(cols) if "symbol" in h)
        i_name = next((i for i, h in enumerate(cols) if any(k in h for k in ("name", "company", "security"))),
                      i_sym + 1)
        i_sec = next(i for i, h in enumerate(cols) if "sector" in h)
        out = []
        for tr in table.find_all("tr")[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) <= max(i_sym, i_name, i_sec):
                continue
            symbol = cells[i_sym].strip()
            if not re.fullmatch(r"[A-Z][A-Z0-9.\-/]{0,6}", symbol):
                continue
            out.append({"symbol": symbol, "name": cells[i_name], "sector": cells[i_sec]})
        if len(out) >= 50:
            return out
    raise ValueError("위키백과 페이지에서 구성종목 표를 찾지 못했습니다.")


parse_sp100 = parse_index_table  # 예전 이름


def fetch_index(url: str, cache_name: str, label: str) -> list[dict]:
    cache = DATA_DIR / cache_name
    try:
        members = parse_index_table(http_get(url).text)
        save_json(cache, members)
        print(f"{label} 구성종목 {len(members)}개를 위키백과에서 가져왔습니다.")
        return members
    except Exception as err:  # noqa: BLE001 - 어떤 실패든 캐시로 대체
        members = load_json(cache, [])
        warn(f"{label} 위키백과 실패 → 저장된 목록 {len(members)}개 사용 ({err})")
        return members


def combine_members(sp500: list[dict], sp100: list[dict]) -> list[dict]:
    """S&P 500 전체에 S&P 100 여부(top100)를 표시한다. 웹앱은 S&P 100만 기본으로 켠다."""
    top = {norm_symbol(m["symbol"]) for m in sp100}
    out = {norm_symbol(m["symbol"]): {**m, "top100": norm_symbol(m["symbol"]) in top} for m in sp500}
    for m in sp100:  # S&P 500 목록을 못 받았거나 빠진 종목이 있어도 S&P 100은 지킨다
        out.setdefault(norm_symbol(m["symbol"]), {**m, "top100": True})
    return list(out.values())


def get_members() -> list[dict]:
    sp500 = fetch_index(WIKI_SP500_URL, "sp500.json", "S&P 500")
    sp100 = fetch_index(WIKI_URL, "sp100.json", "S&P 100")
    return combine_members(sp500, sp100)


# ---------------------------------------------------------------- 2. 실적 발표

def earnings_item(row: dict, member: dict, us_day: date, themes: dict) -> Item:
    symbol = member["symbol"]
    name = member["name"]
    sector_en = member.get("sector", "")
    sector = SECTOR_KO.get(sector_en, sector_en)
    theme = themes.get(symbol) or themes.get(symbol.replace(".", "-"))

    timing_code = timing_code_of(row)
    kst_day, title_tag, when_lines = timing_info(us_day, timing_code)

    actual, surprise = actual_of(row)
    lines = [f"■ {name} ({symbol}) 실적 발표"]
    if actual:
        lines.append(result_line(actual, (row.get("epsForecast") or "").strip(), surprise))
    lines += [
        f"섹터: {sector}" + (f" / 테마: {theme}" if theme else ""),
        *when_lines,
        f"미국 현지 날짜: {us_day.isoformat()}",
    ]
    if row.get("fiscalQuarterEnding"):
        lines.append(f"대상 분기: {row['fiscalQuarterEnding']} 마감 분기")
    eps = (row.get("epsForecast") or "").strip()
    if eps:
        n = (row.get("noOfEsts") or "").strip()
        lines.append(f"시장 예상 EPS(주당순이익): {eps}" + (f" (애널리스트 {n}명)" if n else ""))
    if (row.get("lastYearEPS") or "").strip():
        lines.append(f"작년 같은 분기 EPS: {row['lastYearEPS'].strip()}")
    lines += [
        "",
        TIME_NOTE,
        "※ 공개 정보를 자동으로 모은 것이며 투자 권유가 아닙니다.",
        "",
        "공식 공시(8-K, 실적 보도자료 포함 · 영어):",
        SEC_FILINGS_PAGE.format(symbol=symbol.replace(".", "-")),
        "English source (Nasdaq):",
    ]
    url = NASDAQ_EARNINGS_PAGE.format(symbol=symbol.lower())
    lines.append(url)
    title = (f"✅ {symbol} 실적 결과 · {verdict(surprise)} · {sector}" if actual
             else f"📊 {symbol} 실적 ({title_tag}) · {sector}")

    return Item(
        uid=f"earnings-{norm_symbol(symbol)}-{us_day.isoformat()}@investment-calendar",
        kind="earnings",
        us_date=us_day.isoformat(),
        title=title,
        description="\n".join(lines),
        url=url,
        symbol=symbol,
        timing=timing_code,
        sector=sector,
        kst_date=kst_day,
        actual_eps=actual,
        surprise=surprise,
    )


def money(text) -> str:
    """'$1.23', '($0.12)', 1.23 같은 값을 '$1.23' / '-$0.12' 꼴로. 값이 없으면 ''."""
    if text is None:
        return ""
    t = str(text).strip()
    if not t or t.upper() in {"N/A", "NA", "--", "-"}:
        return ""
    neg = t.startswith("(") and t.endswith(")") or t.startswith("-")
    num = re.sub(r"[^0-9.]", "", t)
    if not num:
        return ""
    try:
        v = float(num)
    except ValueError:
        return ""
    return f"{'-' if neg and v else ''}${v:.2f}"


def percent(text) -> str:
    if text is None:
        return ""
    t = str(text).strip().replace("%", "")
    neg = t.startswith("(") and t.endswith(")")
    try:
        v = float(re.sub(r"[()+,\s]", "", t))
    except ValueError:
        return ""
    return f"{-abs(v) if neg else v:.1f}"


def actual_of(row: dict) -> tuple[str, str]:
    """나스닥 표에서 실제 EPS와 예상 대비 %를 읽는다. 지난 날짜에만 들어 있다."""
    actual = money(row.get("eps") if "eps" in row else row.get("actualEPS"))
    if not actual:
        return "", ""
    surprise = percent(row.get("surprise") if "surprise" in row else row.get("percentageSurprise"))
    if not surprise:
        est = money(row.get("epsForecast"))
        try:
            a, e = float(actual.replace("$", "")), float(est.replace("$", ""))
            surprise = f"{(a - e) / abs(e) * 100:.1f}" if e else ""
        except ValueError:
            surprise = ""
    return actual, surprise


def verdict(surprise: str) -> str:
    try:
        v = float(surprise)
    except ValueError:
        return "결과 발표"
    if abs(v) < 0.05:
        return "예상 부합"
    return f"예상 {'상회' if v > 0 else '하회'} {v:+.1f}%"


def result_line(actual: str, estimate: str, surprise: str) -> str:
    est = f" · 시장 예상 {estimate}" if estimate else ""
    return f"✅ 발표 결과: 실제 EPS {actual}{est} → {verdict(surprise)}"


def timing_code_of(row: dict) -> str:
    timing = (row.get("time") or "").lower()
    return "pre" if "pre" in timing else "after" if "after" in timing else ""


def parse_nasdaq_day(payload: dict, us_day: date, members: dict[str, dict], themes: dict) -> list[Item]:
    rows = ((payload or {}).get("data") or {}).get("rows") or []
    items: dict[str, Item] = {}
    seen_names: set[str] = set()
    for row in rows:
        member = members.get(norm_symbol(row.get("symbol", "")))
        if not member:
            continue
        # GOOGL/GOOG처럼 한 회사가 두 번 나오면 하나만 남긴다.
        company_key = re.sub(r"\W", "", (row.get("name") or member["name"]).lower())
        if company_key in seen_names:
            continue
        seen_names.add(company_key)
        item = earnings_item(row, member, us_day, themes)
        items[item.uid] = item
    return list(items.values())


def market_rows(payload: dict) -> list[list[str]]:
    """그날 실적을 발표하는 미국 상장사 전체를 간단한 표로.
    [티커, 회사명, 장전/장후, 예상 EPS, 대상 분기, 실제 EPS, 예상 대비 %]

    웹앱에서 S&P 100 밖의 종목도 '내 종목'으로 고를 수 있게, 공개된 전체 목록을 그대로 보관한다.
    누가 어떤 종목을 골랐는지는 저장소에 남지 않는다(기기 안에만 저장).
    """
    rows = ((payload or {}).get("data") or {}).get("rows") or []
    out = []
    for row in rows:
        symbol = (row.get("symbol") or "").strip()
        if not symbol:
            continue
        actual, surprise = actual_of(row)
        out.append([symbol, (row.get("name") or "").strip(), timing_code_of(row),
                    (row.get("epsForecast") or "").strip(), (row.get("fiscalQuarterEnding") or "").strip(),
                    actual, surprise])
    return out


def get_earnings(members_list: list[dict], themes: dict, today: date) -> tuple[list[Item], set[str], dict]:
    """반환값: (S&P 500 실적 일정, 성공적으로 조회한 날짜들, 날짜별 전체 상장사 표)"""
    members = {norm_symbol(m["symbol"]): m for m in members_list}
    items: list[Item] = []
    market: dict[str, list] = {}
    fetched: set[str] = set()
    failures = 0
    for offset in range(-DAYS_BACK, DAYS_AHEAD + 1):
        day = today + timedelta(days=offset)
        if day.weekday() >= 5:  # 주말은 건너뜀
            continue
        try:
            resp = http_get(NASDAQ_API.format(day=day.isoformat()), headers=BROWSER_HEADERS, tries=2)
            payload = resp.json()
            if offset in (-3, 3):  # 나스닥이 지난 날짜·앞 날짜에 어떤 항목을 주는지 기록 (점검용)
                rows = ((payload or {}).get("data") or {}).get("rows") or []
                print(f"  {day} 나스닥 항목: {sorted(rows[0].keys()) if rows else '없음'}")
            items += parse_nasdaq_day(payload, day, members, themes)
            market[day.isoformat()] = market_rows(payload)
            fetched.add(day.isoformat())
        except Exception as err:  # noqa: BLE001
            failures += 1
            print(f"  {day} 나스닥 조회 실패: {err}")
        time.sleep(0.7)  # 서버에 부담을 주지 않도록 천천히
    if failures:
        warn(f"나스닥 실적 조회 실패 {failures}일 → 그 날짜는 이전 데이터 유지")
    print(f"실적 발표 일정 {len(items)}건 (조회 성공 {len(fetched)}일)")
    return items, fetched, market


def merge_market(previous: dict, market: dict, today: date) -> dict:
    """새로 조회한 날짜는 새 표로 바꾸고, 조회 못 한 날짜는 이전 표를 유지한다.

    파일 크기를 줄이려고 회사명은 names에 한 번만 두고,
    날짜별 표는 [티커, 장전/장후, 예상 EPS, 대상 분기, 실제 EPS, 예상 대비 %]만 담는다.
    """
    keep_from = (today - timedelta(days=KEEP_PAST_DAYS)).isoformat()
    names = dict(previous.get("names") or {})
    days = {d: rows for d, rows in (previous.get("days") or {}).items() if d >= keep_from}
    for d, rows in market.items():
        days[d] = [[sym, timing, eps, fq, actual, surprise] for sym, name, timing, eps, fq, actual, surprise in rows]
        for sym, name, *_ in rows:
            if name:
                names[sym] = name
    used = {row[0] for rows in days.values() for row in rows}
    return {"names": {k: names[k] for k in sorted(used) if k in names}, "days": dict(sorted(days.items()))}


def parse_surprise(payload: dict, us_day: date) -> tuple[str, str]:
    """나스닥 회사별 '실적 서프라이즈' 표에서 그 날짜에 발표한 실제 EPS를 찾는다."""
    rows = ((((payload or {}).get("data") or {}).get("earningsSurpriseTable") or {}).get("rows")) or []
    for row in rows:
        reported = (row.get("dateReported") or "").strip()
        try:
            day = datetime.strptime(reported, "%m/%d/%Y").date()
        except ValueError:
            continue
        if abs((day - us_day).days) <= 3:
            actual = money(row.get("eps"))
            if actual:
                return actual, percent(row.get("percentageSurprise"))
    return "", ""


def fill_results(earnings: list[Item], today: date) -> list[Item]:
    """발표일이 지났는데 달력 표에 결과가 없으면, 회사별 결과 표에서 한 번 더 찾아본다."""
    since = (today - timedelta(days=RESULT_LOOKBACK_DAYS)).isoformat()
    todo = [i for i in earnings if not i.actual_eps and since <= i.us_date < today.isoformat()]
    found = failures = 0
    for item in todo[:RESULT_LOOKUP_LIMIT]:
        if failures >= 5:  # 연속으로 막히면 이번 실행에서는 그만 (내일 다시 시도)
            print("  결과 조회가 계속 실패해 이번에는 중단합니다.")
            break
        try:
            payload = http_get(NASDAQ_SURPRISE_API.format(symbol=item.symbol.replace(".", "-")),
                               headers=BROWSER_HEADERS, tries=1, timeout=10).json()
            actual, surprise = parse_surprise(payload, date.fromisoformat(item.us_date))
            failures = 0
        except Exception as err:  # noqa: BLE001
            failures += 1
            print(f"  {item.symbol} 결과 조회 실패: {err}")
            continue
        finally:
            time.sleep(0.5)
        if actual:
            found += 1
            item.actual_eps, item.surprise = actual, surprise
            lines = item.description.split("\n")
            lines.insert(1, result_line(actual, "", surprise))
            item.description = "\n".join(lines)
            item.title = f"✅ {item.symbol} 실적 결과 · {verdict(surprise)} · {item.sector}"
    if todo:
        print(f"발표 결과 추가 조회: {min(len(todo), RESULT_LOOKUP_LIMIT)}건 중 {found}건 찾음")
    return earnings


# ---------------------------------------------------------------- 2-1. 실적 예상일

def estimate_items(earnings: list[Item], members_list: list[dict], themes: dict, today: date) -> list[Item]:
    """아직 날짜를 확정하지 않은 다음 분기 실적을 '예상일'로 만든다.

    회사들은 대개 13주 간격, 같은 요일에 실적을 발표한다. 그래서 가장 최근에 알려진
    발표일(지난 발표 또는 확정된 다음 발표)에 13주씩 더해 약 6개월 앞까지 채운다.
    회사가 날짜를 확정하면 그 날짜가 기준이 되어 예상일은 자연스럽게 사라진다.
    """
    members = {norm_symbol(m["symbol"]): m for m in members_list}
    latest: dict[str, Item] = {}
    for item in earnings:
        if item.kind != "earnings" or not item.symbol:
            continue
        key = norm_symbol(item.symbol)
        if key in members and (key not in latest or item.us_date > latest[key].us_date):
            latest[key] = item

    horizon = today + timedelta(days=ESTIMATE_DAYS)
    out: list[Item] = []
    for key, anchor in latest.items():
        member = members[key]
        symbol = member["symbol"]
        sector = SECTOR_KO.get(member.get("sector", ""), member.get("sector", ""))
        theme = themes.get(symbol) or themes.get(symbol.replace(".", "-"))
        anchor_day = date.fromisoformat(anchor.us_date)
        timing_note = {
            "pre": "지난번엔 미국 장 시작 전 발표 (한국시간 같은 날 저녁)",
            "after": "지난번엔 미국 장 마감 후 발표 (한국시간 다음 날 새벽)",
        }.get(anchor.timing, "지난번 발표 시각은 알려지지 않음")
        day = anchor_day + timedelta(days=QUARTER_DAYS)
        while day <= horizon:
            if day > today:
                k = day.weekday()
                url = NASDAQ_EARNINGS_PAGE.format(symbol=symbol.lower())
                lines = [
                    f"■ {member['name']} ({symbol}) 실적 발표 예상일 — 아직 확정 아님",
                    f"섹터: {sector}" + (f" / 테마: {theme}" if theme else ""),
                    f"예상 날짜(미국): {day.isoformat()}({WEEKDAY_KO[k]}) 전후",
                    timing_note,
                    "",
                    f"계산 근거: 직전 발표일 {anchor_day.isoformat()}에서 13주씩 더한 날짜예요.",
                    "실제 날짜는 1~2주 앞뒤로 달라질 수 있어요. 특히 연말 결산 분기(1~2월 발표)는 늦어지기 쉬워요.",
                    "회사가 날짜를 확정하면 이 예상일은 자동으로 확정 일정으로 바뀝니다.",
                    "",
                    "※ 공개 정보를 자동으로 모은 것이며 투자 권유가 아닙니다.",
                    "",
                    "English source (Nasdaq):",
                    url,
                ]
                out.append(Item(
                    uid=f"estimate-{key}-{day.isoformat()}@investment-calendar",
                    kind="estimate",
                    us_date=day.isoformat(),
                    title=f"🗓️ {symbol} 실적 예상일 · {sector}",
                    description="\n".join(lines),
                    url=url,
                    symbol=symbol,
                    timing=anchor.timing,
                    sector=sector,
                    kst_date=timing_info(day, anchor.timing)[0],
                ))
            day += timedelta(days=QUARTER_DAYS)
    print(f"실적 예상일 {len(out)}건 (확정 전, {ESTIMATE_DAYS}일 앞까지)")
    return out


# ---------------------------------------------------------------- 3. FOMC

def parse_fomc(html: str) -> list[tuple[date, bool]]:
    """반환값: [(회의 마지막 날, 경제전망(점도표) 발표 여부), ...]"""
    soup = BeautifulSoup(html, "html.parser")
    meetings: list[tuple[date, bool]] = []
    for panel in soup.select("div.panel"):
        heading = panel.select_one(".panel-heading")
        m = re.search(r"(20\d\d)\s+FOMC Meetings", heading.get_text(" ", strip=True) if heading else "")
        if not m:
            continue
        year = int(m.group(1))
        for row in panel.select(".fomc-meeting"):
            month_el = row.select_one(".fomc-meeting__month")
            date_el = row.select_one(".fomc-meeting__date")
            if not month_el or not date_el:
                continue
            month_txt = month_el.get_text(" ", strip=True).lower()
            date_txt = date_el.get_text(" ", strip=True)
            if re.search(r"unscheduled|notation|cancel", date_txt + month_txt, re.I):
                continue
            dm = re.match(r"(\d{1,2})\s*[-–]\s*(\d{1,2})(\*?)", date_txt)
            if not dm:
                continue
            month_names = [part.strip()[:3] for part in month_txt.split("/")]
            if not month_names or month_names[-1] not in MONTHS:
                continue
            last_month = MONTHS[month_names[-1]]
            try:
                meetings.append((date(year, last_month, int(dm.group(2))), bool(dm.group(3))))
            except ValueError:
                continue
    if not meetings:
        raise ValueError("연준 페이지에서 FOMC 일정을 찾지 못했습니다.")
    return sorted(set(meetings))


# ---------------------------------------------------------------- 3-1. 기준금리 · 연준 위원 전망(점도표)

def parse_fred_csv(text: str) -> list[tuple[str, float]]:
    """FRED CSV(날짜,값)를 [(날짜, 값)]으로. 빈 값('.')은 건너뛴다."""
    out = []
    for line in text.strip().splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            out.append((parts[0].strip(), float(parts[1])))
        except ValueError:
            continue
    return out


def get_policy_rates() -> list[tuple[str, float, float]]:
    """[(날짜, 목표 하단, 목표 상단)]. 실패하면 []."""
    try:
        lower = dict(parse_fred_csv(http_get(FRED_CSV.format(series="DFEDTARL"), tries=2).text))
        upper = dict(parse_fred_csv(http_get(FRED_CSV.format(series="DFEDTARU"), tries=2).text))
        rates = [(d, lower[d], upper[d]) for d in sorted(set(lower) & set(upper))]
        if rates:
            print(f"기준금리 목표 범위: {rates[-1][1]:.2f}~{rates[-1][2]:.2f}% ({rates[-1][0]} 기준)")
        return rates
    except Exception as err:  # noqa: BLE001
        warn(f"기준금리(FRED) 조회 실패 → 금리 정보 없이 진행 ({err})")
        return []


def fmt_range(lo: float, hi: float) -> str:
    return f"{lo:.2f}~{hi:.2f}%"


def rate_on(rates: list, day: str, before: bool):
    """day 직전(before) 또는 직후의 목표 범위."""
    if before:
        cands = [r for r in rates if r[0] < day]
        return cands[-1] if cands else None
    cands = [r for r in rates if r[0] > day]
    return cands[0] if cands else None


def meeting_result(rates: list, day: str) -> str:
    """지난 회의의 결정(동결/인하/인상)을 금리 기록으로 판단."""
    a, b = rate_on(rates, day, True), rate_on(rates, day, False)
    if not a or not b:
        return ""
    diff = round(b[2] - a[2], 2)
    if abs(diff) < 0.01:
        return f"동결 ({fmt_range(b[1], b[2])})"
    return f"{abs(diff):.2f}%p {'인하' if diff < 0 else '인상'} ({fmt_range(a[1], a[2])} → {fmt_range(b[1], b[2])})"


def find_sep_links(html: str) -> list[tuple[str, str]]:
    """연준 일정 페이지에서 경제전망(점도표) 표 주소들. [(YYYYMMDD, 전체 주소)] 최신순."""
    found = {m.group(1): FED_BASE + m.group(0) for m in
             re.finditer(r"/monetarypolicy/fomcprojtabl(\d{8})\.htm", html)}
    return sorted(found.items(), reverse=True)


def parse_dotplot(html: str) -> dict:
    """점도표 표(Figure 2)를 읽는다. 반환: {"years": ["2026", ...], "rows": [(금리, [연도별 인원])]}"""
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        text = table.get_text(" ", strip=True).lower()
        if "midpoint" not in text:
            continue
        trs = table.find_all("tr")
        header = next((tr for tr in trs if re.search(r"20\d\d", tr.get_text(" ", strip=True))), None)
        if header is None:
            continue
        years = [c.get_text(" ", strip=True) for c in header.find_all(["th", "td"])]
        years = [y for y in years if re.fullmatch(r"20\d\d|Longer run", y)]
        rows = []
        for tr in trs:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if not cells:
                continue
            try:
                rate = float(cells[0])
            except ValueError:
                continue
            counts = []
            for c in cells[1:1 + len(years)]:
                counts.append(int(c) if c.isdigit() else 0)
            counts += [0] * (len(years) - len(counts))
            rows.append((rate, counts))
        if years and rows:
            return {"years": years, "rows": rows}
    raise ValueError("점도표 표를 찾지 못했습니다.")


def dot_summary(dots: dict, year: str, midpoint: float | None) -> dict | None:
    """한 해 말 금리 전망: 인원, 중간값, 지금보다 낮게/같게/높게."""
    if year not in dots["years"]:
        return None
    i = dots["years"].index(year)
    points = sorted(v for rate, counts in dots["rows"] for v in [rate] * counts[i])
    if not points:
        return None
    n = len(points)
    median = points[n // 2] if n % 2 else (points[n // 2 - 1] + points[n // 2]) / 2
    out = {"year": year, "n": n, "median": median}
    if midpoint is not None:
        out["lower"] = sum(p < midpoint - 0.01 for p in points)
        out["same"] = sum(abs(p - midpoint) <= 0.01 for p in points)
        out["higher"] = sum(p > midpoint + 0.01 for p in points)
    return out


def get_outlook(fomc_html: str, rates: list, today: date) -> dict:
    """현재 금리 + 최신 점도표 요약. 일부가 실패해도 가능한 것만 담는다."""
    outlook: dict = {}
    if rates:
        d, lo, hi = rates[-1]
        outlook.update(rate_date=d, lower=lo, upper=hi, midpoint=(lo + hi) / 2)
    for sep_day, url in find_sep_links(fomc_html)[:1]:
        try:
            dots = parse_dotplot(http_get(url).text)
            mid = outlook.get("midpoint")
            years = [y for y in dots["years"] if y.isdigit() and int(y) >= today.year]
            outlook["sep_date"] = f"{sep_day[:4]}-{sep_day[4:6]}-{sep_day[6:]}"
            outlook["sep_url"] = url
            outlook["years"] = [s for s in (dot_summary(dots, y, mid) for y in years[:2]) if s]
            if outlook["years"]:
                print(f"점도표({outlook['sep_date']}) 읽음: " +
                      ", ".join(f"{s['year']}년 말 중간값 {s['median']:.3f}%" for s in outlook["years"]))
        except Exception as err:  # noqa: BLE001
            warn(f"점도표 읽기 실패 → 링크만 표시 ({err})")
    return outlook


def outlook_lines(outlook: dict) -> list[str]:
    lines = []
    if "lower" in outlook:
        lines.append(f"현재 기준금리(목표 범위): {fmt_range(outlook['lower'], outlook['upper'])} "
                     f"({outlook['rate_date']} 기준)")
    years = outlook.get("years") or []
    if years:
        y, m, d = outlook["sep_date"].split("-")
        lines.append(f"📍 연준 위원들의 금리 전망 ({int(y)}년 {int(m)}월 점도표, {years[0]['n']}명)")
        for s in years:
            label = "올해" if s is years[0] else "내년"
            line = f" · {s['year']}년 말({label}) 적정 금리 중간값 {s['median']:.3f}%"
            if "lower" in s:
                line += f" — 지금보다 낮게 {s['lower']}명 · 지금 수준 {s['same']}명 · 높게 {s['higher']}명"
            lines.append(line)
        lines.append("※ 점도표는 위원 각자가 생각하는 '연말 적정 금리'예요. 이번 회의 한 번의 결정을 예측한 것은 아니에요.")
    lines.append(f"시장이 보는 이번 회의 확률(CME FedWatch, 영어): {FEDWATCH_URL}")
    return lines


def outlook_note(outlook: dict) -> str:
    parts = []
    if "lower" in outlook:
        parts.append(f"현재 {fmt_range(outlook['lower'], outlook['upper'])}")
    years = outlook.get("years") or []
    if years and "lower" in years[0]:
        s = years[0]
        parts.append(f"위원 {s['year']}년 말 전망: 인하 {s['lower']} · 동결 {s['same']} · 인상 {s['higher']}명")
    return " · ".join(parts)


def fomc_item(last_day: date, with_sep: bool, outlook: dict | None = None,
              rates: list | None = None, today: date | None = None) -> Item:
    start = et_to_utc(last_day, FOMC_STATEMENT_ET)
    presser = start + timedelta(minutes=30)
    outlook, rates = outlook or {}, rates or []
    past = today is not None and last_day < today
    result = meeting_result(rates, last_day.isoformat()) if past else ""
    lines = ["■ FOMC(미국 연방공개시장위원회) 기준금리 결정"]
    if result:
        lines.append(f"✅ 결정: {result}")
    lines += [
        "섹터/테마: 거시경제 · 미국 기준금리 · 달러/채권 금리",
        f"한국시간: {kst_phrase(start)} 성명서 발표 (미 동부 14:00)",
        f"기자회견: {kst_phrase(presser)}부터 (미 동부 14:30)",
        f"미국 현지 날짜: {last_day.isoformat()} (이틀 회의의 둘째 날)",
    ]
    if with_sep:
        lines.append("이번 회의에서는 경제전망(SEP)과 점도표도 함께 공개됩니다.")
    if not past and outlook:
        lines += ["", *outlook_lines(outlook)]
    lines += [
        "",
        "※ 공개 정보를 자동으로 모은 것이며 투자 권유가 아닙니다.",
        "",
        "English source (Federal Reserve):",
        FOMC_URL,
    ]
    if not past and outlook.get("sep_url"):
        lines += ["점도표 원문(Federal Reserve):", outlook["sep_url"]]
    note = f"결정: {result}" if result else ("" if past else outlook_note(outlook))
    return Item(
        uid=f"fomc-{last_day.isoformat()}@investment-calendar",
        kind="fomc",
        us_date=last_day.isoformat(),
        title="🏦 FOMC 금리 결정" + (" (+점도표)" if with_sep else "") + (f" · {result.split(' (')[0]}" if result else ""),
        description="\n".join(lines),
        url=FOMC_URL,
        note=note,
        start_utc=start.isoformat(),
        minutes=60,
        kst_date=start.astimezone(KST).date().isoformat(),
    )


def get_fomc(today: date) -> list[Item] | None:
    """실패하면 None (→ 이전 데이터 유지)."""
    try:
        html = http_get(FOMC_URL).text
        meetings = parse_fomc(html)
        print(f"FOMC 회의 {len(meetings)}건을 연준 홈페이지에서 가져왔습니다.")
        rates = get_policy_rates()
        outlook = get_outlook(html, rates, today)
        return [fomc_item(d, sep, outlook, rates, today) for d, sep in meetings]
    except Exception as err:  # noqa: BLE001
        warn(f"연준 FOMC 페이지 실패 → 이전 데이터 유지 ({err})")
        return None


# ---------------------------------------------------------------- 4. 합치기 & .ics 만들기

def merge(previous: list[Item], earnings: list[Item], fetched_days: set[str],
          fomc: list[Item] | None, today: date) -> list[Item]:
    keep_from = (today - timedelta(days=KEEP_PAST_DAYS)).isoformat()
    merged: dict[str, Item] = {}
    for item in previous:
        if item.kind == "estimate":
            continue  # 예상일은 매번 새로 계산
        if item.us_date < keep_from:
            continue  # 너무 오래된 일정은 정리
        if item.kind == "earnings" and item.us_date in fetched_days:
            continue  # 새로 조회한 날짜는 새 데이터로 교체 (일정 변경·취소 반영)
        if item.kind == "fomc" and fomc is not None and item.us_date >= today.isoformat():
            continue  # 앞으로의 FOMC 일정은 새 데이터로 교체
        merged[item.uid] = item
    for item in earnings + (fomc or []):
        if item.us_date >= keep_from:
            merged[item.uid] = item
    return sorted(merged.values(), key=sort_key)


def sort_key(item: Item):
    return (item.kst_date or item.us_date, item.start_utc or "", item.uid)


def build_ics(items: list[Item], now: datetime) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//investment-calendar//KO")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "투자 일정 (S&P 100 실적 · FOMC)")  # 구독 파일은 크기 때문에 S&P 100만
    cal.add("x-wr-caldesc", "S&P 100 기업 실적 발표와 FOMC 일정. 매일 자동 갱신. 공개 정보 기반, 투자 권유 아님.")
    cal.add("x-wr-timezone", "Asia/Seoul")
    cal.add("refresh-interval", vDuration(timedelta(hours=12)), parameters={"VALUE": "DURATION"})
    cal.add("x-published-ttl", "PT12H")
    for item in items:
        ev = Event()
        ev.add("uid", item.uid)
        ev.add("summary", item.title)
        ev.add("description", item.description)
        ev.add("url", item.url)
        ev.add("dtstamp", now)
        if item.start_utc:
            start = datetime.fromisoformat(item.start_utc)
            ev.add("dtstart", start)
            ev.add("dtend", start + timedelta(minutes=item.minutes))
        else:
            d = date.fromisoformat(item.kst_date or item.us_date)
            ev.add("dtstart", d)
            ev.add("dtend", d + timedelta(days=1))
        ev.add("transp", "TRANSPARENT")  # '바쁨'으로 표시하지 않음
        cal.add_component(ev)
    return cal.to_ical()


def companies(members: list[dict], themes: dict, names_ko: dict | None = None) -> list[dict]:
    """웹앱의 종목 켜고 끄기 목록. 일정이 없는 종목도 미리 고를 수 있게 전부 넣는다."""
    names_ko = names_ko or {}
    out = []
    for m in members:
        sector = SECTOR_KO.get(m.get("sector", ""), m.get("sector", ""))
        out.append({"symbol": m["symbol"], "name": m["name"], "sector": sector,
                    "theme": themes.get(m["symbol"], ""), "ko": names_ko.get(m["symbol"], ""),
                    "top100": bool(m.get("top100", True))})
    return sorted(out, key=lambda c: (c["sector"], c["symbol"]))


# ---------------------------------------------------------------- 5. 환율

def parse_fx(payload: dict) -> list[list]:
    rates = (payload or {}).get("rates") or {}
    out = []
    for day, value in sorted(rates.items()):
        krw = value.get("KRW") if isinstance(value, dict) else None
        if isinstance(krw, (int, float)):
            out.append([day, round(float(krw), 2)])
    return out


def get_fx(today: date, previous: dict) -> dict:
    """달러/원 환율(ECB 기준환율) 최근 약 2개월. 실패하면 이전 값을 그대로 둔다."""
    start = (today - timedelta(days=62)).isoformat()
    for url in FX_URLS:
        try:
            rates = parse_fx(http_get(url.format(start=start, end=today.isoformat()), tries=2).json())
            if rates:
                print(f"달러/원 환율 {len(rates)}일치 (최근 {rates[-1][0]}: {rates[-1][1]})")
                return {"pair": "USD/KRW", "source": "유럽중앙은행(ECB) 기준환율", "url": FX_PAGE, "rates": rates}
        except Exception as err:  # noqa: BLE001
            print(f"  환율 조회 실패 ({url.split('/')[2]}): {err}")
    warn("환율 조회 실패 → 이전 값 유지")
    return previous


def main() -> int:
    parser = argparse.ArgumentParser(description="투자 일정 .ics 생성")
    parser.add_argument("--today", help="기준 날짜(YYYY-MM-DD). 테스트용")
    args = parser.parse_args()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    today = date.fromisoformat(args.today) if args.today else now.astimezone(ET).date()

    events_path = DOCS_DIR / "events.json"
    previous = [Item(**d) for d in load_json(events_path, [])]
    themes = {k: v for k, v in load_json(DATA_DIR / "themes.json", {}).items() if not k.startswith("_")}

    members = get_members()
    if members:
        earnings, fetched, market = get_earnings(members, themes, today)
        earnings = fill_results(earnings, today)
    else:
        warn("S&P 500 목록이 없어 실적 일정을 건너뜁니다.")
        earnings, fetched, market = [], set(), {}
    fomc = get_fomc(today)
    market_all = merge_market(load_json(DOCS_DIR / "market.json", {}), market, today)
    fx = get_fx(today, load_json(DOCS_DIR / "fx.json", {}))

    items = merge(previous, earnings, fetched, fomc, today)
    if members:
        items = sorted(items + estimate_items(items, members, themes, today), key=sort_key)
    if not items:
        print("::error::일정이 하나도 없습니다. 데이터 출처 접속을 확인하세요.")
        return 1

    DOCS_DIR.mkdir(exist_ok=True)
    top100 = {norm_symbol(m["symbol"]) for m in members if m.get("top100")}
    ics_items = [i for i in items if i.kind == "fomc" or not top100 or norm_symbol(i.symbol) in top100]
    (DOCS_DIR / "calendar.ics").write_bytes(build_ics(ics_items, now))
    save_json(events_path, [asdict(i) for i in items])
    if members:
        names_ko = {k: v for k, v in load_json(DATA_DIR / "names_ko.json", {}).items() if not k.startswith("_")}
        save_json(DOCS_DIR / "companies.json", companies(members, themes, names_ko))
    # 전체 상장사 표는 크기를 줄이려고 들여쓰기 없이 저장한다.
    (DOCS_DIR / "market.json").write_text(
        json.dumps(market_all, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    if fx:
        save_json(DOCS_DIR / "fx.json", fx)
    n_e = sum(i.kind == "earnings" for i in items)
    n_f = sum(i.kind == "fomc" for i in items)
    n_x = sum(i.kind == "estimate" for i in items)
    n_r = sum(bool(i.actual_eps) for i in items)
    print(f"완료: 확정 실적 {n_e}건(결과 {n_r}건), 실적 예상일 {n_x}건, FOMC {n_f}건 "
          f"→ 웹앱 전체, 구독 파일 {len(ics_items)}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
