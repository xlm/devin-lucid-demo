package com.example.telemetry.charging

/** Running aggregate of the accepted events of one open session. */
final case class SessionAccumulator(
    startTs: Long,
    lastTs: Long,
    maxSequenceNumber: Long,
    maxEnergyKwh: Double,
    peakPowerKw: Double,
    eventCount: Long,
    complete: Boolean
) {

  /** Energy is cumulative per reading, so the session total is the MAX reading, never a sum. */
  def add(e: ChargingEvent): SessionAccumulator =
    SessionAccumulator(
      startTs = math.min(startTs, e.eventTs),
      lastTs = math.max(lastTs, e.eventTs),
      maxSequenceNumber = math.max(maxSequenceNumber, e.sequenceNumber),
      maxEnergyKwh = SessionAccumulator.keepFirstMax(maxEnergyKwh, e.energyDeliveredKwh),
      peakPowerKw = SessionAccumulator.keepFirstMax(peakPowerKw, e.chargingPowerKw),
      eventCount = eventCount + 1,
      complete = complete || e.isComplete
    )
}

object SessionAccumulator {
  def start(e: ChargingEvent): SessionAccumulator =
    SessionAccumulator(e.eventTs, e.eventTs, e.sequenceNumber, e.energyDeliveredKwh, e.chargingPowerKw, 1, e.isComplete)

  // Keeps the stored reading on ties (e.g. 0.0 vs -0.0) so a value is always passed through unchanged.
  private def keepFirstMax(current: Double, candidate: Double): Double =
    if (candidate > current) candidate else current
}

/**
 * Per-key state: the event_id dedupe set, the open session (if any) and the closed flag.
 *
 * A closed key keeps its dedupe set and closed flag for the lifetime of the run. That is a
 * demo simplification: a real deployment needs a state TTL or this state grows without bound.
 */
final case class SessionState(seenEventIds: Set[String], open: Option[SessionAccumulator], closed: Boolean) {
  def hasSeen(eventId: String): Boolean = seenEventIds.contains(eventId)

  def markSeen(eventId: String): SessionState = copy(seenEventIds = seenEventIds + eventId)

  def accept(e: ChargingEvent): SessionState = {
    require(!closed, "cannot accept an event into a closed session")
    copy(open = Some(open.fold(SessionAccumulator.start(e))(_.add(e))))
  }

  def close: SessionState = copy(open = None, closed = true)
}

object SessionState {
  val Empty: SessionState = SessionState(Set.empty, None, closed = false)
}
