package com.example.telemetry.battery

sealed trait OutputRecord {
  def toJson: ujson.Obj
}

final case class BatterySummary(key: BatteryDailyKey, day: DayAccumulator) extends OutputRecord {
  def toJson: ujson.Obj = ujson.Obj(
    "vehicle_id" -> key.vehicleId,
    "pack_id" -> key.packId,
    "day" -> key.day,
    "min_cell_voltage_v" -> day.minCellVoltageV,
    "max_cell_voltage_v" -> day.maxCellVoltageV,
    "max_cell_imbalance_v" -> day.maxCellImbalanceV,
    "max_pack_temp_c" -> day.maxPackTempC,
    "latest_state_of_health_pct" -> day.latestStateOfHealthPct,
    "event_count" -> day.eventCount.toDouble,
    "schema_version" -> OutputRecords.SchemaVersion
  )
}

final case class ProcessingError(
    errorType: String,
    field: Option[String],
    inputLine: Long,
    ids: BatteryIdentifiers
) extends OutputRecord {
  def toJson: ujson.Obj = {
    def optional(v: Option[String]): ujson.Value = v.fold[ujson.Value](ujson.Null)(ujson.Str(_))
    ujson.Obj(
      "error_type" -> errorType,
      "field" -> optional(field),
      "input_line" -> inputLine.toDouble,
      "event_id" -> optional(ids.eventId),
      "vehicle_id" -> optional(ids.vehicleId),
      "pack_id" -> optional(ids.packId),
      "schema_version" -> OutputRecords.SchemaVersion
    )
  }
}

object ProcessingError {
  val DroppedLate = "DROPPED_LATE"

  def forEvent(errorType: String, inputLine: Long, event: BatteryEvent): ProcessingError =
    ProcessingError(
      errorType,
      None,
      inputLine,
      BatteryIdentifiers(Some(event.eventId), Some(event.vehicleId), Some(event.packId))
    )
}

object OutputRecords {
  val SchemaVersion = "1.0"
}
