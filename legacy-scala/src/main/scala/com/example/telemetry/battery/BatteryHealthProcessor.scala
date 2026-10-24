package com.example.telemetry.battery

import java.sql.Timestamp

import org.apache.spark.sql.functions.expr
import org.apache.spark.sql.streaming.{GroupState, GroupStateTimeout, OutputMode, StreamingQuery, Trigger}
import org.apache.spark.sql.{Dataset, SparkSession}

object BatteryHealthProcessor {
  val InputTopic = "battery.telemetry.v1"
  val SummaryTopic = "battery.health.daily.v1"
  val ErrorTopic = "battery.errors.v1"

  final case class KafkaRecord(topic: String, key: Option[String], value: String)
  final case class RawLine(value: String, position: Long)
  final case class ParsedLine(position: Long, rejection: Option[Rejection], event: Option[BatteryEvent])
  final case class StreamEvent(position: Long, event: BatteryEvent, event_time: Timestamp)

  val Usage: String =
    """usage: BatteryHealthProcessor kafka --bootstrap-servers <hosts> --checkpoint-dir <dir>
      |       BatteryHealthProcessor local --input-dir <dir> --output-dir <dir> --checkpoint-dir <dir>""".stripMargin

  def main(args: Array[String]): Unit = {
    val (mode, options) = args.toList match {
      case currentMode :: rest if rest.size % 2 == 0 =>
        (currentMode, rest.grouped(2).collect { case List(key, value) => key -> value }.toMap)
      case _ => ("", Map.empty[String, String])
    }
    def option(name: String): String = options.getOrElse(name, { System.err.println(Usage); sys.exit(2) })
    val spark = SparkSession.builder().appName("battery-health-processor").getOrCreate()
    val queries = mode match {
      case "kafka" => runKafka(spark, option("--bootstrap-servers"), option("--checkpoint-dir"))
      case "local" => runLocalFiles(spark, option("--input-dir"), option("--output-dir"), option("--checkpoint-dir"))
      case _ =>
        System.err.println(Usage)
        sys.exit(2)
    }
    try {
      while (queries.exists(_.isActive)) {
        spark.streams.awaitAnyTermination()
        spark.streams.resetTerminated()
      }
      queries.flatMap(_.exception).headOption.foreach(throw _)
    } finally spark.stop()
  }

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
    val (validationErrors, dailyOutputs) = pipeline(spark, lines)
    def toKafka(dataset: Dataset[KafkaRecord], name: String) =
      dataset.writeStream
        .format("kafka")
        .option("kafka.bootstrap.servers", bootstrap)
        .option("checkpointLocation", s"$checkpointDir/$name")
        .outputMode(OutputMode.Append())
        .start()
    Seq(toKafka(validationErrors, "validation-errors"), toKafka(dailyOutputs, "daily-summaries"))
  }

  def runLocalFiles(
      spark: SparkSession,
      inputDir: String,
      outputDir: String,
      checkpointDir: String
  ): Seq[StreamingQuery] = {
    import spark.implicits._
    val lines = spark.readStream
      .option("maxFilesPerTrigger", 1)
      .text(inputDir)
      .as[String]
      .map { staged =>
        val tab = staged.indexOf('\t')
        RawLine(staged.substring(tab + 1), staged.substring(0, tab).toLong)
      }
    val (validationErrors, dailyOutputs) = pipeline(spark, lines)
    def toFiles(dataset: Dataset[KafkaRecord], name: String) =
      dataset.writeStream
        .trigger(Trigger.AvailableNow())
        .option("checkpointLocation", s"$checkpointDir/$name")
        .outputMode(OutputMode.Append())
        .foreachBatch { (batch: Dataset[KafkaRecord], batchId: Long) =>
          Seq(SummaryTopic -> "summaries", ErrorTopic -> "errors").foreach { case (topic, directory) =>
            batch.filter(_.topic == topic).map(_.value).write.mode("append")
              .text(s"$outputDir/$directory/$name-batch-${"%08d".format(batchId)}")
          }
        }
        .start()
    Seq(toFiles(validationErrors, "validation"), toFiles(dailyOutputs, "daily"))
  }

  def pipeline(spark: SparkSession, lines: Dataset[RawLine]): (Dataset[KafkaRecord], Dataset[KafkaRecord]) = {
    import spark.implicits._
    val parsed = lines.map { raw =>
      val result = EventValidator.validate(raw.value)
      ParsedLine(raw.position, result.left.toOption, result.toOption)
    }
    val validationErrors = parsed.flatMap { record =>
      record.rejection.map(rejection =>
        errorRecord(ProcessingError(rejection.errorType, rejection.field, record.position, rejection.ids))
      )
    }
    val dailyOutputs = parsed
      .filter(_.event.isDefined)
      .select($"position", $"event", expr("timestamp_millis(event.eventTs)").as("event_time"))
      .withWatermark("event_time", "10 minutes")
      .as[StreamEvent]
      .groupByKey(_.event.key)
      .flatMapGroupsWithState(OutputMode.Append(), GroupStateTimeout.EventTimeTimeout())(updateDay)
    (validationErrors, dailyOutputs)
  }

  def updateDay(
      key: BatteryDailyKey,
      events: Iterator[StreamEvent],
      state: GroupState[DailyState]
  ): Iterator[KafkaRecord] = {
    val current = state.getOption.getOrElse(DailyState.Empty)
    if (state.hasTimedOut) {
      val summaries = current.open.map(day => summaryRecord(BatterySummary(key, day))).toSeq
      state.update(current.close)
      summaries.iterator
    } else {
      val watermark = Option(state.getCurrentWatermarkMs()).filter(_ > 0)
      val (next, errors) = events.foldLeft((current, Vector.empty[KafkaRecord])) {
        case ((daily, output), StreamEvent(position, event, _)) =>
          if (daily.hasSeen(event.eventId)) (daily, output)
          else {
            val seen = daily.markSeen(event.eventId)
            if (Windowing.isLate(event.eventTs, watermark)) {
              (seen, output :+ errorRecord(ProcessingError.forEvent(ProcessingError.DroppedLate, position, event)))
            } else if (seen.closed) {
              (seen, output :+ errorRecord(ProcessingError.forEvent(ProcessingError.DroppedLate, position, event)))
            } else {
              (seen.accept(event), output)
            }
          }
      }
      state.update(next)
      next.open.foreach { day =>
        state.setTimeoutTimestamp(math.max(day.dayEndTs, watermark.getOrElse(0L) + 1))
      }
      errors.iterator
    }
  }

  private def summaryRecord(summary: BatterySummary): KafkaRecord =
    KafkaRecord(SummaryTopic, Some(summary.key.vehicleId), ujson.write(summary.toJson))

  private def errorRecord(error: ProcessingError): KafkaRecord =
    KafkaRecord(ErrorTopic, error.ids.vehicleId, ujson.write(error.toJson))
}
