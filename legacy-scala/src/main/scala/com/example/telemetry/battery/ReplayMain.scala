package com.example.telemetry.battery

import java.io.{BufferedWriter, IOException}
import java.nio.charset.StandardCharsets
import java.nio.file.{Files, NoSuchFileException, Path, StandardOpenOption}

import scala.util.control.NonFatal

object ReplayMain {
  val ExitOk = 0
  val ExitFatal = 1
  val ExitUsage = 2

  val Usage: String =
    """usage: battery-health-processor replay --input <file> --output-dir <dir>
      |           [--stop-after <n> --checkpoint <file>] [--resume-from <file>]""".stripMargin

  final case class Options(
      input: Path,
      outputDir: Path,
      stopAfter: Option[Long],
      checkpoint: Option[Path],
      resumeFrom: Option[Path]
  )

  def main(args: Array[String]): Unit = sys.exit(run(args.toList))

  def run(args: List[String]): Int =
    parseArgs(args) match {
      case Left(_) =>
        log("usage error")
        System.err.println(Usage)
        ExitUsage
      case Right(options) =>
        try replay(options)
        catch {
          case _: NoSuchFileException =>
            log("fatal: file not found")
            ExitFatal
          case NonFatal(error) =>
            log(s"fatal: ${error.getClass.getSimpleName}")
            ExitFatal
        }
    }

  def parseArgs(args: List[String]): Either[String, Options] = {
    val known = Set("--input", "--output-dir", "--stop-after", "--checkpoint", "--resume-from")
    def pairs(rest: List[String], acc: Map[String, String]): Either[String, Map[String, String]] = rest match {
      case Nil => Right(acc)
      case flag :: _ if !known(flag) => Left(s"unknown option $flag")
      case flag :: _ if acc.contains(flag) => Left(s"duplicate option $flag")
      case flag :: value :: tail if !value.startsWith("--") => pairs(tail, acc.updated(flag, value))
      case flag :: _ => Left(s"missing value for $flag")
    }
    for {
      rest <- args match {
        case "replay" :: tail => Right(tail)
        case _                => Left("expected subcommand 'replay'")
      }
      values <- pairs(rest, Map.empty)
      input <- values.get("--input").toRight("--input is required")
      outputDir <- values.get("--output-dir").toRight("--output-dir is required")
      stopAfter <- values.get("--stop-after") match {
        case None => Right(None)
        case Some(value) if value.nonEmpty && value.forall(_.isDigit) =>
          value.toLongOption.toRight("--stop-after is too large").map(Some(_))
        case Some(_) => Left("--stop-after must be a non-negative integer")
      }
      _ <- Either.cond(
        stopAfter.isDefined == values.contains("--checkpoint"),
        (),
        "--stop-after and --checkpoint go together"
      )
    } yield Options(
      Path.of(input),
      Path.of(outputDir),
      stopAfter,
      values.get("--checkpoint").map(Path.of(_)),
      values.get("--resume-from").map(Path.of(_))
    )
  }

  def readLines(input: Path): Vector[String] = {
    val text = new String(Files.readAllBytes(input), StandardCharsets.UTF_8)
    val parts = text.split("\n", -1).toVector
    if (parts.lastOption.contains("")) parts.init else parts
  }

  private def replay(options: Options): Int = {
    val start = options.resumeFrom.fold(EngineState.Initial)(ReplayCheckpoint.read)
    val lines = readLines(options.input)
    if (start.linesConsumed > lines.size) {
      log(s"fatal: checkpoint offset ${start.linesConsumed} is past the end of the input (${lines.size} lines)")
      return ExitFatal
    }
    val batch = lines.drop(start.linesConsumed.toInt)
    val todo = options.stopAfter.fold(batch)(count => batch.take(math.min(count, Int.MaxValue.toLong).toInt))
    log(s"start: offset=${start.linesConsumed} lines_to_process=${todo.size} resumed=${options.resumeFrom.isDefined}")

    Files.createDirectories(options.outputDir)
    val counts = collection.mutable.Map.empty[String, Long].withDefaultValue(0L)
    val end = withAppender(options.outputDir.resolve("summaries.jsonl")) { summaries =>
      withAppender(options.outputDir.resolve("errors.jsonl")) { errors =>
        def emit(records: Seq[OutputRecord]): Unit = records.foreach {
          case summary: BatterySummary =>
            counts("summaries") += 1
            writeLine(summaries, summary.toJson)
          case error: ProcessingError =>
            counts(error.errorType) += 1
            writeLine(errors, error.toJson)
        }
        val processed = todo.foldLeft(start) { (state, line) =>
          val (next, output) = ReplayEngine.step(state, line)
          emit(output)
          next
        }
        if (options.stopAfter.isDefined) processed
        else {
          val (flushed, output) = ReplayEngine.endOfInput(processed)
          emit(output)
          flushed
        }
      }
    }
    options.checkpoint.foreach { path =>
      ReplayCheckpoint.write(end, path)
      log(s"checkpoint written: offset=${end.linesConsumed}")
    }
    val errorCounts = counts.toSeq.filter(_._1 != "summaries").sorted.map { case (kind, count) =>
      s"$kind=$count"
    }
    log(
      s"done: offset=${end.linesConsumed} summaries=${counts("summaries")} " +
        s"errors=${errorCounts.mkString("{", ",", "}")} open_days=${end.openDays.size} " +
        s"end_of_input_flush=${options.stopAfter.isEmpty}"
    )
    ExitOk
  }

  private def withAppender[A](path: Path)(f: BufferedWriter => A): A = {
    val writer = Files.newBufferedWriter(path, StandardCharsets.UTF_8, StandardOpenOption.CREATE, StandardOpenOption.APPEND)
    try f(writer)
    finally writer.close()
  }

  @throws[IOException]
  private def writeLine(writer: BufferedWriter, record: ujson.Obj): Unit = {
    writer.write(ujson.write(record))
    writer.write('\n')
  }

  private def log(message: String): Unit = System.err.println(s"[legacy-battery-replay] $message")
}
