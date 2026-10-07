"""투자 일정 캘린더(.ics) 생성기.

하는 일:
  1. 위키백과에서 S&P 100 구성종목 목록(티커·회사명·섹터)을 가져온다.
  2. 나스닥 실적 캘린더 API에서 지난 100일~앞으로 90일의 실적 발표 일정을 가져와
     S&P 100 종목만 고른다. 아직 확정되지 않은 다음 분기는 13주 간격으로
     '예상일'을 계산해 약 6개월 앞까지 채운다.
  3. 연준(Fed) 홈페이지에서 FOMC 회의 일정을 가져온다.
  4. 한국어 설명(테마/섹터, 한국시간 발표 시점)과 영어 원문 링크를 붙여
     docs/calendar.ics 로 저장한다.

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
NASDAQ_API = "https://api.nasdaq.com/api/calendar/earnings?date={day}"
NASDAQ_EARNINGS_PAGE = "https://www.nasdaq.com/market-activity/stocks/{symbol}/earnings"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")

DAYS_AHEAD = 90        # 오늘부터 며칠 뒤까지 나스닥에서 확정 실적 일정을 조회할지
DAYS_BACK = 100        # 직전 분기 발표일을 알기 위해 며칠 전까지 거꾸로 조회할지
ESTIMATE_DAYS = 183    # 확정 전 '예상일'을 오늘부터 며칠 뒤(약 6개월)까지 보여 줄지
QUARTER_DAYS = 91      # 분기 간격 13주. 7의 배수라 요일이 그대로 유지된다
KEEP_PAST_DAYS = 120   # 지난 일정을 며칠까지 캘린더에 남겨 둘지

# 실적 발표 시각은 나스닥이 "장 전/장 후"로만 알려 준다. 아래는 흔한 시각을 쓴 추정값이다.
PRE_MARKET_ET = (7, 0)     # 장 시작 전: 미 동부 07:00경
AFTER_HOURS_ET = (16, 5)   # 장 마감 후: 미 동부 16:05경
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
    start_utc: str | None = None  # 시각이 정해진 일정이면 UTC 시각(ISO), 아니면 종일 일정
    minutes: int = 30
    symbol: str = ""   # 실적 일정의 티커 (예상일 계산에 사용)
    timing: str = ""   # "pre"(장 전), "after"(장 후), ""(미정)


# ---------------------------------------------------------------- 공통 도구

def http_get(url: str, headers: dict | None = None, tries: int = 3) -> requests.Response:
    last_err: Exception | None = None
    for attempt in range(tries):
        try:
            resp = requests.get(url, headers=headers or {"User-Agent": USER_AGENT}, timeout=30)
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


# ---------------------------------------------------------------- 1. S&P 100 목록

def parse_sp100(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.select("table.wikitable"):
        headers = [th.get_text(" ", strip=True).lower() for th in table.select("tr th")]
        if not any("symbol" in h for h in headers) or not any("sector" in h for h in headers):
            continue
        first_row = table.find("tr")
        cols = [c.get_text(" ", strip=True).lower() for c in first_row.find_all(["th", "td"])]
        i_sym = next(i for i, h in enumerate(cols) if "symbol" in h)
        i_name = next((i for i, h in enumerate(cols) if "name" in h or "company" in h), i_sym + 1)
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
    raise ValueError("위키백과 페이지에서 S&P 100 표를 찾지 못했습니다.")


def get_sp100() -> list[dict]:
    cache = DATA_DIR / "sp100.json"
    try:
        members = parse_sp100(http_get(WIKI_URL).text)
        save_json(cache, members)
        print(f"S&P 100 구성종목 {len(members)}개를 위키백과에서 가져왔습니다.")
        return members
    except Exception as err:  # noqa: BLE001 - 어떤 실패든 캐시로 대체
        members = load_json(cache, [])
        warn(f"위키백과 실패 → 저장된 목록 {len(members)}개 사용 ({err})")
        return members


# ---------------------------------------------------------------- 2. 실적 발표

def earnings_item(row: dict, member: dict, us_day: date, themes: dict) -> Item:
    symbol = member["symbol"]
    name = member["name"]
    sector_en = member.get("sector", "")
    sector = SECTOR_KO.get(sector_en, sector_en)
    theme = themes.get(symbol) or themes.get(symbol.replace(".", "-"))

    timing = (row.get("time") or "").lower()
    if "pre" in timing:
        start = et_to_utc(us_day, PRE_MARKET_ET)
        when_label = "장 시작 전"
        when = f"{kst_phrase(start)}경 (미 동부 07:00경 추정, 미국 {when_label})"
        title_tag = "장전"
        timing_code = "pre"
    elif "after" in timing:
        start = et_to_utc(us_day, AFTER_HOURS_ET)
        when_label = "장 마감 후"
        when = f"{kst_phrase(start)}경 (미 동부 16:05경 추정, 미국 {when_label})"
        title_tag = "장후"
        timing_code = "after"
    else:
        start = None
        when = "발표 시각 미정 (회사가 아직 시각을 알리지 않음)"
        title_tag = "시각 미정"
        timing_code = ""

    lines = [
        f"■ {name} ({symbol}) 실적 발표",
        f"섹터: {sector}" + (f" / 테마: {theme}" if theme else ""),
        f"한국시간: {when}",
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
        "※ 발표 시각은 나스닥의 '장 전/장 후' 구분을 바탕으로 한 추정입니다. 정확한 시각은 회사 IR 공지를 확인하세요.",
        "※ 공개 정보를 자동으로 모은 것이며 투자 권유가 아닙니다.",
        "",
        "English source (Nasdaq):",
    ]
    url = NASDAQ_EARNINGS_PAGE.format(symbol=symbol.lower())
    lines.append(url)

    return Item(
        uid=f"earnings-{norm_symbol(symbol)}-{us_day.isoformat()}@investment-calendar",
        kind="earnings",
        us_date=us_day.isoformat(),
        title=f"📊 {symbol} 실적 ({title_tag}) · {sector}",
        description="\n".join(lines),
        url=url,
        start_utc=start.isoformat() if start else None,
        minutes=30,
        symbol=symbol,
        timing=timing_code,
    )


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


def get_earnings(members_list: list[dict], themes: dict, today: date) -> tuple[list[Item], set[str]]:
    """반환값: (실적 일정 목록, 성공적으로 조회한 날짜들)"""
    members = {norm_symbol(m["symbol"]): m for m in members_list}
    items: list[Item] = []
    fetched: set[str] = set()
    failures = 0
    for offset in range(-DAYS_BACK, DAYS_AHEAD + 1):
        day = today + timedelta(days=offset)
        if day.weekday() >= 5:  # 주말은 건너뜀
            continue
        try:
            resp = http_get(NASDAQ_API.format(day=day.isoformat()), headers=BROWSER_HEADERS, tries=2)
            items += parse_nasdaq_day(resp.json(), day, members, themes)
            fetched.add(day.isoformat())
        except Exception as err:  # noqa: BLE001
            failures += 1
            print(f"  {day} 나스닥 조회 실패: {err}")
        time.sleep(0.7)  # 서버에 부담을 주지 않도록 천천히
    if failures:
        warn(f"나스닥 실적 조회 실패 {failures}일 → 그 날짜는 이전 데이터 유지")
    print(f"실적 발표 일정 {len(items)}건 (조회 성공 {len(fetched)}일)")
    return items, fetched


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
            "pre": "지난번엔 미국 장 시작 전 발표 (한국시간 같은 날 밤)",
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


def fomc_item(last_day: date, with_sep: bool) -> Item:
    start = et_to_utc(last_day, FOMC_STATEMENT_ET)
    presser = start + timedelta(minutes=30)
    lines = [
        "■ FOMC(미국 연방공개시장위원회) 기준금리 결정",
        "섹터/테마: 거시경제 · 미국 기준금리 · 달러/채권 금리",
        f"한국시간: {kst_phrase(start)} 성명서 발표 (미 동부 14:00)",
        f"기자회견: {kst_phrase(presser)}부터 (미 동부 14:30)",
        f"미국 현지 날짜: {last_day.isoformat()} (이틀 회의의 둘째 날)",
    ]
    if with_sep:
        lines.append("이번 회의에서는 경제전망(SEP)과 점도표도 함께 공개됩니다.")
    lines += [
        "",
        "※ 공개 정보를 자동으로 모은 것이며 투자 권유가 아닙니다.",
        "",
        "English source (Federal Reserve):",
        FOMC_URL,
    ]
    return Item(
        uid=f"fomc-{last_day.isoformat()}@investment-calendar",
        kind="fomc",
        us_date=last_day.isoformat(),
        title="🏦 FOMC 금리 결정" + (" (+점도표)" if with_sep else ""),
        description="\n".join(lines),
        url=FOMC_URL,
        start_utc=start.isoformat(),
        minutes=60,
    )


def get_fomc() -> list[Item] | None:
    """실패하면 None (→ 이전 데이터 유지)."""
    try:
        meetings = parse_fomc(http_get(FOMC_URL).text)
        print(f"FOMC 회의 {len(meetings)}건을 연준 홈페이지에서 가져왔습니다.")
        return [fomc_item(d, sep) for d, sep in meetings]
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
    return sorted(merged.values(), key=lambda i: (i.us_date, i.start_utc or "", i.uid))


def build_ics(items: list[Item], now: datetime) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//investment-calendar//KO")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "투자 일정 (S&P 100 실적 · FOMC)")
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
            d = date.fromisoformat(item.us_date)
            ev.add("dtstart", d)
            ev.add("dtend", d + timedelta(days=1))
        ev.add("transp", "TRANSPARENT")  # '바쁨'으로 표시하지 않음
        cal.add_component(ev)
    return cal.to_ical()


def main() -> int:
    parser = argparse.ArgumentParser(description="투자 일정 .ics 생성")
    parser.add_argument("--today", help="기준 날짜(YYYY-MM-DD). 테스트용")
    args = parser.parse_args()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    today = date.fromisoformat(args.today) if args.today else now.astimezone(ET).date()

    events_path = DOCS_DIR / "events.json"
    previous = [Item(**d) for d in load_json(events_path, [])]
    themes = {k: v for k, v in load_json(DATA_DIR / "themes.json", {}).items() if not k.startswith("_")}

    members = get_sp100()
    if members:
        earnings, fetched = get_earnings(members, themes, today)
    else:
        warn("S&P 100 목록이 없어 실적 일정을 건너뜁니다.")
        earnings, fetched = [], set()
    fomc = get_fomc()

    items = merge(previous, earnings, fetched, fomc, today)
    if members:
        items = sorted(items + estimate_items(items, members, themes, today),
                       key=lambda i: (i.us_date, i.start_utc or "", i.uid))
    if not items:
        print("::error::일정이 하나도 없습니다. 데이터 출처 접속을 확인하세요.")
        return 1

    DOCS_DIR.mkdir(exist_ok=True)
    (DOCS_DIR / "calendar.ics").write_bytes(build_ics(items, now))
    save_json(events_path, [asdict(i) for i in items])
    n_e = sum(i.kind == "earnings" for i in items)
    n_f = sum(i.kind == "fomc" for i in items)
    n_x = sum(i.kind == "estimate" for i in items)
    print(f"완료: 확정 실적 {n_e}건, 실적 예상일 {n_x}건, FOMC {n_f}건 → docs/calendar.ics")
    return 0


if __name__ == "__main__":
    sys.exit(main())
