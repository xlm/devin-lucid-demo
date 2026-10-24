ThisBuild / scalaVersion := "2.13.16"
ThisBuild / organization := "com.example.telemetry"

lazy val root = (project in file("."))
  .settings(
    name := "charging-session-processor",
    scalacOptions ++= Seq("-deprecation", "-feature", "-unchecked", "-Xlint"),
    libraryDependencies ++= Seq(
      "com.lihaoyi" %% "ujson" % "4.4.3",
      // Provided: the streaming job compiles against Spark, replay mode never loads it.
      "org.apache.spark" %% "spark-sql" % "3.5.9" % Provided,
      "org.scalameta" %% "munit" % "1.0.4" % Test
    ),
    Test / fork := true
  )
