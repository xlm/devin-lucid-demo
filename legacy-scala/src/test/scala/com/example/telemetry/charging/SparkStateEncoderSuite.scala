package com.example.telemetry.charging

import org.apache.spark.sql.Encoders

/** The streaming job keeps SessionState in Spark's state store, so it must be encodable. */
class SparkStateEncoderSuite extends munit.FunSuite {
  test("Spark can derive encoders for the state and records of the streaming job") {
    assert(Encoders.product[SessionState].schema.fieldNames.sameElements(Array("seenEventIds", "open", "closed")))
    Encoders.product[ChargingSessionProcessor.StreamEvent]
    Encoders.product[ChargingSessionProcessor.KafkaRecord]
    Encoders.product[ChargingSessionProcessor.ParsedLine]
  }
}
