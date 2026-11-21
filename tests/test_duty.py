"""值班收件的场景测试：每个用例一台全新的演练服务，从 09:05 走到 10:00。"""
import unittest
from threading import Thread

from duty_desk import Desk, Source, Span, TransportError, seconds
from source_service import create_source


class Harness(unittest.TestCase):
    def setUp(self):
        self.server = create_source()
        self.thread = Thread(target=self.server.serve_forever,
                             kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def desk(self):
        desk = Desk(Source(f"http://127.0.0.1:{self.server.server_port}"))
        self.addCleanup(desk.close)
        return desk

    def walk(self, desk, *moments):
        desk.tick()
        for moment in moments:
            desk.tick(moment)
        return desk.report("STORM7")

    def window(self, report, name):
        return next(row for row in report["windows"] if row["id"] == name)

    def pair(self, report, name):
        return next(row for row in report["pairs"] if row["id"] == name)

    def sample(self, desk, ref):
        return next(row for row in desk.station_samples(ref.split("/")[0]) if row.ref == ref)


class PhysicalEvidence(Harness):
    def test_trigger_identity_is_stable_across_repeats(self):
        desk = self.desk()
        desk.tick()
        desk.tick("09:12:00")           # Q4 的 T1、T2 在摘要里重复出现
        q4 = [row for row in desk.triggers.values() if row.station == "Q4"]
        self.assertEqual(sorted(row.id for row in q4), ["T1", "T2", "T3", "T4"])
        self.assertEqual(len([row for row in desk.station_samples("Q4")
                              if row.origin == "trigger"]), 4)

    def test_jammed_valve_never_proves_a_bottle(self):
        desk = self.desk()
        desk.tick()
        # T2 的脉冲紧邻的是一条卡位记录，不能算进实物瓶。
        self.assertEqual(self.sample(desk, "Q4/M1/boot-A/T2#4").state, "pending")
        self.assertIn("卡位", self.sample(desk, "Q4/M1/boot-A/T2#4").reason)
        self.assertEqual(self.window(desk.report("STORM7"), "S-TWO")["status"], "pending")

    def test_revised_page_restores_the_bottle_without_new_sampling(self):
        desk = self.desk()
        desk.tick()
        desk.tick("09:10:00")
        self.assertEqual(self.sample(desk, "Q4/M1/boot-A/T2#4").state, "pending")
        desk.tick("09:15:00")           # 修订页 A-Fix 在 relay 班次后到
        sample = self.sample(desk, "Q4/M1/boot-A/T2#4")
        self.assertEqual(sample.bottle, ("BX10", "S2", "M1/boot-A"))
        # 同一时刻箱资料已更正为不含 S2：实物瓶成立，但已不能送检。
        self.assertEqual(sample.state, "lost")
        self.assertIn("不含 S2", sample.reason)
        self.assertIn("A-Fix", [ident for kind, ident, _ in sample.basis])
        # 修订没有多出一次取样：Q4 仍然只有四个触发。
        self.assertEqual(len([row for row in desk.station_samples("Q4")
                              if row.origin == "trigger"]), 4)

    def test_gap_states_are_distinguished(self):
        desk = self.desk()
        self.walk(desk, "09:26:00")
        history = desk.history()
        q8 = [gap for gap in history.gaps if gap.station == "Q8"]
        self.assertIn("no_records", {gap.state for gap in q8})
        self.assertIn("offline", {gap.state for gap in q8})
        offline = next(gap for gap in q8 if gap.state == "offline")
        result = history.bottle(next(row for row in desk.triggers.values()
                                     if row.station == "Q8" and row.epoch == "boot-B"),
                                offline.first)
        self.assertEqual(result.state, "pending")
        self.assertIn("离线", result.reason)
        no_records = next(gap for gap in q8 if gap.state == "no_records")
        result = history.bottle(next(row for row in desk.triggers.values()
                                     if row.station == "Q8" and row.epoch == "boot-A"),
                                no_records.first)
        self.assertEqual(result.state, "lost")

    def test_reused_slot_numbers_do_not_imply_continuity(self):
        desk = self.desk()
        self.walk(desk, "09:26:00")     # B-New（boot-B）已列出
        history = desk.history()
        boot_b = next(row for row in desk.triggers.values()
                      if row.station == "Q8" and row.epoch == "boot-B")
        result = history.bottle(boot_b, 2)
        self.assertEqual(result.state, "pending")
        self.assertIn(result.code, ("restart", "pending_page"))
        # 同号瓶位在 boot-A 里有自己的映射，不被新世代覆盖。
        boot_a = next(row for row in desk.triggers.values()
                      if row.station == "Q8" and row.epoch == "boot-A")
        self.assertEqual(history.bottle(boot_a, 2).state, "proven")

    def test_missing_segment_does_not_pretend_to_be_continuous(self):
        desk = self.desk()
        self.walk(desk, "09:26:00")
        history = desk.history()
        # Q8/boot-A 的 3-4 段柜端确认没有记录：跨过它的位置不能当连续证据。
        self.assertEqual(history._record(("M2", "boot-A"), 3)[1], "gap:no_records")


class WindowsAndPairs(Harness):
    def test_clock_interval_must_sit_inside_one_phase(self):
        desk = self.desk()
        self.walk(desk, "09:12:00")
        sample = self.sample(desk, "Q8/M2/boot-A/T1#2")
        self.assertEqual(sample.possible.as_list(), ["09:13:05", "09:13:15"])
        history = desk.history()
        phasing = history.phase_at("Q8", "STORM7", sample.possible)
        self.assertEqual(phasing.state, "proven")
        self.assertEqual(phasing.phase, "peak")

    def test_window_is_downgraded_when_blank_correction_excludes_bottle(self):
        desk = self.desk()
        desk.tick()
        desk.tick("09:10:00")
        before = self.window(desk.report("STORM7"), "S-TWO")
        self.assertEqual(before["status"], "proven")
        desk.tick("09:14:00")           # BL-4 把空白样覆盖改成 S1、S3
        after = self.window(desk.report("STORM7"), "S-TWO")
        self.assertEqual(after["status"], "open")
        self.assertIn("不含 S2", after["reason"])

    def test_lab_receipt_keeps_the_evidence_version_it_was_delivered_on(self):
        desk = self.desk()
        report = self.walk(desk, "09:10:00", "09:25:00")
        archive = next(row for row in report["archives"] if row["station"] == "Q8")
        self.assertEqual(archive["state"], "received")
        self.assertEqual(archive["receipt"], "LAB-12")
        self.assertIn("C3", [item["id"] for item in archive["versions"]])
        # Q4 的 S2 在 09:23 被退回，登记的是当时凭据，不是后来的更正版。
        rejected = next(row for row in report["archives"] if row["station"] == "Q4"
                        and row["bottle"] == "S2")
        self.assertEqual(rejected["state"], "rejected")
        self.assertTrue(rejected["digest"])
        # 退回也按退回来的那一刻存证，不改写更早的收件版本。
        self.assertEqual(rejected["known"], "09:23:00")
        self.assertIn("C1", [item["id"] for item in rejected["versions"]])

    def test_pair_needs_both_sides_and_compatible_travel(self):
        desk = self.desk()
        self.walk(desk, "09:12:00")
        pair = self.pair(desk.report("STORM7"), "PAIR-1")
        self.assertEqual(pair["status"], "proven")
        low, high = (seconds(value) for value in pair["spread"])
        self.assertGreaterEqual(low, 120)
        self.assertLessEqual(high, 360)

    def test_one_side_missing_never_clears_the_other_station(self):
        desk = self.desk()
        report = self.walk(desk, "09:20:00")
        pair = self.pair(report, "PAIR-1")
        self.assertNotEqual(pair["status"], "proven")
        self.assertEqual(pair["downstream"]["status"], "proven")
        self.assertNotEqual(pair["upstream"]["status"], "proven")
        self.assertEqual(self.window(report, "S-THREE")["status"], "proven")

    def test_weather_revision_only_rechecks_affected_station(self):
        desk = self.desk()
        report = self.walk(desk, "09:13:00")
        q4 = [row for row in report["windows"] if row["station"] == "Q4"]
        q8 = [row for row in report["windows"] if row["station"] == "Q8"]
        self.assertTrue(q4 and q8)
        self.assertTrue(all(row["reviewed_with"] == "F7b" for row in q4))
        self.assertTrue(all(row["reviewed_with"] == "F8" for row in q8))
        # rev 2 把 peak 窗提前到 09:03..09:17，T3 的 09:08:42..09:10:42 仍在 peak。
        phasing = desk.history().phase_at("Q4", "STORM7", Span(seconds("09:08:42"),
                                                               seconds("09:10:42")))
        self.assertEqual(phasing.phase, "peak")

    def test_late_and_repeated_pages_add_no_new_sample(self):
        desk = self.desk()
        desk.tick()
        desk.tick("09:12:00")
        first = {row.ref.split("#")[0] for row in desk.station_samples("Q4")}
        desk.tick("09:15:00")
        desk.tick("09:19:00")
        later = {row.ref.split("#")[0] for row in desk.station_samples("Q4")}
        self.assertEqual(first, later)
        self.assertEqual(len(first), 4)


class Scheduling(Harness):
    def test_incumbent_booking_survives_late_page_corrections(self):
        desk = self.desk()
        desk.tick()
        desk.tick("09:10:00")
        before = [row for row in desk.bookings.values() if row.trip == "RIVER-1"]
        self.assertTrue(before)
        desk.tick("09:14:00")           # 作业窗降级、车次截止
        desk.tick("09:16:00")
        after = [row for row in desk.bookings.values() if row.trip == "RIVER-1"]
        self.assertEqual(len(before), len(after))
        self.assertTrue(all(row.state in ("accepted", "in_flight") for row in after))

    def test_lost_receipt_stays_in_flight_and_is_not_rebooked(self):
        desk = self.desk()
        desk.tick()
        desk.tick("09:10:00")
        pending = [row for row in desk.bookings.values() if row.state == "in_flight"]
        self.assertTrue(pending)
        request = pending[0].request
        desk.tick("09:14:00")
        self.assertEqual(desk.bookings[request].state, "accepted")
        self.assertEqual(desk.bookings[request].note, "提交后回执丢失，保留在途占用")
        self.assertEqual(sum(row.request == request for row in desk.bookings.values()), 1)

    def test_box_slot_is_never_used_twice(self):
        desk = self.desk()
        for moment in ("09:10:00", "09:12:00", "09:20:00", "09:25:00", "09:30:00"):
            desk.tick(moment)
        seen = {}
        for booking in desk.bookings.values():
            if booking.state not in ("accepted", "in_flight"):
                continue
            key = (booking.trip, booking.bottle_set)
            self.assertNotIn(key, seen)
            seen[key] = booking.request

    def test_unfillable_window_is_not_planned_again(self):
        desk = self.desk()
        report = self.walk(desk, "09:10:00", "09:20:00")
        self.assertEqual(self.window(report, "S-TWO")["status"], "unfillable")
        self.assertNotIn("S-TWO", {row["window"] for row in desk.plan_trips()})

    def test_arrival_must_still_fall_inside_the_phase(self):
        desk = self.desk()
        self.walk(desk, "09:20:00")
        for plan in desk.plan_trips():
            window = next(row for row in desk.windows() if row.id == plan["window"])
            self.assertIsNotNone(window.span)
            self.assertTrue(window.span[0] <= seconds(plan["arrival"]) < window.span[1])

    def test_field_sample_counts_only_with_real_time_and_box(self):
        desk = self.desk()
        report = self.walk(desk, "09:25:00")
        field = [row for row in desk.station_samples("Q4") if row.origin == "field"]
        self.assertTrue(field)
        self.assertEqual(field[0].possible.as_list(), ["09:18:00", "09:18:00"])
        self.assertEqual(field[0].bottle[:2], ("BX11", "S4"))
        self.assertEqual(field[0].state, "proven")
        self.assertEqual(self.window(report, "S-THREE")["status"], "proven")
        self.assertEqual(self.window(report, "S-THREE")["sample"]["origin"], "field")

    def test_overdue_return_does_not_retroactively_fill_an_earlier_window(self):
        desk = self.desk()
        report = self.walk(desk, "09:30:00", "09:40:00")
        rise = self.window(report, "S-ONE")
        self.assertNotEqual(rise["sample"]["origin"], "field")
        self.assertEqual(rise["sample"]["possible"], ["08:43:50", "08:44:20"])

    def test_cold_deadline_invalidates_a_previously_usable_sample(self):
        desk = self.desk()
        report = self.walk(desk, "09:20:00")
        self.assertEqual(self.window(report, "S-ONE")["status"], "proven")
        desk.tick("09:39:00")           # BX10 cold_by 09:38 已过
        later = self.window(desk.report("STORM7"), "S-ONE")
        self.assertEqual(later["status"], "pending")
        self.assertIn("冷藏", later["reason"])


class ReportShape(unittest.TestCase):
    def test_span_helpers(self):
        self.assertTrue(Span(1, 4).contained((0, 5)))
        self.assertFalse(Span(1, 5).contained((0, 5)))
        self.assertTrue(Span(4, 6).meets((5, 9)))
        self.assertEqual(Span.of(["09:00:00", "09:00:00"]).as_list(), ["09:00:00", "09:00:00"])
        with self.assertRaises(ValueError):
            Span(5, 1)

    def test_unreachable_source_raises_transport_error(self):
        desk = Desk(Source("http://127.0.0.1:9", timeout=0.2))
        with self.assertRaises(TransportError):
            desk.tick()


if __name__ == "__main__":
    unittest.main()


class PlanActions(Harness):
    def test_last_bus_is_taken_only_at_last_call_or_when_radio_is_lost(self):
        desk = self.desk()
        desk.tick()
        desk.tick("09:08:00")
        plans = {row["window"]: row for row in desk.plan_trips()}
        self.assertTrue(plans)
        # 09:08 时缺的样还可能靠 R2 补领拿到，末班车还要等，先不占。
        self.assertTrue(all(row["action"] == "wait_radio" for row in plans.values()))

    def test_window_without_any_remaining_trip_is_not_planned(self):
        desk = self.desk()
        report = self.walk(desk, "09:10:00", "09:16:00")
        # S-TWO 已因空白样更正失效；09:16 之后 RIVER-1 已截止，不能再排。
        self.assertEqual(self.window(report, "S-TWO")["status"], "open")
        self.assertNotIn("S-TWO", {row["window"] for row in desk.plan_trips()})

    def test_booked_window_is_not_planned_twice(self):
        desk = self.desk()
        for moment in ("09:10:00", "09:12:00", "09:20:00", "09:25:00"):
            desk.tick(moment)
        live = {row.window for row in desk.bookings.values() if row.live}
        self.assertTrue(live)
        self.assertFalse(live & {row["window"] for row in desk.plan_trips()})

    def test_open_pair_member_window_holds_a_seat(self):
        desk = self.desk()
        desk.tick()
        desk.tick("09:08:00")
        # Q8 peak 是配对成员窗且已开：缺的一侧还没证据，也要先把末班车占住。
        booked = {row.window for row in desk.bookings.values() if row.live}
        self.assertIn("PAIR:PAIR-1:Q8:peak", booked)
        self.assertNotIn("S-THREE", booked)      # 独立窗仍可等无线补领
        self.assertEqual(desk.bookings["STORM7-PAIR:PAIR-1:Q8:peak-RIVER-1-V1"].station, "Q8")


class RadioScheduling(Harness):
    def test_page_is_kept_for_the_pass_that_can_still_decide_a_trip(self):
        desk = self.desk()
        desk.tick()
        # 09:05 tower 班只有 1 页额度：先用它领覆盖 T1-T2 的 A-In，而不是更晚的页。
        desk.tick("09:05:00")
        self.assertIsNotNone(desk.pages[("M1", "A-In")].events)
        self.assertIsNone(desk.pages[("M1", "A-Out")].events)
        self.assertEqual(desk.slot_spend[("T1", "09:05:00")], 1)

    def test_quota_is_respected_per_pass(self):
        desk = self.desk()
        for moment in ("09:05:00", "09:10:00", "09:12:00", "09:15:00", "09:20:00"):
            desk.tick(moment)
        limits = {("T1", "09:05:00"): 1, ("R1", "09:10:00"): 1,
                  ("T2", "09:12:00"): 1, ("R2", "09:15:00"): 1,
                  ("T3", "09:19:00"): 2}
        for key, pages in limits.items():
            self.assertLessEqual(desk.slot_spend.get(key, 0), pages, key)

    def test_relay_gap_is_retried_on_the_relay_window(self):
        desk = self.desk()
        desk.tick()
        desk.tick("09:10:00")
        self.assertIsNotNone(desk.pages[("M1", "A-Mid")].events)
        self.assertEqual(desk.pages[("M1", "A-Mid")].granted_slot, "R1")

    def test_evicted_page_is_marked_dead_not_retried(self):
        desk = self.desk()
        desk.tick()
        desk.tick("09:20:00")     # T1 早过，A-In 已淘汰
        page = desk.pages[("M1", "A-In")]
        if page.events is None:
            self.assertIsNotNone(page.dead)
        self.assertTrue(all(row["outcome"] != "retry" for row in desk.fetch_pages()))

    def test_page_digest_is_verified_before_use(self):
        desk = self.desk()
        desk.tick()
        page = desk.pages[("M1", "A-In")]
        self.assertEqual(len(page.digest), 64)
        for sample in desk.station_samples("Q4"):
            for item in sample.basis:
                kind, ident, dig = item
                if kind == "page" and ident == "A-In":
                    self.assertEqual(dig, page.digest)


class RevisionSemantics(Harness):
    def test_revision_withdraws_the_old_page_as_soon_as_it_is_listed(self):
        desk = self.desk()
        desk.tick()
        desk.tick("09:10:00")
        # 09:12 之后 A-Fix 已发布但还没到；A-In 里 3 号的卡位不再算证据。
        desk.tick("09:12:00")
        self.assertNotIn("A-In", [page.id for page in desk.history().usable_pages(("M1", "boot-A"))])
        sample = self.sample(desk, "Q4/M1/boot-A/T2")
        self.assertEqual(sample.state, "pending")
        self.assertIn("还没领到", sample.reason)
        # 09:15 新版到手后恢复，仍只有那一次取样。
        desk.tick("09:15:00")
        self.assertEqual(self.sample(desk, "Q4/M1/boot-A/T2#4").bottle,
                         ("BX10", "S2", "M1/boot-A"))

    def test_revision_keeps_the_same_trigger_identity(self):
        desk = self.desk()
        desk.tick()
        before = {row.ref.split("#")[0] for row in desk.station_samples("Q4")}
        desk.tick("09:15:00")
        after = {row.ref.split("#")[0] for row in desk.station_samples("Q4")}
        self.assertEqual(before, after)

    def test_weather_revision_can_make_a_span_straddle(self):
        desk = self.desk()
        desk.tick()
        history = desk.history()
        # rev 1 下 T2 的可能区间整体落在 peak。
        old = history.phase_at("Q4", "STORM7", Span(seconds("09:02:37"), seconds("09:03:07")))
        self.assertEqual((old.state, old.phase), ("proven", "peak"))
        desk.tick("09:13:00")     # F7b 把 peak 改到 09:03 起
        new = desk.history().phase_at("Q4", "STORM7",
                                      Span(seconds("09:02:37"), seconds("09:03:07")))
        self.assertEqual(new.state, "straddle")
        self.assertEqual(set(new.adjacent), {"rise", "peak"})
        windows = {row["id"]: row for row in desk.report("STORM7")["windows"]}
        self.assertEqual(windows["S-TWO"]["reviewed_with"], "F7b")
        # T2 已不能独占 peak 窗；此刻 S-TWO 靠另一条整段落在 peak 内的 T3 撑着，
        # 但 T2 自己不再被当成 peak 的确定证据。
        t2 = self.sample(desk, "Q4/M1/boot-A/T2")
        self.assertEqual(t2.phases, {"STORM7": "straddle"})
        t3 = self.sample(desk, "Q4/M1/boot-A/T3#6")
        self.assertEqual(t3.phases, {"STORM7": "peak"})


class LateCorrections(Harness):
    def test_duplicate_ticks_never_duplicate_a_booking(self):
        desk = self.desk()
        desk.tick()
        for moment in ("09:05:00", "09:08:00", "09:10:00", "09:12:00",
                       "09:14:00", "09:16:00", "09:20:00"):
            desk.tick(moment)
            desk.tick(moment)          # 同一时刻重收一遍
        keys = [(row.trip, row.bottle_set) for row in desk.bookings.values()
                if row.state in ("accepted", "in_flight")]
        self.assertEqual(len(keys), len(set(keys)))

    def test_page_correction_never_cancels_an_accepted_field_task(self):
        desk = self.desk()
        desk.tick()
        desk.tick("09:10:00")
        accepted = {row.request for row in desk.bookings.values() if row.state == "accepted"}
        desk.tick("09:14:00")       # 空白样更正+S-TWO 降级
        desk.tick("09:15:00")       # 修订页到
        for request in accepted:
            self.assertIn(request, desk.bookings)
            self.assertEqual(desk.bookings[request].state, "accepted")

    def test_field_receipt_arriving_after_departure_keeps_its_travel_record(self):
        desk = self.desk()
        desk.tick()
        desk.tick("09:10:00")
        booking = next(row for row in desk.bookings.values() if row.trip == "RIVER-1")
        travel = dict(booking.travel())
        desk.tick("09:14:00")       # 回执确认
        desk.tick("09:25:00")       # 现场回条另到
        self.assertEqual(booking.travel(), travel)
        self.assertEqual(booking.state, "accepted")
        # 现场实际记录单独登记，不覆写派车时的 travel。
        fields = [row for row in desk.lab.values() if row.state == "field_sample"]
        self.assertTrue(fields)
        self.assertEqual(fields[0].at, "09:18:00")
