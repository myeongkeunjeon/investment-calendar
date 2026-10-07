"""샘플 데이터로 캘린더 생성기를 검사한다. 실행: python -m unittest discover tests"""

import json
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from icalendar import Calendar

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import build_calendar as bc  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
THEMES = {"JPM": "미국 최대 은행, 투자은행"}


class TestSources(unittest.TestCase):
    def setUp(self):
        self.members = bc.parse_sp100((FIX / "wiki_sp100.html").read_text())
        self.by_sym = {bc.norm_symbol(m["symbol"]): m for m in self.members}

    def test_sp100_table(self):
        syms = [m["symbol"] for m in self.members]
        self.assertIn("AAPL", syms)
        self.assertIn("BRK.B", syms)
        self.assertEqual(self.by_sym["AAPL"]["sector"], "Information Technology")

    def test_earnings_filter_and_times(self):
        payload = json.loads((FIX / "nasdaq_2026-10-14.json").read_text())
        items = bc.parse_nasdaq_day(payload, date(2026, 10, 14), self.by_sym, THEMES)
        syms = sorted(i.uid.split("-")[1] for i in items)
        # S&P 100 아닌 SMLL 제외, GOOG/GOOGL 중복 제거, BRK/B 인식
        self.assertEqual(syms, ["BRKB", "GOOGL", "JPM"])

        jpm = next(i for i in items if "JPM" in i.uid)
        # 정확한 시각을 지어내지 않고 시간대로 안내: 미 동부 06:00~08:30 EDT = 한국 저녁 7:00~9:30
        self.assertIsNone(jpm.start_utc)
        self.assertEqual(jpm.kst_date, "2026-10-14")
        self.assertIn("10월 14일(수) 저녁 7:00~9:30 사이", jpm.description)
        self.assertIn("미국 정규장 개장: 한국시간 밤 10:30", jpm.description)
        self.assertIn("장 전·저녁", jpm.title)
        self.assertIn("섹터: 금융 / 테마: 미국 최대 은행", jpm.description)
        self.assertIn("https://www.nasdaq.com/market-activity/stocks/jpm/earnings", jpm.description)

        goog = next(i for i in items if "GOOGL" in i.uid)
        # 장 마감 후(16:00~17:00 EDT)는 한국 다음 날 새벽 5:00~6:00
        self.assertEqual(goog.kst_date, "2026-10-15")
        self.assertIn("10월 15일(목) 새벽 5:00~6:00 사이", goog.description)

        brk = next(i for i in items if "BRKB" in i.uid)
        self.assertEqual(brk.kst_date, "2026-10-14")  # 시각 미정 → 미국 날짜 그대로
        self.assertIn("시각 미정", brk.title)

    def test_winter_time_shifts_one_hour(self):
        # 미국 서머타임이 끝나면(11월 첫 일요일 이후) 한국시간이 1시간 늦어진다
        self.assertEqual(bc.timing_info(date(2026, 11, 3), "pre")[2][0],
                         "한국시간: 11월 3일(화) 저녁 8:00~10:30 사이 (미국 장 시작 전 발표)")
        self.assertEqual(bc.timing_info(date(2026, 11, 3), "after")[0], "2026-11-04")

    def test_market_rows_keep_every_listed_company(self):
        payload = json.loads((FIX / "nasdaq_2026-10-14.json").read_text())
        rows = bc.market_rows(payload)
        self.assertEqual(len(rows), 5)  # S&P 100 밖 종목(SMLL)도 남는다
        self.assertEqual(rows[-1], ["SMLL", "Small Cap Inc.", "pre", "$0.10", "Sep/2026", "", ""])
        previous = {"names": {"KEEP": "Keep Co.", "OLD": "Old Co."},
                    "days": {"2026-01-01": [["OLD", "", "", ""]], "2026-10-13": [["KEEP", "pre", "", ""]]}}
        merged = bc.merge_market(previous, {"2026-10-14": rows}, date(2026, 10, 7))
        self.assertEqual(list(merged["days"]), ["2026-10-13", "2026-10-14"])  # 오래된 날짜는 정리
        self.assertEqual(merged["days"]["2026-10-14"][-1], ["SMLL", "pre", "$0.10", "Sep/2026", "", ""])
        self.assertEqual(merged["names"]["SMLL"], "Small Cap Inc.")
        self.assertEqual(merged["names"]["KEEP"], "Keep Co.")
        self.assertNotIn("OLD", merged["names"])  # 쓰이지 않는 이름도 정리

    def test_fomc(self):
        meetings = bc.parse_fomc((FIX / "fomc.html").read_text())
        self.assertIn((date(2026, 11, 1), False), meetings)  # Oct/Nov 31-1
        self.assertIn((date(2026, 3, 18), True), meetings)   # * = 점도표
        self.assertIn((date(2027, 1, 27), False), meetings)
        self.assertEqual(len(meetings), 5)  # notation vote 제외

        dec = bc.fomc_item(date(2026, 12, 9), True)
        # 12월은 미국 서머타임 해제: 14:00 EST = 한국 다음 날 04:00
        self.assertIn("12월 10일(목) 오전 4:00", dec.description)
        self.assertIn("점도표", dec.title)


class TestSp500AndResults(unittest.TestCase):
    def test_sp500_table_and_top100_flag(self):
        sp500 = bc.parse_index_table((FIX / "wiki_sp500.html").read_text())
        self.assertEqual(sp500[1], {"symbol": "ZTS", "name": "Zoetis", "sector": "Health Care"})
        sp100 = [{"symbol": "AAPL", "name": "Apple Inc.", "sector": "Information Technology"},
                 {"symbol": "BRK.B", "name": "Berkshire", "sector": "Financials"}]
        members = {m["symbol"]: m for m in bc.combine_members(sp500, sp100)}
        self.assertTrue(members["AAPL"]["top100"])
        self.assertFalse(members["ZTS"]["top100"])
        self.assertTrue(members["BRK.B"]["top100"])  # S&P 500 표에 없어도 S&P 100은 지킨다

    def test_past_day_results(self):
        payload = json.loads((FIX / "nasdaq_2026-10-02_past.json").read_text())
        members = {bc.norm_symbol(m["symbol"]): m for m in [
            {"symbol": "JPM", "name": "JPMorgan Chase", "sector": "Financials"},
            {"symbol": "AAPL", "name": "Apple Inc.", "sector": "Information Technology"}]}
        items = {i.symbol: i for i in bc.parse_nasdaq_day(payload, date(2026, 10, 2), members, THEMES)}
        jpm = items["JPM"]
        self.assertEqual((jpm.actual_eps, jpm.surprise), ("$5.20", "3.8"))
        self.assertIn("✅ 발표 결과: 실제 EPS $5.20 · 시장 예상 $5.01 → 예상 상회 +3.8%", jpm.description)
        self.assertTrue(jpm.title.startswith("✅ JPM 실적 결과 · 예상 상회 +3.8%"))
        self.assertIn("sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=JPM&type=8-K", jpm.description)
        # 서프라이즈 값이 없으면 예상치로 계산, 적자는 음수로
        self.assertEqual((items["AAPL"].actual_eps, items["AAPL"].surprise), ("-$0.10", "-150.0"))
        rows = bc.market_rows(payload)
        self.assertEqual(rows[0][5:], ["$5.20", "3.8"])
        self.assertEqual(rows[2][5:], ["", ""])  # 아직 결과 없음

    def test_money_and_verdict(self):
        self.assertEqual(bc.money("$1,234.5"), "$1234.50")
        self.assertEqual(bc.money(-0.3), "-$0.30")
        self.assertEqual(bc.money("N/A"), "")
        self.assertEqual(bc.verdict("-2.04"), "예상 하회 -2.0%")
        self.assertEqual(bc.verdict("0"), "예상 부합")
        self.assertEqual(bc.verdict(""), "결과 발표")

    def test_fill_results_from_surprise_table(self):
        item = bc.Item("earnings-JPM-2026-10-01@x", "earnings", "2026-10-01", "📊 JPM 실적", "■ JPM\n섹터", "u",
                       symbol="JPM", sector="금융")
        payload = {"data": {"earningsSurpriseTable": {"rows": [
            {"fiscalQtrEnd": "Sep 2026", "dateReported": "10/1/2026", "eps": 5.2,
             "consensusForecast": "5.01", "percentageSurprise": "3.79"}]}}}

        class R:
            def json(self):
                return payload
        orig_get, orig_sleep = bc.http_get, bc.time.sleep
        bc.http_get, bc.time.sleep = (lambda *a, **k: R()), (lambda s: None)
        try:
            bc.fill_results([item], date(2026, 10, 7))
        finally:
            bc.http_get, bc.time.sleep = orig_get, orig_sleep
        self.assertEqual((item.actual_eps, item.surprise), ("$5.20", "3.8"))
        self.assertEqual(item.description.split("\n")[1], "✅ 발표 결과: 실제 EPS $5.20 → 예상 상회 +3.8%")


class TestFomcOutlook(unittest.TestCase):
    def setUp(self):
        lower = dict(bc.parse_fred_csv((FIX / "fred_lower.csv").read_text()))
        upper = dict(bc.parse_fred_csv((FIX / "fred_upper.csv").read_text()))
        self.rates = [(d, lower[d], upper[d]) for d in sorted(set(lower) & set(upper))]

    def test_fred_and_meeting_result(self):
        self.assertEqual(self.rates[-1], ("2026-10-06", 3.75, 4.0))  # '.'(빈 값)은 건너뜀
        self.assertEqual(bc.meeting_result(self.rates, "2026-09-16"),
                         "0.25%p 인하 (4.00~4.25% → 3.75~4.00%)")
        self.assertEqual(bc.meeting_result(self.rates, "2026-09-18"), "동결 (3.75~4.00%)")
        self.assertEqual(bc.meeting_result(self.rates, "2026-12-09"), "")  # 아직 기록 없음

    def test_sep_link_and_dotplot(self):
        html = '<a href="/monetarypolicy/fomcprojtabl20260617.htm">x</a> <a href="/monetarypolicy/fomcprojtabl20260916.htm">y</a>'
        self.assertEqual(bc.find_sep_links(html)[0],
                         ("20260916", "https://www.federalreserve.gov/monetarypolicy/fomcprojtabl20260916.htm"))
        dots = bc.parse_dotplot((FIX / "fomc_projtabl.html").read_text())
        self.assertEqual(dots["years"], ["2026", "2027", "2028", "Longer run"])
        s = bc.dot_summary(dots, "2026", 3.875)  # 현재 3.75~4.00% → 중간 3.875
        self.assertEqual(s, {"year": "2026", "n": 19, "median": 3.875, "lower": 4, "same": 8, "higher": 7})
        self.assertEqual(bc.dot_summary(dots, "2027", 3.875)["median"], 3.625)

    def test_fomc_item_texts(self):
        outlook = {"rate_date": "2026-10-06", "lower": 3.75, "upper": 4.0, "midpoint": 3.875,
                   "sep_date": "2026-09-16", "sep_url": "https://example/sep",
                   "years": [{"year": "2026", "n": 19, "median": 3.875, "lower": 4, "same": 8, "higher": 7}]}
        future = bc.fomc_item(date(2026, 10, 28), False, outlook, self.rates, date(2026, 10, 7))
        self.assertIn("현재 기준금리(목표 범위): 3.75~4.00% (2026-10-06 기준)", future.description)
        self.assertIn("📍 연준 위원들의 금리 전망 (2026년 9월 점도표, 19명)", future.description)
        self.assertIn("지금보다 낮게 4명 · 지금 수준 8명 · 높게 7명", future.description)
        self.assertIn("cme-fedwatch-tool", future.description)
        self.assertEqual(future.note, "현재 3.75~4.00% · 위원 2026년 말 전망: 인하 4 · 동결 8 · 인상 7명")
        past = bc.fomc_item(date(2026, 9, 16), True, outlook, self.rates, date(2026, 10, 7))
        self.assertIn("✅ 결정: 0.25%p 인하", past.description)
        self.assertNotIn("점도표, 19명", past.description)  # 지난 회의엔 현재 전망을 붙이지 않음
        self.assertTrue(past.title.endswith("· 0.25%p 인하"))
        self.assertEqual(past.note, "결정: 0.25%p 인하 (4.00~4.25% → 3.75~4.00%)")


class TestEstimates(unittest.TestCase):
    def test_estimates_follow_latest_known_date(self):
        members = [{"symbol": "AAPL", "name": "Apple Inc.", "sector": "Information Technology"},
                   {"symbol": "JPM", "name": "JPMorgan Chase", "sector": "Financials"}]
        today = date(2026, 10, 7)
        known = [
            # AAPL: 지난 7월 발표만 알려짐(10월 날짜 미확정) → 10월 말부터 예상
            bc.Item("earnings-AAPL-2026-07-30@x", "earnings", "2026-07-30", "t", "d", "u",
                    symbol="AAPL", timing="after"),
            # JPM: 지난 발표 + 확정된 다음 발표 → 확정일 이후부터 예상
            bc.Item("earnings-JPM-2026-07-14@x", "earnings", "2026-07-14", "t", "d", "u",
                    symbol="JPM", timing="pre"),
            bc.Item("earnings-JPM-2026-10-13@x", "earnings", "2026-10-13", "t", "d", "u",
                    symbol="JPM", timing="pre"),
            bc.Item("fomc-2026-10-28@x", "fomc", "2026-10-28", "t", "d", "u"),
        ]
        out = bc.estimate_items(known, members, THEMES, today)
        dates = {(i.symbol, i.us_date) for i in out}
        self.assertEqual(dates, {
            ("AAPL", "2026-10-29"), ("AAPL", "2027-01-28"),
            ("JPM", "2027-01-12"),
        })  # 4월 날짜는 6개월(2027-04-08) 밖이라 빠진다
        # 13주 간격이라 요일이 유지된다 (AAPL 7/30 목요일 → 목요일)
        self.assertTrue(all(date.fromisoformat(i.us_date).weekday() == 3 for i in out if i.symbol == "AAPL"))
        jan = next(i for i in out if i.us_date == "2027-01-12")
        self.assertIn("아직 확정 아님", jan.description)
        self.assertIn("장 시작 전", jan.description)
        self.assertIn("예상일", jan.title)

    def test_old_estimates_are_dropped_on_merge(self):
        old = bc.Item("estimate-AAPL-2026-10-29@x", "estimate", "2026-10-29", "t", "d", "u")
        self.assertEqual(bc.merge([old], [], set(), None, date(2026, 10, 7)), [])


class TestFx(unittest.TestCase):
    def test_parse_fx(self):
        payload = {"base": "USD", "rates": {"2026-10-06": {"KRW": 1381.237}, "2026-10-05": {"KRW": 1379.5}}}
        self.assertEqual(bc.parse_fx(payload), [["2026-10-05", 1379.5], ["2026-10-06", 1381.24]])
        self.assertEqual(bc.parse_fx({"rates": {"2026-10-06": {"JPY": 1}}}), [])


class TestCompanies(unittest.TestCase):
    def test_company_list_for_web_app(self):
        members = [{"symbol": "JPM", "name": "JPMorgan Chase", "sector": "Financials"},
                   {"symbol": "AAPL", "name": "Apple Inc.", "sector": "Information Technology"}]
        out = bc.companies(members, THEMES, {"JPM": "JP모건 체이스"})
        self.assertEqual(out[0], {"symbol": "JPM", "name": "JPMorgan Chase", "sector": "금융",
                                  "theme": "미국 최대 은행, 투자은행", "ko": "JP모건 체이스", "top100": True})
        self.assertEqual(out[1]["theme"], "")


class TestMergeAndIcs(unittest.TestCase):
    def test_merge_replaces_refetched_days_and_keeps_others(self):
        today = date(2026, 10, 7)
        old_moved = bc.Item("earnings-AAA-2026-10-14@x", "earnings", "2026-10-14", "t", "d", "u")
        old_unfetched = bc.Item("earnings-BBB-2026-10-20@x", "earnings", "2026-10-20", "t", "d", "u")
        old_past = bc.Item("earnings-CCC-2026-09-01@x", "earnings", "2026-09-01", "t", "d", "u")
        too_old = bc.Item("earnings-DDD-2025-01-01@x", "earnings", "2025-01-01", "t", "d", "u")
        new = bc.Item("earnings-AAA-2026-10-15@x", "earnings", "2026-10-15", "t", "d", "u")
        out = bc.merge([old_moved, old_unfetched, old_past, too_old], [new],
                       {"2026-10-14", "2026-10-15"}, None, today)
        uids = [i.uid for i in out]
        self.assertEqual(uids, [old_past.uid, new.uid, old_unfetched.uid])

    def test_ics_is_valid(self):
        items = [
            bc.fomc_item(date(2026, 10, 28), False),
            bc.Item("earnings-X-2026-10-14@x", "earnings", "2026-10-14", "📊 X 실적", "줄1\n줄2, 쉼표; 세미콜론", "https://example.com"),
        ]
        raw = bc.build_ics(items, datetime(2026, 10, 7, tzinfo=timezone.utc))
        self.assertIn(b"REFRESH-INTERVAL;VALUE=DURATION:PT12H", raw)
        cal = Calendar.from_ical(raw)
        events = cal.walk("VEVENT")
        self.assertEqual(len(events), 2)
        self.assertEqual(str(events[1]["description"]), "줄1\n줄2, 쉼표; 세미콜론")
        self.assertEqual(events[1].decoded("dtstart"), date(2026, 10, 14))
        for line in raw.split(b"\r\n"):
            self.assertLessEqual(len(line), 75)  # RFC 5545 줄 길이 제한


if __name__ == "__main__":
    unittest.main()
