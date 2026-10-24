package com.example.telemetry.charging

/**
 * Invariant: a line becomes a ChargingEvent only if every field meets its contract rule
 * (section 4); otherwise the first failing check in section 4.1 order names the error.
 */
class EventValidatorSuite extends munit.FunSuite {
  private val base: Map[String, ujson.Value] = Map(
    "event_id" -> "EVT-T-001",
    "vehicle_id" -> "VEH-0999",
    "session_id" -> "S-0999-A",
    "event_timestamp" -> "2026-01-15T08:00:00.000Z",
    "ingestion_timestamp" -> "2026-01-15T08:00:02.000Z",
    "sequence_number" -> 1,
    "charging_state" -> "CHARGING",
    "battery_soc" -> 20.0,
    "charging_power_kw" -> 50.0,
    "energy_delivered_kwh" -> 0.0,
    "schema_version" -> "1.0"
  )

  private def line(changes: (String, ujson.Value)*): String = ujson.write(ujson.Obj.from(base ++ changes))

  private def without(field: String): String = ujson.write(ujson.Obj.from(base - field))

  private def rejection(l: String): (String, Option[String]) =
    EventValidator.validate(l).left.map(r => (r.errorType, r.field)).swap.getOrElse(fail(s"accepted: $l"))

  private def assertInvalid(field: String, values: ujson.Value*): Unit =
    values.foreach(v => assertEquals(rejection(line(field -> v)), ("INVALID_FIELD", Some(field)), clue(v)))

  /** For JSON number tokens that a Scala literal cannot express, such as 1e400. */
  private def assertInvalidToken(field: String, token: String): Unit =
    assertEquals(rejection(line(field -> 424242).replace("424242", token)), ("INVALID_FIELD", Some(field)), clue(token))

  private def assertValid(changes: (String, ujson.Value)*): Unit =
    assert(EventValidator.validate(line(changes: _*)).isRight, clue(changes))

  test("a well-formed event is accepted with epoch-ms time and integral sequence") {
    val e = EventValidator.validate(line("sequence_number" -> 3.0)).toOption.get
    assertEquals(e.eventTs, 1768464000000L)
    assertEquals(e.sequenceNumber, 3L)
    assertEquals(e.key, SessionKey("VEH-0999", "S-0999-A"))
  }

  test("unparsable, non-object, empty and NaN/Infinity lines are MALFORMED_JSON with null ids") {
    def withToken(token: String) = line("battery_soc" -> 424242).replace("424242", token)
    val malformed = Seq("", " ", "not json", "[1,2]", "42", "null", "\"VEH-0999\"", withToken("NaN"),
      withToken("Infinity"), withToken("-Infinity"), withToken("+5"), withToken("05"), line() + " trailing")
    malformed.foreach { l =>
      val r = EventValidator.validate(l).left.toOption.getOrElse(fail(s"accepted: $l"))
      assertEquals((r.errorType, r.field, r.ids), ("MALFORMED_JSON", None, Identifiers.Absent), clue(l))
    }
  }

  test("schema_version is checked first: missing, malformed, then unsupported major") {
    assertEquals(rejection(ujson.write(ujson.Obj("schema_version" -> "2.0"))), ("UNSUPPORTED_SCHEMA_VERSION", Some("schema_version")))
    assertEquals(rejection(ujson.write(ujson.Obj())), ("MISSING_FIELD", Some("schema_version")))
    assertEquals(rejection(line("schema_version" -> ujson.Null)), ("MISSING_FIELD", Some("schema_version")))
    assertInvalid("schema_version", "v1", "1", "1.0.0", 1.0, " 1.0", "1.0\n", "\u0661.0")
    assertEquals(rejection(line("schema_version" -> "01.0")), ("UNSUPPORTED_SCHEMA_VERSION", Some("schema_version")))
    assertValid("schema_version" -> "1.10")
  }

  test("fields are checked in contract order and the first failure wins") {
    assertEquals(rejection(line("event_id" -> ujson.Null, "battery_soc" -> 500)), ("MISSING_FIELD", Some("event_id")))
    assertEquals(rejection(line("session_id" -> "", "charging_state" -> "X")), ("INVALID_FIELD", Some("session_id")))
    assertEquals(rejection(without("energy_delivered_kwh")), ("MISSING_FIELD", Some("energy_delivered_kwh")))
  }

  test("vehicle_id must be VEH- plus four ASCII digits") {
    assertInvalid("vehicle_id", "WVWZZZ1JZXW000001", "veh-0001", "VEH-001", "VEH-00001", "VEH-0001\n",
      " VEH-0001", "VEH-\u0661\u0662\u0663\u0664", "VEH-\uff10\uff10\uff10\uff11", 1234)
  }

  test("ids are 1-64 code points") {
    assertValid("event_id" -> "x" * 64, "session_id" -> "\ud83d\ude00" * 64)
    assertInvalid("event_id", "", "x" * 65, 7)
    assertInvalid("session_id", "\ud83d\ude00" * 65)
  }

  test("timestamps need exact millisecond UTC format and a real calendar date") {
    assertValid("event_timestamp" -> "2028-02-29T23:59:59.999Z", "ingestion_timestamp" -> "0001-01-01T00:00:00.000Z")
    assertInvalid("event_timestamp", "2026-02-30T08:00:00.000Z", "2026-01-15T24:00:00.000Z",
      "2026-01-15T08:00:60.000Z", "0000-01-01T00:00:00.000Z", "2026-01-15T08:00:00Z", "2026-01-15T08:00:00.0000Z",
      "2026-01-15 08:00:00.000Z", "2026-01-15T08:00:00.000+00:00", "2026-01-15t08:00:00.000z",
      "2026-01-15T08:00:00.000Z\n", "\uff12\uff10\uff12\uff16-01-15T08:00:00.000Z", 1768464000000.0)
    assertInvalid("ingestion_timestamp", "2027-02-29T00:00:00.000Z")
  }

  test("sequence_number is a finite integral value in 0..2^53-1") {
    assertValid("sequence_number" -> 0)
    assertValid("sequence_number" -> 9007199254740991.0)
    assertInvalid("sequence_number", -1, 3.5, 9007199254740992.0, "3", true, ujson.Arr(1))
    assertInvalidToken("sequence_number", "1e400")
  }

  test("measurements must be finite numbers within bounds; booleans and digit strings are not numbers") {
    assertValid("battery_soc" -> 0, "charging_power_kw" -> 0, "energy_delivered_kwh" -> 0)
    assertValid("battery_soc" -> 100)
    assertInvalid("battery_soc", -0.001, 100.001, "50", true)
    assertInvalidToken("battery_soc", "-0.0001e6")
    assertInvalidToken("charging_power_kw", "1e400")
    assertInvalid("charging_power_kw", -5.0, "5.0", false)
    assertInvalid("energy_delivered_kwh", -1e-9, "5.0", true)
  }

  test("charging_state is one of four exact values") {
    assertInvalid("charging_state", "CHARGNG", "complete", "COMPLETE ", 1)
    Seq("IDLE", "CHARGING", "COMPLETE", "FAULT").foreach(s => assertValid("charging_state" -> s))
  }

  test("synthetic_location is optional, nullable, else SYN-[A-Z0-9-]+") {
    assertValid("synthetic_location" -> ujson.Null)
    assertValid("synthetic_location" -> "SYN-GRID-0042")
    assertInvalid("synthetic_location", "52.5200,13.4050", "SYN-", "syn-grid", "SYN-GRID-0042\n", 42)
  }

  test("unknown fields are ignored") {
    assertValid("charger_type" -> "DC_FAST", "firmware_build" -> "syn-1")
  }

  test("id echo copies each identifier only when it is valid on its own") {
    val r = EventValidator.validate(line("vehicle_id" -> "WVWZZZ1JZXW000001", "event_id" -> "")).left.toOption.get
    assertEquals(r.ids, Identifiers(None, None, Some("S-0999-A")))
    assertEquals((r.errorType, r.field), ("INVALID_FIELD", Some("event_id")))
  }
}
