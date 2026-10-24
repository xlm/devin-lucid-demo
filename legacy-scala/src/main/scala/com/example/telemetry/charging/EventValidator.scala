package com.example.telemetry.charging

import java.time.format.{DateTimeFormatter, ResolverStyle}
import java.time.{Instant, LocalDateTime, ZoneOffset}

import scala.util.Try
import scala.util.control.NonFatal

/** Identifiers echoed into an error record; each is None when not valid on its own. */
final case class Identifiers(eventId: Option[String], vehicleId: Option[String], sessionId: Option[String])

object Identifiers {
  val Absent: Identifiers = Identifiers(None, None, None)
}

final case class Rejection(errorType: String, field: Option[String], ids: Identifiers)

/** Validates one input line against `charging.events.v1`, first failure wins. */
object EventValidator {
  val MalformedJson = "MALFORMED_JSON"
  val MissingField = "MISSING_FIELD"
  val InvalidField = "INVALID_FIELD"
  val UnsupportedSchemaVersion = "UNSUPPORTED_SCHEMA_VERSION"

  val MaxSequenceNumber: Long = 9007199254740991L
  val IdMaxLength = 64
  val SupportedMajorVersion = "1"

  // Regex patterns match the whole string, so a trailing newline never slips through.
  private val VehiclePattern = "VEH-[0-9]{4}".r
  private val SchemaVersionPattern = "([0-9]+)\\.([0-9]+)".r
  private val LocationPattern = "SYN-[A-Z0-9-]+".r
  private val TimestampPattern = "([0-9]{4})-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.[0-9]{3}Z".r

  private val TimestampFormat =
    DateTimeFormatter.ofPattern("uuuu-MM-dd'T'HH:mm:ss.SSS'Z'").withResolverStyle(ResolverStyle.STRICT)

  /** Epoch ms of a strict `YYYY-MM-DDTHH:MM:SS.sssZ` UTC timestamp on a real calendar date. */
  def parseTimestamp(s: String): Option[Long] = s match {
    // Year 0000 parses under STRICT but is not a calendar year the contract accepts.
    case TimestampPattern(year) if year != "0000" =>
      Try(LocalDateTime.parse(s, TimestampFormat).toInstant(ZoneOffset.UTC).toEpochMilli).toOption
    case _ => None
  }

  def formatTimestamp(epochMs: Long): String =
    TimestampFormat.format(LocalDateTime.ofInstant(Instant.ofEpochMilli(epochMs), ZoneOffset.UTC))

  /** Length in code points, not UTF-16 units. */
  def isValidId(s: String): Boolean = {
    val n = s.codePointCount(0, s.length)
    n >= 1 && n <= IdMaxLength
  }

  def validate(line: String): Either[Rejection, ChargingEvent] =
    parseObject(line) match {
      case None => Left(Rejection(MalformedJson, None, Identifiers.Absent))
      case Some(obj) =>
        checkFields(obj).left.map { case (errorType, field) => Rejection(errorType, Some(field), echoIds(obj)) }
    }

  // ujson rejects NaN/Infinity tokens and empty input, matching the contract's MALFORMED_JSON.
  private def parseObject(line: String): Option[collection.Map[String, ujson.Value]] =
    try ujson.read(line) match {
      case ujson.Obj(fields) => Some(fields)
      case _                 => None
    } catch { case NonFatal(_) => None }

  private def echoIds(obj: collection.Map[String, ujson.Value]): Identifiers = {
    def validId(name: String) = obj.get(name).collect { case ujson.Str(s) if isValidId(s) => s }
    Identifiers(
      validId("event_id"),
      obj.get("vehicle_id").collect { case ujson.Str(s @ VehiclePattern()) => s },
      validId("session_id")
    )
  }

  private type Check[A] = Either[(String, String), A]

  private object Timestamp {
    def unapply(s: String): Option[Long] = parseTimestamp(s)
  }

  private def finite(d: Double): Boolean = !d.isNaN && !d.isInfinite

  private def checkFields(obj: collection.Map[String, ujson.Value]): Check[ChargingEvent] = {
    def required(name: String): Check[ujson.Value] = obj.get(name) match {
      case None | Some(ujson.Null) => Left((MissingField, name))
      case Some(v)                       => Right(v)
    }
    def rule[A](name: String)(accept: PartialFunction[ujson.Value, A]): Check[A] =
      required(name).flatMap(v => accept.lift(v).toRight((InvalidField, name)))

    // ujson.Num carries every JSON number as a Double, so 3 and 3.0 are the same value.
    // Booleans are ujson.Bool and digit strings are ujson.Str, so neither is a number.
    for {
      major <- rule("schema_version") { case ujson.Str(SchemaVersionPattern(major, _)) => major }
      _ <- Either.cond(major == SupportedMajorVersion, (), (UnsupportedSchemaVersion, "schema_version"))
      eventId <- rule("event_id") { case ujson.Str(s) if isValidId(s) => s }
      vehicleId <- rule("vehicle_id") { case ujson.Str(s @ VehiclePattern()) => s }
      sessionId <- rule("session_id") { case ujson.Str(s) if isValidId(s) => s }
      eventTs <- rule("event_timestamp") { case ujson.Str(Timestamp(ms)) => ms }
      _ <- rule("ingestion_timestamp") { case ujson.Str(Timestamp(ms)) => ms }
      seq <- rule("sequence_number") {
        case ujson.Num(d) if finite(d) && d.isWhole && d >= 0 && d <= MaxSequenceNumber.toDouble => d.toLong
      }
      state <- rule("charging_state") { case ujson.Str(s) if ChargingEvent.States(s) => s }
      soc <- rule("battery_soc") { case ujson.Num(d) if finite(d) && d >= 0 && d <= 100 => d }
      power <- rule("charging_power_kw") { case ujson.Num(d) if finite(d) && d >= 0 => d }
      energy <- rule("energy_delivered_kwh") { case ujson.Num(d) if finite(d) && d >= 0 => d }
      _ <- obj.get("synthetic_location") match {
        case None | Some(ujson.Null) | Some(ujson.Str(LocationPattern())) => Right(())
        case Some(_) => Left((InvalidField, "synthetic_location"))
      }
    } yield ChargingEvent(eventId, vehicleId, sessionId, eventTs, seq, state, soc, power, energy)
  }
}
