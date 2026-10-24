package com.example.telemetry.charging

import java.nio.ByteBuffer
import java.nio.channels.FileChannel
import java.nio.charset.StandardCharsets
import java.nio.file.{Files, Path, StandardCopyOption, StandardOpenOption}

/**
 * Private checkpoint format of the legacy replay mode: the whole EngineState as JSON.
 *
 * It holds vehicle and session identifiers (needed to restore per-key state) but never
 * measurements beyond the session aggregates and never synthetic_location.
 */
object ReplayCheckpoint {
  val Format = "legacy-charging-replay-checkpoint/1"

  def toJson(state: EngineState): ujson.Obj = {
    def num(n: Long) = ujson.Num(n.toDouble)
    val sessions = state.sessions.toSeq
      .sortBy { case (k, _) => (k.vehicleId, k.sessionId) }
      .map { case (k, s) =>
        ujson.Obj(
          "vehicle_id" -> k.vehicleId,
          "session_id" -> k.sessionId,
          "seen_event_ids" -> ujson.Arr.from(s.seenEventIds.toSeq.sorted.map(ujson.Str(_))),
          "closed" -> s.closed,
          "open" -> s.open.fold[ujson.Value](ujson.Null) { a =>
            ujson.Obj(
              "start_ts" -> num(a.startTs),
              "last_ts" -> num(a.lastTs),
              "max_sequence_number" -> num(a.maxSequenceNumber),
              "max_energy_kwh" -> a.maxEnergyKwh,
              "peak_power_kw" -> a.peakPowerKw,
              "event_count" -> num(a.eventCount),
              "complete" -> a.complete
            )
          }
        )
      }
    ujson.Obj(
      "format" -> Format,
      "lines_consumed" -> num(state.linesConsumed),
      "max_event_ts" -> state.maxEventTs.fold[ujson.Value](ujson.Null)(num),
      "sessions" -> ujson.Arr.from(sessions)
    )
  }

  def fromJson(json: ujson.Value): EngineState = {
    require(json("format").str == Format, "unsupported checkpoint format")
    def long(v: ujson.Value) = v.num.toLong
    val sessions = json("sessions").arr.map { s =>
      val open = s("open") match {
        case ujson.Null => None
        case a =>
          Some(
            SessionAccumulator(
              long(a("start_ts")),
              long(a("last_ts")),
              long(a("max_sequence_number")),
              a("max_energy_kwh").num,
              a("peak_power_kw").num,
              long(a("event_count")),
              a("complete").bool
            )
          )
      }
      SessionKey(s("vehicle_id").str, s("session_id").str) ->
        SessionState(s("seen_event_ids").arr.map(_.str).toSet, open, s("closed").bool)
    }
    EngineState(long(json("lines_consumed")), json("max_event_ts").numOpt.map(_.toLong), sessions.toMap)
  }

  /** Writes to a temp file in the target directory, then renames it over the target. */
  def write(state: EngineState, target: Path): Unit = {
    val dir = Option(target.toAbsolutePath.getParent).getOrElse(Path.of("."))
    Files.createDirectories(dir)
    val tmp = Files.createTempFile(dir, s".${target.getFileName}.", ".tmp")
    try {
      val channel = FileChannel.open(tmp, StandardOpenOption.WRITE, StandardOpenOption.TRUNCATE_EXISTING)
      try {
        val buf = ByteBuffer.wrap(ujson.write(toJson(state)).getBytes(StandardCharsets.UTF_8))
        while (buf.hasRemaining) channel.write(buf)
        channel.force(true)
      } finally channel.close()
      Files.move(tmp, target, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
    } finally Files.deleteIfExists(tmp)
  }

  def read(path: Path): EngineState =
    fromJson(ujson.read(new String(Files.readAllBytes(path), StandardCharsets.UTF_8)))
}
