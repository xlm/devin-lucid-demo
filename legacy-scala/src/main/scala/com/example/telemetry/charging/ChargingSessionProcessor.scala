package com.example.telemetry.charging

import java.sql.Timestamp

import org.apache.spark.sql.functions.expr
import org.apache.spark.sql.streaming.{GroupState, GroupStateTimeout, OutputMode, StreamingQuery, Trigger}
import org.apache.spark.sql.{Dataset, SparkSession}

/**
 * Legacy Spark Structured Streaming job: charging.events.v1 -> charging.session.summary.v1.
 *
 * Reads raw JSON from Kafka (or, in `local` mode, from staged files for bin/spark-local),
 * validates each record with [[EventValidator]], groups by
 * (vehicle_id, session_id) and aggregates with flatMapGroupsWithState under an event-time
 * timeout and a 10 minute watermark. Summaries are keyed by vehicle_id, so ordering holds
 * per vehicle within a partition only. Validation, lateness and closed-session errors go to
 * charging.errors.v1.
 *
 * The approved behaviour is the replay contract (ReplayMain, docs/build-contract.md
 * sections 4-7). This job only approximates it. Known differences, kept here as legacy
 * documentation, not as guarantees:
 *
 *  - The watermark advances once per micro-batch (max event time of the previous batch
 *    minus 10 minutes), not per event. Whether an event is late, and when timers fire,
 *    depends on how the broker and the trigger cut the stream into batches.
 *  - Spark filters rows behind the watermark before the state function runs, so most late
 *    events are dropped silently instead of producing LATE_EVENT_DROPPED. The lateness
 *    check in [[updateSession]] only catches what the batch watermark lets through.
 *  - A group times out once the watermark has moved past its timeout timestamp. The replay
 *    contract fires when expiry <= watermark, so boundary cases may close one batch later.
 *  - Within a batch, event order inside a group and the order across groups are not
 *    arrival order. The replay contract emits timers in (expiry, vehicle_id, session_id)
 *    order; this job has no order across keys at all.
 *  - There is no end of input. Open sessions are emitted only when a later watermark
 *    passes them, never by a final flush.
 *  - input_line in error records is the Kafka offset + 1 (local mode: the staged line
 *    number), not a line of a replay file.
 *  - Delivery is at-least-once: the Kafka sink is not transactional, so a restart from the
 *    checkpoint can re-emit summaries and errors. Downstream consumers must upsert by key.
 *  - Closed keys keep their state forever (no TTL), so the state store grows without bound.
 */
object ChargingSessionProcessor {
  val InputTopic = "charging.events.v1"
  val SummaryTopic = "charging.session.summary.v1"
  val ErrorTopic = "charging.errors.v1"

  /** One output record; `topic` routes summaries and errors. */
  final case class KafkaRecord(topic: String, key: Option[String], value: String)

  /** An input line with its 1-based position in the stream (Kafka offset + 1, or file line). */
  final case class RawLine(value: String, position: Long)

  final case class ParsedLine(position: Long, rejection: Option[Rejection], event: Option[ChargingEvent])

  /** A valid event with its stream position and the event-time column the watermark tracks. */
  final case class StreamEvent(position: Long, event: ChargingEvent, event_time: Timestamp)

  val Usage: String =
    """usage: ChargingSessionProcessor kafka --bootstrap-servers <hosts> --checkpoint-dir <dir>
      |       ChargingSessionProcessor local --input-dir <dir> --output-dir <dir> --checkpoint-dir <dir>""".stripMargin

  def main(args: Array[String]): Unit = {
    val (mode, opts) = args.toList match {
      case m :: rest if rest.size % 2 == 0 => (m, rest.grouped(2).collect { case List(k, v) => k -> v }.toMap)
      case _                               => ("", Map.empty[String, String])
    }
    def opt(name: String): String = opts.getOrElse(name, { System.err.println(Usage); sys.exit(2) })
    val spark = SparkSession.builder().appName("charging-session-processor").getOrCreate()
    val queries = mode match {
      case "kafka" => runKafka(spark, opt("--bootstrap-servers"), opt("--checkpoint-dir"))
      case "local" => runLocalFiles(spark, opt("--input-dir"), opt("--output-dir"), opt("--checkpoint-dir"))
      case _ =>
        System.err.println(Usage)
        sys.exit(2)
    }
    queries.foreach(_.awaitTermination())
  }

  /** Production wiring: Kafka in, Kafka out (needs spark-sql-kafka-0-10 at runtime). */
  def runKafka(spark: SparkSession, bootstrap: String, checkpointDir: String): Seq[StreamingQuery] = {
    import spark.implicits._
    val lines = spark.readStream
      .format("kafka")
      .option("kafka.bootstrap.servers", bootstrap)
      .option("subscribe", InputTopic)
      .option("startingOffsets", "earliest")
      .load()
      .selectExpr("CAST(value AS STRING) AS value", "offset + 1 AS position")
      .as[RawLine]
    val (validationErrors, sessionOutputs) = pipeline(spark, lines)
    def toKafka(ds: Dataset[KafkaRecord], name: String) =
      ds.writeStream
        .format("kafka")
        .option("kafka.bootstrap.servers", bootstrap)
        .option("checkpointLocation", s"$checkpointDir/$name")
        .outputMode(OutputMode.Append())
        .start()
    Seq(toKafka(validationErrors, "validation-errors"), toKafka(sessionOutputs, "sessions"))
  }

  /**
   * Cluster-free smoke mode used by bin/spark-local: a directory of staged text files in,
   * JSONL part files out, Trigger.AvailableNow so the query stops when the input is drained.
   * Each staged line is `<line number>\t<original line>`, standing in for the Kafka offset.
   */
  def runLocalFiles(spark: SparkSession, inputDir: String, outputDir: String, checkpointDir: String): Seq[StreamingQuery] = {
    import spark.implicits._
    val lines = spark.readStream
      .option("maxFilesPerTrigger", 1)
      .text(inputDir)
      .as[String]
      .map { staged =>
        val tab = staged.indexOf('\t')
        RawLine(staged.substring(tab + 1), staged.substring(0, tab).toLong)
      }
    val (validationErrors, sessionOutputs) = pipeline(spark, lines)
    def toFiles(ds: Dataset[KafkaRecord], name: String) =
      ds.writeStream
        .trigger(Trigger.AvailableNow())
        .option("checkpointLocation", s"$checkpointDir/$name")
        .outputMode(OutputMode.Append())
        .foreachBatch { (batch: Dataset[KafkaRecord], batchId: Long) =>
          Seq(SummaryTopic -> "summaries", ErrorTopic -> "errors").foreach { case (topic, dir) =>
            batch.filter(_.topic == topic).map(_.value).write.mode("append").text(s"$outputDir/$dir/$name-batch-$batchId")
          }
        }
        .start()
    Seq(toFiles(validationErrors, "validation"), toFiles(sessionOutputs, "sessions"))
  }

  /** Validation, then per-session state. Returns (validation errors, session summaries and errors). */
  def pipeline(spark: SparkSession, lines: Dataset[RawLine]): (Dataset[KafkaRecord], Dataset[KafkaRecord]) = {
    import spark.implicits._
    val parsed = lines.map { r =>
      val result = EventValidator.validate(r.value)
      ParsedLine(r.position, result.left.toOption, result.toOption)
    }

    val validationErrors = parsed.flatMap { p =>
      p.rejection.map(r => errorRecord(ProcessingError(r.errorType, r.field, p.position, r.ids)))
    }

    val sessionOutputs = parsed
      .filter(_.event.isDefined)
      .select($"position", $"event", expr("timestamp_millis(event.eventTs)").as("event_time"))
      .withWatermark("event_time", "10 minutes")
      .as[StreamEvent]
      .groupByKey(_.event.key)
      .flatMapGroupsWithState(OutputMode.Append(), GroupStateTimeout.EventTimeTimeout())(updateSession)

    (validationErrors, sessionOutputs)
  }

  /**
   * State function for one session key. Applies the same dedupe, lateness and closed-session
   * rules as [[ReplayEngine]], but against the batch watermark (see the header).
   */
  def updateSession(
      key: SessionKey,
      events: Iterator[StreamEvent],
      state: GroupState[SessionState]
  ): Iterator[KafkaRecord] = {
    val current = state.getOption.getOrElse(SessionState.Empty)

    if (state.hasTimedOut) {
      val summaries = current.open.map(s => summaryRecord(SessionSummary(key, s))).toSeq
      state.update(current.close)
      summaries.iterator
    } else {
      val watermark = Option(state.getCurrentWatermarkMs()).filter(_ > 0)
      val (next, errors) = events.foldLeft((current, Vector.empty[KafkaRecord])) { case ((s, out), StreamEvent(position, e, _)) =>
        if (s.hasSeen(e.eventId)) (s, out)
        else {
          val seen = s.markSeen(e.eventId)
          val errorType =
            if (Windowing.isLate(e.eventTs, watermark)) Some(ProcessingError.LateEventDropped)
            else if (seen.closed) Some(ProcessingError.SessionAlreadyClosed)
            else None
          errorType match {
            case Some(t) => (seen, out :+ errorRecord(ProcessingError.forEvent(t, position, e)))
            case None    => (seen.accept(e), out)
          }
        }
      }
      state.update(next)
      next.open.foreach { s =>
        // Spark rejects a timeout earlier than the current watermark.
        state.setTimeoutTimestamp(math.max(Windowing.expiry(s), watermark.getOrElse(0L) + 1))
      }
      errors.iterator
    }
  }

  private def summaryRecord(s: SessionSummary): KafkaRecord =
    KafkaRecord(SummaryTopic, Some(s.key.vehicleId), ujson.write(s.toJson))

  private def errorRecord(e: ProcessingError): KafkaRecord =
    KafkaRecord(ErrorTopic, e.ids.vehicleId, ujson.write(e.toJson))
}
