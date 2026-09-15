// Package config reads moderation service settings from the environment.
package config

import (
	"os"
	"strconv"

	"github.com/Anwesha33/listingforge/moderation-service/internal/policy"
)

type Config struct {
	KafkaBrokers  string
	ConsumerGroup string
	DatabaseURL   string
	ListenAddr    string
	Thresholds    policy.Thresholds
	NeighbourK    int
}

func Load() Config {
	t := policy.DefaultThresholds()
	return Config{
		KafkaBrokers:  env("KAFKA_BROKERS", "localhost:9092"),
		ConsumerGroup: env("CONSUMER_GROUP", "moderation-service"),
		DatabaseURL:   env("DATABASE_URL", "postgres://listingforge:listingforge@localhost:5432/listingforge"),
		ListenAddr:    env("LISTEN_ADDR", ":8082"),
		NeighbourK:    envInt("NEIGHBOUR_K", 5),
		Thresholds: policy.Thresholds{
			DuplicateReject:       float32(envFloat("DUPLICATE_REJECT", float64(t.DuplicateReject))),
			DuplicateReview:       float32(envFloat("DUPLICATE_REVIEW", float64(t.DuplicateReview))),
			MaxExtractionProblems: envInt("MAX_EXTRACTION_PROBLEMS", t.MaxExtractionProblems),
			MinAttributeAgreement: envFloat("MIN_ATTRIBUTE_AGREEMENT", t.MinAttributeAgreement),
		},
	}
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func envFloat(key string, def float64) float64 {
	if v := os.Getenv(key); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil {
			return f
		}
	}
	return def
}
