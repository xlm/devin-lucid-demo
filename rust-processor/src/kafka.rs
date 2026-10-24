//! The transaction boundary covers Kafka-to-Kafka read-process-write on one
//! broker cluster when downstream consumers use `read_committed`; it does not
//! provide an end-to-end guarantee. In-memory dedupe, open sessions and the
//! watermark are lost on restart in this demo. A single instance is assumed,
//! its watermark is global across the partitions it reads, and streaming has
//! no end-of-input flush.

use std::error::Error;
use std::io;
use std::time::Duration;

use rdkafka::consumer::{Consumer, StreamConsumer};
use rdkafka::message::Message;
use rdkafka::producer::{FutureProducer, FutureRecord, Producer};
use rdkafka::util::Timeout;
use rdkafka::ClientConfig;

use crate::config::ServiceConfig;
use crate::processors::charging_session::{ChargingSessionProcessor, Output};

const BATCH_SIZE: usize = 100;
const POLL_TIMEOUT: Duration = Duration::from_millis(250);
const TRANSACTION_TIMEOUT: Duration = Duration::from_secs(30);

pub fn serve(config: ServiceConfig) -> Result<(), Box<dyn Error>> {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?;
    runtime.block_on(serve_async(config))
}

async fn serve_async(config: ServiceConfig) -> Result<(), Box<dyn Error>> {
    let consumer: StreamConsumer = ClientConfig::new()
        .set("bootstrap.servers", &config.kafka_bootstrap_servers)
        .set("group.id", &config.charging_consumer_group)
        .set("isolation.level", "read_committed")
        .set("enable.auto.commit", "false")
        .create()?;
    consumer.subscribe(&[&config.charging_input_topic])?;

    let producer: FutureProducer = ClientConfig::new()
        .set("bootstrap.servers", &config.kafka_bootstrap_servers)
        .set("transactional.id", &config.charging_transactional_id)
        .create()?;
    producer.init_transactions(TRANSACTION_TIMEOUT)?;

    let mut processor = ChargingSessionProcessor::new(config.rules);
    let mut message_count = 0_u64;
    loop {
        let first = match tokio::time::timeout(POLL_TIMEOUT, consumer.recv()).await {
            Ok(Ok(message)) => message.payload().map(|payload| payload.to_vec()),
            Ok(Err(error)) => return Err(Box::new(error)),
            Err(_) => continue,
        };
        let mut batch = vec![first];
        while batch.len() < BATCH_SIZE {
            match tokio::time::timeout(Duration::ZERO, consumer.recv()).await {
                Ok(Ok(message)) => batch.push(message.payload().map(|payload| payload.to_vec())),
                Ok(Err(error)) => return Err(Box::new(error)),
                Err(_) => break,
            }
        }
        producer.begin_transaction()?;
        let result = process_batch(
            &config,
            &consumer,
            &producer,
            &mut processor,
            &mut message_count,
            batch,
        )
        .await;
        if let Err(error) = result {
            let _ = producer.abort_transaction(TRANSACTION_TIMEOUT);
            return Err(error);
        }
    }
}

async fn process_batch(
    config: &ServiceConfig,
    consumer: &StreamConsumer,
    producer: &FutureProducer,
    processor: &mut ChargingSessionProcessor,
    message_count: &mut u64,
    batch: Vec<Option<Vec<u8>>>,
) -> Result<(), Box<dyn Error>> {
    for payload in batch {
        *message_count += 1;
        let line = payload
            .as_deref()
            .and_then(|payload| std::str::from_utf8(payload).ok())
            .unwrap_or("");
        for output in processor.process_line(*message_count, line) {
            produce_output(producer, config, output).await?;
        }
    }
    let offsets = consumer.position()?;
    let group_metadata = consumer.group_metadata().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::NotConnected,
            "consumer group metadata is unavailable",
        )
    })?;
    producer.send_offsets_to_transaction(&offsets, &group_metadata, TRANSACTION_TIMEOUT)?;
    producer.commit_transaction(TRANSACTION_TIMEOUT)?;
    Ok(())
}

async fn produce_output(
    producer: &FutureProducer,
    config: &ServiceConfig,
    output: Output,
) -> Result<(), Box<dyn Error>> {
    match output {
        Output::Summary(record) => {
            let payload = serde_json::to_vec(&record)?;
            let key = record.vehicle_id;
            producer
                .send(
                    FutureRecord::to(&config.charging_summary_topic)
                        .key(&key)
                        .payload(&payload),
                    Timeout::After(TRANSACTION_TIMEOUT),
                )
                .await
                .map_err(|(error, _)| Box::new(error) as Box<dyn Error>)?;
        }
        Output::Error(record) => {
            let payload = serde_json::to_vec(&record)?;
            let key = record.vehicle_id;
            let result = match key.as_deref() {
                Some(key) => {
                    producer
                        .send(
                            FutureRecord::to(&config.charging_error_topic)
                                .key(key)
                                .payload(&payload),
                            Timeout::After(TRANSACTION_TIMEOUT),
                        )
                        .await
                }
                None => {
                    producer
                        .send(
                            FutureRecord::<(), _>::to(&config.charging_error_topic)
                                .payload(&payload),
                            Timeout::After(TRANSACTION_TIMEOUT),
                        )
                        .await
                }
            };
            result.map_err(|(error, _)| Box::new(error) as Box<dyn Error>)?;
        }
    }
    Ok(())
}
