package com.example.telemetry.charging

/**
 * Event-time rules: watermark, lateness, session expiry and the order in which timers fire.
 * All times are epoch milliseconds of event time, never wall-clock time.
 */
object Windowing {
  val AllowedLatenessMs: Long = 10L * 60 * 1000
  val InactivityTimeoutMs: Long = 30L * 60 * 1000

  /** Watermark used for the end-of-input flush: every open session is due. */
  val EndOfInput: Long = Long.MaxValue

  /** None until the first event has advanced max_event_ts; before that nothing is late. */
  def watermark(maxEventTs: Option[Long]): Option[Long] = maxEventTs.map(_ - AllowedLatenessMs)

  /** An event exactly at the watermark is still on time. */
  def isLate(eventTs: Long, watermark: Option[Long]): Boolean = watermark.exists(eventTs < _)

  /** A COMPLETE session expires at its last event, any other after the inactivity timeout. */
  def expiry(s: SessionAccumulator): Long =
    if (s.complete) s.lastTs else s.lastTs + InactivityTimeoutMs

  def isDue(s: SessionAccumulator, watermark: Long): Boolean = expiry(s) <= watermark

  /** Plain string comparison by Unicode code point, not by UTF-16 unit. */
  val byCodePoint: Ordering[String] =
    (a: String, b: String) => java.util.Arrays.compare(a.codePoints.toArray, b.codePoints.toArray)

  private val firingOrder: Ordering[(Long, String, String)] =
    Ordering.Tuple3(Ordering.Long, byCodePoint, byCodePoint)

  /** Open sessions due at `watermark`, in ascending (expiry, vehicle_id, session_id) order. */
  def dueTimers(open: Iterable[(SessionKey, SessionAccumulator)], watermark: Long): Seq[(SessionKey, SessionAccumulator)] =
    open.iterator
      .filter { case (_, s) => isDue(s, watermark) }
      .toSeq
      .sortBy { case (k, s) => (expiry(s), k.vehicleId, k.sessionId) }(firingOrder)
}
