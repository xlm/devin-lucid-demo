package com.example.telemetry.charging

/** A record on one of the two output topics. */
sealed trait OutputRecord {
  def toJson: ujson.Obj
}

/** `charging.session.summary.v1`, Kafka-keyed by vehicle_id. */
final case class SessionSummary(key: SessionKey, session: SessionAccumulator) extends OutputRecord {
  def completionReason: String = if (session.complete) "COMPLETE" else "INACTIVITY_TIMEOUT"

  def toJson: ujson.Obj = ujson.Obj(
    "session_id" -> key.sessionId,
    "vehicle_id" -> key.vehicleId,
    "start_ts" -> EventValidator.formatTimestamp(session.startTs),
    "end_ts" -> EventValidator.formatTimestamp(session.lastTs),
    "total_energy_kwh" -> session.maxEnergyKwh,
    "peak_power_kw" -> session.peakPowerKw,
    "event_count" -> session.eventCount.toDouble,
    "completion_reason" -> completionReason,
    "schema_version" -> OutputRecords.SchemaVersion
  )
}

/** `charging.errors.v1`. Never carries the raw payload, measurements or synthetic_location. */
final case class ProcessingError(errorType: String, field: Option[String], inputLine: Long, ids: Identifiers)
    extends OutputRecord {
  def toJson: ujson.Obj = {
    def optional(v: Option[String]): ujson.Value = v.fold[ujson.Value](ujson.Null)(ujson.Str(_))
    ujson.Obj(
      "error_type" -> errorType,
      "field" -> optional(field),
      "input_line" -> inputLine.toDouble,
      "event_id" -> optional(ids.eventId),
      "vehicle_id" -> optional(ids.vehicleId),
      "session_id" -> optional(ids.sessionId),
      "schema_version" -> OutputRecords.SchemaVersion
    )
  }
}

object ProcessingError {
  val LateEventDropped = "LATE_EVENT_DROPPED"
  val SessionAlreadyClosed = "SESSION_ALREADY_CLOSED"

  def forEvent(errorType: String, inputLine: Long, e: ChargingEvent): ProcessingError =
    ProcessingError(errorType, None, inputLine, Identifiers(Some(e.eventId), Some(e.vehicleId), Some(e.sessionId)))
}

object OutputRecords {
  val SchemaVersion = "1.0"
}
