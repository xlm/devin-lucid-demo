package com.example.telemetry.charging

import java.io.{BufferedWriter, IOException}
import java.nio.charset.StandardCharsets
import java.nio.file.{Files, NoSuchFileException, Path, StandardOpenOption}

import scala.util.control.NonFatal

/**
 * Replay entrypoint: runs the session logic over a JSONL file instead of Kafka.
 *
 * {{{
 * charging-processor replay --input <file> --output-dir <dir>
 *     [--stop-after <n> --checkpoint <file>] [--resume-from <file>]
 * }}}
 *
 * Never touches Spark classes: spark-sql is a Provided dependency and absent at runtime.
 * Logs go to stderr and carry counts only, never vehicle_id, session_id or location values.
 */
object ReplayMain {
  val ExitOk = 0
  val ExitFatal = 1
  val ExitUsage = 2

  val Usage: String =
    """usage: charging-processor replay --input <file> --output-dir <dir>
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
      case Left(problem) =>
        log(s"usage error: $problem")
        System.err.println(Usage)
        ExitUsage
      case Right(opts) =>
        try replay(opts)
        catch {
          case e: NoSuchFileException =>
            log(s"fatal: file not found: ${e.getFile}")
            ExitFatal
          // Exception messages may quote checkpoint content, so only the class name is logged.
          case NonFatal(e) =>
            log(s"fatal: ${e.getClass.getSimpleName}")
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
      m <- pairs(rest, Map.empty)
      input <- m.get("--input").toRight("--input is required")
      outputDir <- m.get("--output-dir").toRight("--output-dir is required")
      stopAfter <- m.get("--stop-after") match {
        case None                                         => Right(None)
        case Some(n) if n.nonEmpty && n.forall(_.isDigit) => n.toLongOption.toRight("--stop-after is too large").map(Some(_))
        case Some(_)                                      => Left("--stop-after must be a non-negative integer")
      }
      _ <- Either.cond(stopAfter.isDefined == m.contains("--checkpoint"), (), "--stop-after and --checkpoint go together")
    } yield Options(
      Path.of(input),
      Path.of(outputDir),
      stopAfter,
      m.get("--checkpoint").map(Path.of(_)),
      m.get("--resume-from").map(Path.of(_))
    )
  }

  /** Input lines are the file split on \n; a final trailing newline adds no extra line. */
  def readLines(input: Path): Vector[String] = {
    val text = new String(Files.readAllBytes(input), StandardCharsets.UTF_8)
    val parts = text.split("\n", -1).toVector
    if (parts.lastOption.contains("")) parts.init else parts
  }

  private def replay(opts: Options): Int = {
    val start = opts.resumeFrom.fold(EngineState.Initial)(ReplayCheckpoint.read)
    val lines = readLines(opts.input)
    if (start.linesConsumed > lines.size) {
      log(s"fatal: checkpoint offset ${start.linesConsumed} is past the end of the input (${lines.size} lines)")
      return ExitFatal
    }
    val batch = lines.drop(start.linesConsumed.toInt)
    val todo = opts.stopAfter.fold(batch)(n => batch.take(math.min(n, Int.MaxValue.toLong).toInt))
    log(s"start: offset=${start.linesConsumed} lines_to_process=${todo.size} resumed=${opts.resumeFrom.isDefined}")

    Files.createDirectories(opts.outputDir)
    val counts = collection.mutable.Map.empty[String, Long].withDefaultValue(0L)
    val end = withAppender(opts.outputDir.resolve("summaries.jsonl")) { summaries =>
      withAppender(opts.outputDir.resolve("errors.jsonl")) { errors =>
        def emit(records: Seq[OutputRecord]): Unit = records.foreach {
          case s: SessionSummary =>
            counts("summaries") += 1
            writeLine(summaries, s.toJson)
          case e: ProcessingError =>
            counts(e.errorType) += 1
            writeLine(errors, e.toJson)
        }
        val processed = todo.foldLeft(start) { (state, line) =>
          val (next, out) = ReplayEngine.step(state, line)
          emit(out)
          next
        }
        if (opts.stopAfter.isDefined) processed
        else {
          val (flushed, out) = ReplayEngine.endOfInput(processed)
          emit(out)
          flushed
        }
      }
    }
    // Outputs are flushed and closed before the checkpoint exists. A crash in between
    // re-emits on resume (at-least-once); this is not exactly-once.
    opts.checkpoint.foreach { path =>
      ReplayCheckpoint.write(end, path)
      log(s"checkpoint written: offset=${end.linesConsumed}")
    }
    val errorCounts = counts.toSeq.filter(_._1 != "summaries").sorted.map { case (t, n) => s"$t=$n" }
    log(
      s"done: offset=${end.linesConsumed} summaries=${counts("summaries")} " +
        s"errors=${errorCounts.mkString("{", ",", "}")} open_sessions=${end.openSessions.size} " +
        s"end_of_input_flush=${opts.stopAfter.isEmpty}"
    )
    ExitOk
  }

  private def withAppender[A](path: Path)(f: BufferedWriter => A): A = {
    val w = Files.newBufferedWriter(path, StandardCharsets.UTF_8, StandardOpenOption.CREATE, StandardOpenOption.APPEND)
    try f(w)
    finally w.close()
  }

  @throws[IOException]
  private def writeLine(w: BufferedWriter, record: ujson.Obj): Unit = {
    w.write(ujson.write(record))
    w.write('\n')
  }

  private def log(msg: String): Unit = System.err.println(s"[legacy-replay] $msg")
}
