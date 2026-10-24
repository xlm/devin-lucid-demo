package com.example.telemetry.battery

class ReplayEngineSuite extends munit.FunSuite {
  private def event(id: String, minute: Int, sequence: Int, soh: Double, vmin: Double = 3.5, vmax: Double = 3.8): String =
    ujson.write(ujson.Obj(
      "schema_version" -> "1.0",
      "event_id" -> id,
      "vehicle_id" -> "VEH-0900",
      "pack_id" -> "PK-0900-A",
      "event_timestamp" -> EventValidator.formatTimestamp(1768464000000L + minute * 60000L),
      "ingestion_timestamp" -> EventValidator.formatTimestamp(1768464000000L + minute * 60000L),
      "sequence_number" -> sequence,
      "cell_voltage_min_v" -> vmin,
      "cell_voltage_max_v" -> vmax,
      "pack_temp_c" -> 25.0,
      "state_of_health_pct" -> soh
    ))

  test("dedupe precedes lateness and checkpoint preserves daily state") {
    val lines = Seq(event("a", 0, 1, 96.0), event("b", 30, 2, 95.0), event("a", 0, 1, 10.0))
    val (afterFirstTwo, _) = lines.take(2).foldLeft((EngineState.Initial, Vector.empty[OutputRecord])) {
      case ((state, output), line) =>
        val (next, records) = ReplayEngine.step(state, line)
        (next, output ++ records)
    }
    assertEquals(ReplayCheckpoint.fromJson(ReplayCheckpoint.toJson(afterFirstTwo)), afterFirstTwo)
    val (state, errors) = ReplayEngine.step(afterFirstTwo, lines(2))
    assertEquals(errors, Seq.empty)
    assertEquals(state.linesConsumed, 3L)
    val (end, summaries) = ReplayEngine.endOfInput(state)
    assertEquals(summaries.collect { case summary: BatterySummary => summary.day.eventCount }, Seq(2L))
    assertEquals(end.dailyStates.values.map(_.closed).toSet, Set(true))
  }
}
