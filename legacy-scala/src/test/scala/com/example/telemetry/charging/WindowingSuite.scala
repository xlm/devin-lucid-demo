package com.example.telemetry.charging

class WindowingSuite extends munit.FunSuite {
  private def acc(lastTs: Long, complete: Boolean) = SessionAccumulator(0L, lastTs, 1L, 1.0, 1.0, 1L, complete)

  test("no watermark before the first event, so nothing is late") {
    assertEquals(Windowing.watermark(None), None)
    assert(!Windowing.isLate(0L, None))
  }

  test("an event exactly at the watermark is on time, 1 ms before it is late") {
    val wm = Windowing.watermark(Some(1000000L))
    assertEquals(wm, Some(400000L))
    assert(!Windowing.isLate(400000L, wm))
    assert(Windowing.isLate(399999L, wm))
  }

  test("COMPLETE expires at last_ts, otherwise after the inactivity timeout, due at equality") {
    assertEquals(Windowing.expiry(acc(5000L, complete = true)), 5000L)
    assertEquals(Windowing.expiry(acc(5000L, complete = false)), 5000L + 1800000L)
    assert(Windowing.isDue(acc(5000L, complete = true), 5000L))
    assert(!Windowing.isDue(acc(5000L, complete = true), 4999L))
  }

  test("timers fire in (expiry, vehicle_id, session_id) order") {
    val open = Seq(
      SessionKey("VEH-0002", "S-A") -> acc(10L, complete = true),
      SessionKey("VEH-0001", "S-B") -> acc(10L, complete = true),
      SessionKey("VEH-0001", "S-A") -> acc(10L, complete = true),
      SessionKey("VEH-0000", "S-Z") -> acc(5L, complete = true),
      SessionKey("VEH-0000", "S-Y") -> acc(11L, complete = true)
    )
    val due = Windowing.dueTimers(open, 10L).map(_._1)
    assertEquals(due, Seq(SessionKey("VEH-0000", "S-Z"), SessionKey("VEH-0001", "S-A"),
      SessionKey("VEH-0001", "S-B"), SessionKey("VEH-0002", "S-A")))
  }

  test("session ids compare by code point, not UTF-16 unit") {
    // U+1F600 is above U+FF5E as a code point, but its first UTF-16 unit (0xD83D) is below.
    assert(Windowing.byCodePoint.lt("\uff5e", "\ud83d\ude00"))
    assert("\uff5e".compareTo("\ud83d\ude00") > 0)
  }
}
