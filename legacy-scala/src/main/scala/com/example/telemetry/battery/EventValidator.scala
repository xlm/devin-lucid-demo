package com.example.telemetry.battery

import java.time.format.{DateTimeFormatter, ResolverStyle}
import java.time.{Instant, LocalDateTime, ZoneOffset}

import scala.util.Try
import scala.util.control.NonFatal

object EventValidator {
  val MalformedJson = "MALFORMED_JSON"
  val MissingField = "MISSING_FIELD"
  val InvalidField = "INVALID_FIELD"
  val UnsupportedSchemaVersion = "UNSUPPORTED_SCHEMA_VERSION"

  val MaxSequenceNumber: Long = 9007199254740991L
  val IdMaxLength = 64
  val SupportedMajorVersion = "1"

  private val VehiclePattern = "VEH-[0-9]{4}".r
  private val SchemaVersionPattern = "([0-9]+)\\.([0-9]+)".r
  private val LocationPattern = "SYN-[A-Z0-9-]+".r
  private val TimestampPattern = "([0-9]{4})-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.[0-9]{3}Z".r
  private val TimestampFormat =
    DateTimeFormatter.ofPattern("uuuu-MM-dd'T'HH:mm:ss.SSS'Z'").withResolverStyle(ResolverStyle.STRICT)

  def parseTimestamp(s: String): Option[Long] = s match {
    case TimestampPattern(year) if year != "0000" =>
      Try(LocalDateTime.parse(s, TimestampFormat).toInstant(ZoneOffset.UTC).toEpochMilli).toOption
    case _ => None
  }

  def formatTimestamp(epochMs: Long): String =
    TimestampFormat.format(LocalDateTime.ofInstant(Instant.ofEpochMilli(epochMs), ZoneOffset.UTC))

  def isValidId(s: String): Boolean = {
    val n = s.codePointCount(0, s.length)
    n >= 1 && n <= IdMaxLength
  }

  def validate(line: String): Either[Rejection, BatteryEvent] =
    parseObject(line) match {
      case None => Left(Rejection(MalformedJson, None, BatteryIdentifiers.Absent))
      case Some(obj) =>
        checkFields(obj).left.map { case (errorType, field) =>
          Rejection(errorType, Some(field), echoIds(obj))
        }
    }

  private def parseObject(line: String): Option[collection.Map[String, ujson.Value]] =
    try ujson.read(line) match {
      case ujson.Obj(fields) => Some(fields)
      case _                 => None
    } catch { case NonFatal(_) => None }

  private def echoIds(obj: collection.Map[String, ujson.Value]): BatteryIdentifiers = {
    def validId(name: String) = obj.get(name).collect { case ujson.Str(s) if isValidId(s) => s }
    BatteryIdentifiers(
      validId("event_id"),
      obj.get("vehicle_id").collect { case ujson.Str(s @ VehiclePattern()) => s },
      validId("pack_id")
    )
  }

  private type Check[A] = Either[(String, String), A]

  private object Timestamp {
    def unapply(s: String): Option[Long] = parseTimestamp(s)
  }

  private def finite(d: Double): Boolean = !d.isNaN && !d.isInfinite

  private def checkFields(obj: collection.Map[String, ujson.Value]): Check[BatteryEvent] = {
    def required(name: String): Check[ujson.Value] = obj.get(name) match {
      case None | Some(ujson.Null) => Left((MissingField, name))
      case Some(v)                 => Right(v)
    }
    def rule[A](name: String)(accept: PartialFunction[ujson.Value, A]): Check[A] =
      required(name).flatMap(v => accept.lift(v).toRight((InvalidField, name)))
    def inRange(name: String, minimum: Double, maximum: Double): Check[Double] =
      rule(name) { case ujson.Num(d) if finite(d) && d >= minimum && d <= maximum => d }

    for {
      major <- rule("schema_version") { case ujson.Str(SchemaVersionPattern(major, _)) => major }
      _ <- Either.cond(major == SupportedMajorVersion, (), (UnsupportedSchemaVersion, "schema_version"))
      eventId <- rule("event_id") { case ujson.Str(s) if isValidId(s) => s }
      vehicleId <- rule("vehicle_id") { case ujson.Str(s @ VehiclePattern()) => s }
      packId <- rule("pack_id") { case ujson.Str(s) if isValidId(s) => s }
      eventTs <- rule("event_timestamp") { case ujson.Str(Timestamp(ms)) => ms }
      _ <- rule("ingestion_timestamp") { case ujson.Str(Timestamp(ms)) => ms }
      sequenceNumber <- rule("sequence_number") {
        case ujson.Num(d)
            if finite(d) && d.isWhole && d >= 0 && d <= MaxSequenceNumber.toDouble =>
          d.toLong
      }
      minVoltage <- inRange("cell_voltage_min_v", 0.0, 5.0)
      maxVoltage <- inRange("cell_voltage_max_v", 0.0, 5.0)
      _ <- Either.cond(maxVoltage >= minVoltage, (), (InvalidField, "cell_voltage_max_v"))
      temperature <- inRange("pack_temp_c", -50.0, 100.0)
      stateOfHealth <- inRange("state_of_health_pct", 0.0, 100.0)
      _ <- obj.get("synthetic_location") match {
        case None | Some(ujson.Null) => Right(())
        case Some(ujson.Str(LocationPattern())) => Right(())
        case _ => Left((InvalidField, "synthetic_location"))
      }
    } yield BatteryEvent(eventId, vehicleId, packId, eventTs, sequenceNumber, minVoltage, maxVoltage, temperature, stateOfHealth)
  }
}
