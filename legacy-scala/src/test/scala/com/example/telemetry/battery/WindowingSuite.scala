package com.example.telemetry.battery

class WindowingSuite extends munit.FunSuite {
  test("watermark closes a UTC day exactly at its day end") {
    val dayEnd = EventValidator.parseTimestamp("2026-01-16T00:00:00.000Z").get
    assertEquals(Windowing.watermark(None), None)
    assertEquals(Windowing.watermark(Some(dayEnd + Windowing.AllowedLatenessMs)), Some(dayEnd))
    val open = DayAccumulator(dayEnd, 3.5, 3.8, 0.1, 25.0, dayEnd - 1, 1, 95.0, 1)
    val key = BatteryDailyKey("VEH-0001", "PK-0001-A", "2026-01-15")
    assertEquals(Windowing.dueTimers(Seq(key -> open), dayEnd - 1), Seq.empty)
    assertEquals(Windowing.dueTimers(Seq(key -> open), dayEnd), Seq(key -> open))
  }
}
