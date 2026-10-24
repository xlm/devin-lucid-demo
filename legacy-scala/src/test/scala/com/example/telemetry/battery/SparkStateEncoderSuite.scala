package com.example.telemetry.battery

import org.apache.spark.sql.Encoders

class SparkStateEncoderSuite extends munit.FunSuite {
  test("Spark can derive encoders for the battery stream state and records") {
    assert(Encoders.product[DailyState].schema.fieldNames.sameElements(Array("seenEventIds", "open", "closed")))
    Encoders.product[BatteryHealthProcessor.StreamEvent]
    Encoders.product[BatteryHealthProcessor.KafkaRecord]
    Encoders.product[BatteryHealthProcessor.ParsedLine]
  }
}
