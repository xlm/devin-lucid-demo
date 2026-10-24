use std::env;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Rules {
    pub allowed_lateness_ms: i64,
    pub inactivity_timeout_ms: i64,
}

impl Rules {
    pub const CONTRACT: Self = Self {
        allowed_lateness_ms: 600_000,
        inactivity_timeout_ms: 1_800_000,
    };
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ServiceConfig {
    pub kafka_bootstrap_servers: String,
    pub kafka_auto_offset_reset: String,
    pub charging_input_topic: String,
    pub charging_summary_topic: String,
    pub charging_error_topic: String,
    pub charging_consumer_group: String,
    pub charging_transactional_id: String,
    pub rules: Rules,
}

impl ServiceConfig {
    pub fn from_env() -> Self {
        Self {
            kafka_bootstrap_servers: env_value("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
            kafka_auto_offset_reset: env_value("KAFKA_AUTO_OFFSET_RESET", "earliest"),
            charging_input_topic: env_value("CHARGING_INPUT_TOPIC", "charging.events.v1"),
            charging_summary_topic: env_value(
                "CHARGING_SUMMARY_TOPIC",
                "charging.session.summary.v1",
            ),
            charging_error_topic: env_value("CHARGING_ERROR_TOPIC", "charging.errors.v1"),
            charging_consumer_group: env_value("CHARGING_CONSUMER_GROUP", "charging-processor"),
            charging_transactional_id: env_value(
                "CHARGING_TRANSACTIONAL_ID",
                "charging-processor-1",
            ),
            rules: Rules {
                allowed_lateness_ms: env_i64(
                    "CHARGING_ALLOWED_LATENESS_MS",
                    Rules::CONTRACT.allowed_lateness_ms,
                ),
                inactivity_timeout_ms: env_i64(
                    "CHARGING_INACTIVITY_TIMEOUT_MS",
                    Rules::CONTRACT.inactivity_timeout_ms,
                ),
            },
        }
    }
}

fn env_value(name: &str, default: &str) -> String {
    env::var(name).unwrap_or_else(|_| default.to_owned())
}

fn env_i64(name: &str, default: i64) -> i64 {
    env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

#[cfg(test)]
mod tests {
    use super::ServiceConfig;
    use std::env;

    #[test]
    fn kafka_auto_offset_reset_defaults_to_earliest() {
        let expected =
            env::var("KAFKA_AUTO_OFFSET_RESET").unwrap_or_else(|_| "earliest".to_owned());
        let config = ServiceConfig::from_env();

        assert_eq!(config.kafka_auto_offset_reset, expected);
    }
}
