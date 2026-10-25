package com.example.telemetry.battery

final case class DayAccumulator(
    dayEndTs: Long,
    minCellVoltageV: Double,
    maxCellVoltageV: Double,
    maxCellImbalanceV: Double,
    maxPackTempC: Double,
    latestEventTs: Long,
    latestSequenceNumber: Long,
    latestStateOfHealthPct: Double,
    eventCount: Long
) {
  def add(event: BatteryEvent): DayAccumulator = {
    val imbalance = event.maxCellVoltageV - event.minCellVoltageV
    val isLatest =
      event.eventTs > latestEventTs ||
        (event.eventTs == latestEventTs && event.sequenceNumber > latestSequenceNumber)
    copy(
      minCellVoltageV = math.min(minCellVoltageV, event.minCellVoltageV),
      maxCellVoltageV = math.max(maxCellVoltageV, event.maxCellVoltageV),
      maxCellImbalanceV = if (imbalance > maxCellImbalanceV) imbalance else maxCellImbalanceV,
      maxPackTempC = math.max(maxPackTempC, event.packTempC),
      latestEventTs = if (isLatest) event.eventTs else latestEventTs,
      latestSequenceNumber = if (isLatest) event.sequenceNumber else latestSequenceNumber,
      latestStateOfHealthPct = if (isLatest) event.stateOfHealthPct else latestStateOfHealthPct,
      eventCount = eventCount + 1
    )
  }
}

object DayAccumulator {
  def start(event: BatteryEvent): DayAccumulator = {
    val dayEndTs = (Math.floorDiv(event.eventTs, Windowing.DayMs) + 1) * Windowing.DayMs
    DayAccumulator(
      dayEndTs,
      event.minCellVoltageV,
      event.maxCellVoltageV,
      event.maxCellVoltageV - event.minCellVoltageV,
      event.packTempC,
      event.eventTs,
      event.sequenceNumber,
      event.stateOfHealthPct,
      1L
    )
  }
}

final case class DailyState(seenEventIds: Set[String], open: Option[DayAccumulator], closed: Boolean) {
  def hasSeen(eventId: String): Boolean = seenEventIds.contains(eventId)

  def markSeen(eventId: String): DailyState = copy(seenEventIds = seenEventIds + eventId)

  def accept(event: BatteryEvent): DailyState = {
    require(!closed, "cannot accept an event into a closed day")
    copy(open = Some(open.fold(DayAccumulator.start(event))(_.add(event))))
  }

  def close: DailyState = copy(open = None, closed = true)
}

object DailyState {
  val Empty: DailyState = DailyState(Set.empty, None, closed = false)
}
