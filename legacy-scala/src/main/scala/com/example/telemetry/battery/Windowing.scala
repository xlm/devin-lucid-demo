package com.example.telemetry.battery

object Windowing {
  val AllowedLatenessMs: Long = 10L * 60 * 1000
  val DayMs: Long = 24L * 60 * 60 * 1000
  val EndOfInput: Long = Long.MaxValue

  def watermark(maxEventTs: Option[Long]): Option[Long] = maxEventTs.map(_ - AllowedLatenessMs)

  def isLate(eventTs: Long, watermark: Option[Long]): Boolean = watermark.exists(eventTs < _)

  def dayOf(eventTs: Long): String =
    EventValidator.formatTimestamp(Math.floorDiv(eventTs, DayMs) * DayMs).take(10)

  val byCodePoint: Ordering[String] =
    (a: String, b: String) => java.util.Arrays.compare(a.codePoints.toArray, b.codePoints.toArray)

  private val firingOrder: Ordering[(Long, String, String, String)] =
    Ordering.Tuple4(Ordering.Long, byCodePoint, byCodePoint, byCodePoint)

  def dueTimers(
      open: Iterable[(BatteryDailyKey, DayAccumulator)],
      watermark: Long
  ): Seq[(BatteryDailyKey, DayAccumulator)] =
    open.iterator
      .filter { case (_, day) => day.dayEndTs <= watermark }
      .toSeq
      .sortBy { case (key, day) => (day.dayEndTs, key.vehicleId, key.packId, key.day) }(firingOrder)
}
