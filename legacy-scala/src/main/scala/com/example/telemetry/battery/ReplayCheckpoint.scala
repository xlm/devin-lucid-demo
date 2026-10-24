package com.example.telemetry.battery

import java.nio.ByteBuffer
import java.nio.channels.FileChannel
import java.nio.charset.StandardCharsets
import java.nio.file.{Files, Path, StandardCopyOption, StandardOpenOption}

object ReplayCheckpoint {
  val Format = "legacy-battery-health-replay-checkpoint/1"

  def toJson(state: EngineState): ujson.Obj = {
    def num(n: Long) = ujson.Num(n.toDouble)
    val days = state.dailyStates.toSeq
      .sortBy { case (key, _) => (key.vehicleId, key.packId, key.day) }(
        Ordering.Tuple3(Windowing.byCodePoint, Windowing.byCodePoint, Windowing.byCodePoint)
      )
      .map { case (key, daily) =>
        ujson.Obj(
          "vehicle_id" -> key.vehicleId,
          "pack_id" -> key.packId,
          "day" -> key.day,
          "seen_event_ids" -> ujson.Arr.from(daily.seenEventIds.toSeq.sorted(Windowing.byCodePoint).map(ujson.Str(_))),
          "closed" -> daily.closed,
          "open" -> daily.open.fold[ujson.Value](ujson.Null) { accumulator =>
            ujson.Obj(
              "day_end_ts" -> num(accumulator.dayEndTs),
              "min_cell_voltage_v" -> accumulator.minCellVoltageV,
              "max_cell_voltage_v" -> accumulator.maxCellVoltageV,
              "max_cell_imbalance_v" -> accumulator.maxCellImbalanceV,
              "max_pack_temp_c" -> accumulator.maxPackTempC,
              "latest_event_ts" -> num(accumulator.latestEventTs),
              "latest_sequence_number" -> num(accumulator.latestSequenceNumber),
              "latest_state_of_health_pct" -> accumulator.latestStateOfHealthPct,
              "event_count" -> num(accumulator.eventCount)
            )
          }
        )
      }
    ujson.Obj(
      "format" -> Format,
      "lines_consumed" -> num(state.linesConsumed),
      "max_event_ts" -> state.maxEventTs.fold[ujson.Value](ujson.Null)(num),
      "daily_states" -> ujson.Arr.from(days)
    )
  }

  def fromJson(json: ujson.Value): EngineState = {
    require(json("format").str == Format, "unsupported checkpoint format")
    def long(value: ujson.Value) = value.num.toLong
    val days = json("daily_states").arr.map { record =>
      val open = record("open") match {
        case ujson.Null => None
        case accumulator =>
          Some(
            DayAccumulator(
              long(accumulator("day_end_ts")),
              accumulator("min_cell_voltage_v").num,
              accumulator("max_cell_voltage_v").num,
              accumulator("max_cell_imbalance_v").num,
              accumulator("max_pack_temp_c").num,
              long(accumulator("latest_event_ts")),
              long(accumulator("latest_sequence_number")),
              accumulator("latest_state_of_health_pct").num,
              long(accumulator("event_count"))
            )
          )
      }
      val key = BatteryDailyKey(record("vehicle_id").str, record("pack_id").str, record("day").str)
      key -> DailyState(record("seen_event_ids").arr.map(_.str).toSet, open, record("closed").bool)
    }
    EngineState(long(json("lines_consumed")), json("max_event_ts").numOpt.map(_.toLong), days.toMap)
  }

  def write(state: EngineState, target: Path): Unit = {
    val dir = Option(target.toAbsolutePath.getParent).getOrElse(Path.of("."))
    Files.createDirectories(dir)
    val tmp = Files.createTempFile(dir, s".${target.getFileName}.", ".tmp")
    try {
      val channel = FileChannel.open(tmp, StandardOpenOption.WRITE, StandardOpenOption.TRUNCATE_EXISTING)
      try {
        val bytes = ujson.write(toJson(state)).getBytes(StandardCharsets.UTF_8)
        val buffer = ByteBuffer.wrap(bytes)
        while (buffer.hasRemaining) channel.write(buffer)
        channel.force(true)
      } finally channel.close()
      Files.move(tmp, target, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
    } finally Files.deleteIfExists(tmp)
  }

  def read(path: Path): EngineState =
    fromJson(ujson.read(new String(Files.readAllBytes(path), StandardCharsets.UTF_8)))
}
