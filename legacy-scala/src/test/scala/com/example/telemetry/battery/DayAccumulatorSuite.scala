package com.example.telemetry.battery

class DayAccumulatorSuite extends munit.FunSuite {
  private val vehicleId = "VEH-0109"
  private val packId = "PK-0109-A"
  private val day = "2026-01-15"

  private def event(id: String, minute: Int, sequence: Int, min: Double, max: Double, soh: Double): BatteryEvent =
    BatteryEvent(
      id,
      vehicleId,
      packId,
      1768464000000L + minute * 60000L,
      sequence,
      min,
      max,
      25.0,
      soh
    )

  test("imbalance trap tracks the largest spread from one event") {
    val readings = Seq(
      event("a", 0, 1, 3.60, 3.70, 92.0),
      event("b", 30, 2, 3.50, 3.55, 92.0),
      event("c", 60, 3, 3.68, 3.80, 91.9),
      event("d", 90, 4, 3.66, 3.74, 91.9)
    )
    val aggregate = readings.tail.foldLeft(DayAccumulator.start(readings.head))(_.add(_))

    assertEquals(aggregate.minCellVoltageV, 3.5)
    assertEquals(aggregate.maxCellVoltageV, 3.8)
    assertEquals(aggregate.maxCellImbalanceV, 0.11999999999999966)
    assertNotEquals(aggregate.maxCellImbalanceV, aggregate.maxCellVoltageV - aggregate.minCellVoltageV)
  }

  test("SOH selects greatest event time then greatest sequence number") {
    val readings = Seq(
      event("first", 0, 1, 3.6, 3.7, 96.0),
      event("latest", 8, 3, 3.6, 3.7, 95.8),
      event("tie-loser", 8, 2, 3.6, 3.7, 95.9)
    )
    val aggregate = readings.tail.foldLeft(DayAccumulator.start(readings.head))(_.add(_))
    assertEquals(aggregate.latestStateOfHealthPct, 95.8)
    assertEquals((aggregate.latestEventTs, aggregate.latestSequenceNumber), (readings(1).eventTs, 3L))
    assertEquals(BatteryDailyKey(vehicleId, packId, Windowing.dayOf(readings.head.eventTs)).day, day)
  }
}
