package com.example.telemetry.battery

class EventValidatorSuite extends munit.FunSuite {
  private val base: Map[String, ujson.Value] = Map(
    "schema_version" -> "1.0",
    "event_id" -> "BAT-T-001",
    "vehicle_id" -> "VEH-0999",
    "pack_id" -> "PK-0999-A",
    "event_timestamp" -> "2026-01-15T08:00:00.000Z",
    "ingestion_timestamp" -> "2026-01-15T08:00:03.000Z",
    "sequence_number" -> 1,
    "cell_voltage_min_v" -> 3.5,
    "cell_voltage_max_v" -> 3.8,
    "pack_temp_c" -> 25.0,
    "state_of_health_pct" -> 95.0
  )

  private def line(changes: (String, ujson.Value)*): String =
    ujson.write(ujson.Obj.from(base ++ changes))

  private def rejection(value: String): (String, Option[String]) =
    EventValidator.validate(value).left.map(r => (r.errorType, r.field)).swap.getOrElse(fail("expected rejection"))

  test("valid event parses UTC time and preserves the daily key") {
    val event = EventValidator.validate(line("synthetic_location" -> "SYN-GRID-1")).toOption.get
    assertEquals(event.eventTs, 1768464000000L)
    assertEquals(event.key, BatteryDailyKey("VEH-0999", "PK-0999-A", "2026-01-15"))
  }

  test("schema version and fields use first-failure validation order") {
    assertEquals(rejection("{}"), ("MISSING_FIELD", Some("schema_version")))
    assertEquals(rejection(line("schema_version" -> "2.0")), ("UNSUPPORTED_SCHEMA_VERSION", Some("schema_version")))
    assertEquals(rejection(line("cell_voltage_min_v" -> -1, "pack_temp_c" -> 200)),
      ("INVALID_FIELD", Some("cell_voltage_min_v")))
    assertEquals(rejection(line("cell_voltage_min_v" -> 3.9, "cell_voltage_max_v" -> 3.8)),
      ("INVALID_FIELD", Some("cell_voltage_max_v")))
  }

  test("malformed JSON and independently valid error identifiers are handled") {
    val malformed = EventValidator.validate("[1]").left.toOption.get
    assertEquals((malformed.errorType, malformed.ids), (EventValidator.MalformedJson, BatteryIdentifiers.Absent))
    val invalid = EventValidator.validate(line("state_of_health_pct" -> 101)).left.toOption.get
    assertEquals(invalid.ids, BatteryIdentifiers(Some("BAT-T-001"), Some("VEH-0999"), Some("PK-0999-A")))
  }

  test("measurement bounds, sequence and optional location are guarded") {
    Seq(
      line("cell_voltage_min_v" -> -0.1),
      line("cell_voltage_min_v" -> 5.1),
      line("cell_voltage_min_v" -> 0.0, "cell_voltage_max_v" -> -0.1),
      line("cell_voltage_max_v" -> 5.1),
      line("pack_temp_c" -> -50.1),
      line("pack_temp_c" -> 100.1),
      line("state_of_health_pct" -> -0.1),
      line("state_of_health_pct" -> 100.1),
      line("state_of_health_pct" -> "95"),
      line("sequence_number" -> -1),
      line("sequence_number" -> 9007199254740992d),
      line("state_of_health_pct" -> true),
      line("sequence_number" -> 1.5),
      line("synthetic_location" -> "52.5,13.4")
    ).foreach(value => assert(EventValidator.validate(value).isLeft))
    val lowerBoundary = EventValidator.validate(
      line(
        "sequence_number" -> 0,
        "cell_voltage_min_v" -> 0.0,
        "cell_voltage_max_v" -> 5.0,
        "pack_temp_c" -> -50.0,
        "state_of_health_pct" -> 0.0,
        "synthetic_location" -> ujson.Null
      )
    )
    assert(lowerBoundary.isRight, s"lower bounds were rejected: $lowerBoundary")
    assert(
      EventValidator.validate(line("cell_voltage_min_v" -> 0.0, "cell_voltage_max_v" -> 0.0)).isRight
    )
    assert(
      EventValidator.validate(line("cell_voltage_min_v" -> 5.0, "cell_voltage_max_v" -> 5.0)).isRight
    )
    val upperInput = line(
      "sequence_number" -> ujson.Num(9007199254740991d),
      "pack_temp_c" -> 100.0,
      "state_of_health_pct" -> 100.0
    )
    val upperBoundary = EventValidator.validate(upperInput)
    assert(
      upperBoundary.isRight,
      s"upper bounds were rejected: $upperBoundary input=$upperInput max=${EventValidator.MaxSequenceNumber.toDouble}"
    )
  }

  test("ASCII identifiers and location patterns reject confusable or spaced forms") {
    val invalid = Seq(
      line("vehicle_id" -> "veh-0999"),
      line("vehicle_id" -> "VEH-\u0660\u0669\u0669\u0669"),
      line("vehicle_id" -> "VEH0999"),
      line("synthetic_location" -> "syn-grid-1"),
      line("synthetic_location" -> "SYN-GRID_1"),
      line("synthetic_location" -> " SYN-GRID-1"),
      line("synthetic_location" -> "SYN-GR\uFF29D-1"),
      line("schema_version" -> "\uFF11.0"),
      line("event_timestamp" -> "2026-02-30T08:00:00.000Z"),
      line("event_timestamp" -> "2026-01-15T08:00:00.000+00:00"),
      line("event_timestamp" -> "٢٠٢٦-01-15T08:00:00.000Z"),
      line("pack_id" -> ""),
      line("event_id" -> ("😀" * 65))
    )
    invalid.foreach(value => assert(EventValidator.validate(value).isLeft))
    assert(EventValidator.validate(line("synthetic_location" -> ujson.Null)).isRight)
    assert(EventValidator.validate(line("event_id" -> ("😀" * 64))).isRight)
  }
}
