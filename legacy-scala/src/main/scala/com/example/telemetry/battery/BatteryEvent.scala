package com.example.telemetry.battery

final case class BatteryDailyKey(vehicleId: String, packId: String, day: String)

final case class BatteryEvent(
    eventId: String,
    vehicleId: String,
    packId: String,
    eventTs: Long,
    sequenceNumber: Long,
    minCellVoltageV: Double,
    maxCellVoltageV: Double,
    packTempC: Double,
    stateOfHealthPct: Double
) {
  def key: BatteryDailyKey = BatteryDailyKey(vehicleId, packId, Windowing.dayOf(eventTs))
}

final case class BatteryIdentifiers(eventId: Option[String], vehicleId: Option[String], packId: Option[String])

object BatteryIdentifiers {
  val Absent: BatteryIdentifiers = BatteryIdentifiers(None, None, None)
}

final case class Rejection(errorType: String, field: Option[String], ids: BatteryIdentifiers)
