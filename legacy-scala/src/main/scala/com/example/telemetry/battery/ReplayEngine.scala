package com.example.telemetry.battery

final case class EngineState(
    linesConsumed: Long,
    maxEventTs: Option[Long],
    dailyStates: Map[BatteryDailyKey, DailyState]
) {
  def watermark: Option[Long] = Windowing.watermark(maxEventTs)

  def keyState(key: BatteryDailyKey): DailyState = dailyStates.getOrElse(key, DailyState.Empty)

  def withKey(key: BatteryDailyKey, state: DailyState): EngineState =
    copy(dailyStates = dailyStates.updated(key, state))

  def openDays: Iterable[(BatteryDailyKey, DayAccumulator)] =
    dailyStates.view.flatMap { case (key, state) => state.open.map(key -> _) }
}

object EngineState {
  val Initial: EngineState = EngineState(0L, None, Map.empty)
}

object ReplayEngine {
  def step(state: EngineState, line: String): (EngineState, Seq[OutputRecord]) = {
    val lineNo = state.linesConsumed + 1
    val consumed = state.copy(linesConsumed = lineNo)
    EventValidator.validate(line) match {
      case Left(rejection) =>
        (consumed, Seq(ProcessingError(rejection.errorType, rejection.field, lineNo, rejection.ids)))
      case Right(event) =>
        val key = event.key
        val keyState = consumed.keyState(key)
        if (keyState.hasSeen(event.eventId)) (consumed, Nil)
        else {
          val seen = keyState.markSeen(event.eventId)
          if (Windowing.isLate(event.eventTs, consumed.watermark))
            (consumed.withKey(key, seen), Seq(ProcessingError.forEvent(ProcessingError.DroppedLate, lineNo, event)))
          else {
            val updated = seen.accept(event)
            val advanced = consumed
              .withKey(key, updated)
              .copy(maxEventTs = Some(consumed.maxEventTs.fold(event.eventTs)(math.max(_, event.eventTs))))
            val (fired, summaries) = fireTimers(advanced, advanced.watermark.get)
            (fired, summaries)
          }
        }
    }
  }

  def endOfInput(state: EngineState): (EngineState, Seq[OutputRecord]) =
    fireTimers(state, Windowing.EndOfInput)

  private def fireTimers(state: EngineState, watermark: Long): (EngineState, Seq[BatterySummary]) = {
    val due = Windowing.dueTimers(state.openDays, watermark)
    val closed = due.foldLeft(state) { case (current, (key, _)) =>
      current.withKey(key, current.keyState(key).close)
    }
    (closed, due.map { case (key, day) => BatterySummary(key, day) })
  }
}
