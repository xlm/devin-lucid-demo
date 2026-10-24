package com.example.telemetry.charging

/** Session key: one charging session of one vehicle. */
final case class SessionKey(vehicleId: String, sessionId: String)

/** A `charging.events.v1` reading that passed validation. Times are epoch ms. */
final case class ChargingEvent(
    eventId: String,
    vehicleId: String,
    sessionId: String,
    eventTs: Long,
    sequenceNumber: Long,
    chargingState: String,
    batterySoc: Double,
    chargingPowerKw: Double,
    energyDeliveredKwh: Double
) {
  def key: SessionKey = SessionKey(vehicleId, sessionId)
  def isComplete: Boolean = chargingState == ChargingEvent.Complete
}

object ChargingEvent {
  val Complete = "COMPLETE"
  val States: Set[String] = Set("IDLE", "CHARGING", Complete, "FAULT")
}
