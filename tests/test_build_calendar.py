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
        # 미 동부 2026-10-14 07:00 EDT = 한국 같은 날 20:00
        self.assertEqual(jpm.start_utc, "2026-10-14T11:00:00+00:00")
        self.assertIn("10월 14일(수) 오후 8:00", jpm.description)
        self.assertIn("섹터: 금융 / 테마: 미국 최대 은행", jpm.description)
        self.assertIn("https://www.nasdaq.com/market-activity/stocks/jpm/earnings", jpm.description)

        goog = next(i for i in items if "GOOGL" in i.uid)
        # 미 동부 16:05 EDT = 한국 다음 날 05:05
        self.assertIn("10월 15일(목) 오전 5:05", goog.description)

        brk = next(i for i in items if "BRKB" in i.uid)
        self.assertIsNone(brk.start_utc)  # 시각 미정 → 종일 일정

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
