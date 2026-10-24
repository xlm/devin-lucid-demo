package com.example.telemetry.charging

class ReplayEngineSuite extends munit.FunSuite {
  private val minute = 60000L
  private val t0 = 1768464000000L

  private def ev(id: String, session: String, minutes: Long, energy: Double, power: Double = 10.0,
                 state: String = "CHARGING", vehicle: String = "VEH-0900"): String =
    ujson.write(ujson.Obj(
      "event_id" -> id, "vehicle_id" -> vehicle, "session_id" -> session,
      "event_timestamp" -> EventValidator.formatTimestamp(t0 + minutes * minute),
      "ingestion_timestamp" -> EventValidator.formatTimestamp(t0 + minutes * minute),
      "sequence_number" -> 1, "charging_state" -> state, "battery_soc" -> 50,
      "charging_power_kw" -> power, "energy_delivered_kwh" -> energy, "schema_version" -> "1.0"))

  private def run(lines: Seq[String]): Seq[OutputRecord] = {
    val (state, out) = lines.foldLeft((EngineState.Initial, Vector.empty[OutputRecord])) { case ((s, acc), l) =>
      val (next, o) = ReplayEngine.step(s, l)
      (next, acc ++ o)
    }
    out ++ ReplayEngine.endOfInput(state)._2
  }

  private def summaries(out: Seq[OutputRecord]) = out.collect { case s: SessionSummary => s }
  private def errors(out: Seq[OutputRecord]) = out.collect { case e: ProcessingError => (e.errorType, e.inputLine) }

  test("total energy is the MAX cumulative reading, not a sum, even out of order") {
    val out = run(Seq(ev("e1", "S-A", 0, 0.0), ev("e3", "S-A", 10, 9.0), ev("e2", "S-A", 5, 4.5)))
    val s = summaries(out).ensuring(_.size == 1).head
    assertEquals((s.session.maxEnergyKwh, s.session.eventCount, s.completionReason), (9.0, 3L, "INACTIVITY_TIMEOUT"))
  }

  test("dedupe keeps the first copy and is scoped to the session key") {
    val out = run(Seq(ev("e1", "S-A", 0, 1.0, power = 10), ev("e1", "S-A", 1, 99.0, power = 99),
      ev("e1", "S-B", 1, 2.0)))
    val byKey = summaries(out).map(s => s.key.sessionId -> s.session.maxEnergyKwh).toMap
    assertEquals(byKey, Map("S-A" -> 1.0, "S-B" -> 2.0))
    assertEquals(errors(out), Seq.empty)
  }

  test("an event behind the watermark is dropped late; one for a closed session still advances it") {
    val out = run(Seq(
      ev("a1", "S-A", 0, 1.0, state = "COMPLETE"),
      ev("b1", "S-B", 20, 1.0), // watermark 10 min: S-A (expiry 0) fires
      ev("a2", "S-A", 15, 2.0), // closed session
      ev("c1", "S-C", 9, 1.0), // behind watermark
      ev("a3", "S-A", 60, 3.0) // closed, but moves watermark to 50 min: S-B (expiry 50) fires
    ))
    assertEquals(errors(out), Seq(("SESSION_ALREADY_CLOSED", 3L), ("LATE_EVENT_DROPPED", 4L), ("SESSION_ALREADY_CLOSED", 5L)))
    assertEquals(summaries(out).map(_.key.sessionId), Seq("S-A", "S-B"))
    assertEquals(out.indexWhere(_.isInstanceOf[SessionSummary], 2), 4)
  }

  test("checkpoint round trip restores identical state and a split run equals a single run") {
    val lines = Seq(ev("a1", "S-A", 0, 0.1), ev("a2", "S-A", 3, 0.30000000000000004, power = 1e-7),
      ev("b1", "S-B", 40, 12.345678901234567, state = "COMPLETE"), ev("a1", "S-A", 41, 5.0), ev("a3", "S-A", 42, 1.0))
    val single = run(lines)
    (0 to lines.size).foreach { k =>
      val (mid, first) = lines.take(k).foldLeft((EngineState.Initial, Vector.empty[OutputRecord])) { case ((s, acc), l) =>
        val (n, o) = ReplayEngine.step(s, l)
        (n, acc ++ o)
      }
      val restored = ReplayCheckpoint.fromJson(ujson.read(ujson.write(ReplayCheckpoint.toJson(mid))))
      assertEquals(restored, mid)
      val (end, second) = lines.drop(k).foldLeft((restored, Vector.empty[OutputRecord])) { case ((s, acc), l) =>
        val (n, o) = ReplayEngine.step(s, l)
        (n, acc ++ o)
      }
      assertEquals(first ++ second ++ ReplayEngine.endOfInput(end)._2, single, clue(k))
    }
  }

  test("doubles survive the JSON round trip bit for bit") {
    val r = new scala.util.Random(7)
    (1 to 2000).map(_ => java.lang.Double.longBitsToDouble(r.nextLong())).filter(d => !d.isNaN && !d.isInfinite).foreach { d =>
      assertEquals(java.lang.Double.doubleToRawLongBits(ujson.read(ujson.write(ujson.Num(d))).num),
        java.lang.Double.doubleToRawLongBits(if (d == 0.0) 0.0 else d), clue(d))
    }
  }
}
