package com.example.telemetry.charging

/**
 * Everything replay mode must restore after a restart.
 *
 * @param linesConsumed input lines already processed; the next line number is this plus one
 * @param maxEventTs    max event time over events that advanced the watermark, None before the first
 * @param sessions      per-key state, including closed keys
 */
final case class EngineState(linesConsumed: Long, maxEventTs: Option[Long], sessions: Map[SessionKey, SessionState]) {
  def watermark: Option[Long] = Windowing.watermark(maxEventTs)

  def keyState(key: SessionKey): SessionState = sessions.getOrElse(key, SessionState.Empty)

  def withKey(key: SessionKey, s: SessionState): EngineState = copy(sessions = sessions.updated(key, s))

  def openSessions: Iterable[(SessionKey, SessionAccumulator)] =
    sessions.view.flatMap { case (k, s) => s.open.map(k -> _) }
}

object EngineState {
  val Initial: EngineState = EngineState(0L, None, Map.empty)
}

/**
 * Single-threaded, arrival-order processing of `charging.events.v1` lines. This is the
 * replay form of the streaming job: one event at a time, so the watermark advances per
 * event rather than per micro-batch. It has no Spark dependency.
 */
object ReplayEngine {

  /** Processes the next input line; returns the new state and the records it emits, in order. */
  def step(state: EngineState, line: String): (EngineState, Seq[OutputRecord]) = {
    val lineNo = state.linesConsumed + 1
    val consumed = state.copy(linesConsumed = lineNo)
    EventValidator.validate(line) match {
      case Left(r) => (consumed, Seq(ProcessingError(r.errorType, r.field, lineNo, r.ids)))
      case Right(e) =>
        val key = e.key
        val keyState = consumed.keyState(key)
        // First occurrence wins; a duplicate leaves no trace, not even a watermark change.
        if (keyState.hasSeen(e.eventId)) (consumed, Nil)
        else {
          val seen = keyState.markSeen(e.eventId)
          if (Windowing.isLate(e.eventTs, consumed.watermark))
            (consumed.withKey(key, seen), Seq(ProcessingError.forEvent(ProcessingError.LateEventDropped, lineNo, e)))
          else {
            val (updated, rejected) =
              if (seen.closed) (seen, Seq(ProcessingError.forEvent(ProcessingError.SessionAlreadyClosed, lineNo, e)))
              else (seen.accept(e), Nil)
            // An event for a closed session is not accepted but still advances the watermark.
            val advanced = consumed
              .withKey(key, updated)
              .copy(maxEventTs = Some(consumed.maxEventTs.fold(e.eventTs)(math.max(_, e.eventTs))))
            val (fired, summaries) = fireTimers(advanced, advanced.watermark.get)
            (fired, rejected ++ summaries)
          }
        }
    }
  }

  /** End of input: the watermark becomes +infinity and every open session is emitted. */
  def endOfInput(state: EngineState): (EngineState, Seq[OutputRecord]) =
    fireTimers(state, Windowing.EndOfInput)

  private def fireTimers(state: EngineState, watermark: Long): (EngineState, Seq[SessionSummary]) = {
    val due = Windowing.dueTimers(state.openSessions, watermark)
    val closed = due.foldLeft(state) { case (s, (key, _)) => s.withKey(key, s.keyState(key).close) }
    (closed, due.map { case (key, session) => SessionSummary(key, session) })
  }
}
