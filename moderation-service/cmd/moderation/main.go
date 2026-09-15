// Command moderation consumes enriched listings, applies content policy and
// duplicate detection, and publishes a decision.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/segmentio/kafka-go"

	"github.com/Anwesha33/listingforge/moderation-service/internal/config"
	"github.com/Anwesha33/listingforge/moderation-service/internal/dedup"
	"github.com/Anwesha33/listingforge/moderation-service/internal/events"
	"github.com/Anwesha33/listingforge/moderation-service/internal/policy"
)

var (
	decisions = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "moderation_decisions_total",
		Help: "Automated moderation decisions by verdict.",
	}, []string{"verdict"})

	reasonsFired = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "moderation_reasons_total",
		Help: "Policy rules and signals that contributed to a decision.",
	}, []string{"code"})

	duration = prometheus.NewHistogram(prometheus.HistogramOpts{
		Name:    "moderation_duration_seconds",
		Help:    "Time to reach a decision, including the nearest-neighbour query.",
		Buckets: []float64{0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1},
	})
)

func main() {
	cfg := config.Load()
	prometheus.MustRegister(decisions, reasonsFired, duration)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	pool, err := pgxpool.New(ctx, cfg.DatabaseURL)
	if err != nil {
		log.Fatalf("connect to postgres: %v", err)
	}
	defer pool.Close()
	if err := pool.Ping(ctx); err != nil {
		log.Fatalf("ping postgres: %v", err)
	}

	finder := dedup.New(pool)

	reader := kafka.NewReader(kafka.ReaderConfig{
		Brokers: []string{cfg.KafkaBrokers},
		GroupID: cfg.ConsumerGroup,
		Topic:   events.TopicEnriched,
		// Offsets are committed explicitly after the decision has been
		// published, so a crash mid-decision replays the listing rather than
		// losing it.
		CommitInterval: 0,
		StartOffset:    kafka.FirstOffset,
	})
	defer reader.Close()

	writer := &kafka.Writer{
		Addr:     kafka.TCP(cfg.KafkaBrokers),
		Balancer: &kafka.Hash{},
		// Wait for all replicas: a lost decision leaves a listing stuck in
		// ENRICHED forever with nothing to move it along.
		RequiredAcks: kafka.RequireAll,
	}
	defer writer.Close()

	go serveAdmin(cfg.ListenAddr, pool)

	go func() {
		sig := make(chan os.Signal, 1)
		signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
		<-sig
		log.Println("shutdown signal received")
		cancel()
	}()

	log.Printf("moderation service started: reject>=%.2f review>=%.2f k=%d",
		cfg.Thresholds.DuplicateReject, cfg.Thresholds.DuplicateReview, cfg.NeighbourK)

	for {
		msg, err := reader.FetchMessage(ctx)
		if err != nil {
			if errors.Is(err, context.Canceled) {
				log.Println("stopped")
				return
			}
			log.Printf("fetch: %v", err)
			continue
		}

		if err := handle(ctx, cfg, finder, writer, msg.Value); err != nil {
			log.Printf("handling listing failed: %v", err)
		}

		if err := reader.CommitMessages(ctx, msg); err != nil && !errors.Is(err, context.Canceled) {
			log.Printf("commit: %v", err)
		}
	}
}

func handle(ctx context.Context, cfg config.Config, finder *dedup.Finder,
	writer *kafka.Writer, raw []byte) error {

	started := time.Now()

	var in events.ListingEnriched
	if err := json.Unmarshal(raw, &in); err != nil {
		return err
	}

	// Both the seller's original text and the model's rewrite are checked. The
	// rewrite alone is not enough: enrichment strips promotional claims, so a
	// violation can vanish from the normalised title while still being what the
	// seller wrote and what a reviewer needs to see.
	violations := policy.Evaluate(in.RawTitle, in.RawDescription, in.NormalizedTitle, in.Description)

	var nearestID int64
	var nearestSeller string
	var nearestSim float32
	var nearestCategory string
	var nearestAttrs map[string]any
	if len(in.Embedding) > 0 {
		matches, err := finder.Nearest(ctx, in.ListingID, in.Embedding, cfg.NeighbourK)
		if err != nil {
			// A failed similarity query must not publish the listing by
			// default. Leaving the fields zero means the decision records
			// "duplicate detection did not run" and routes to a human.
			log.Printf("dedup query failed for listing %d: %v", in.ListingID, err)
		} else if len(matches) > 0 {
			nearestID = matches[0].ListingID
			nearestSeller = matches[0].SellerID
			nearestSim = matches[0].Similarity
			nearestCategory = matches[0].Category
			nearestAttrs = matches[0].Attributes
		}
	}

	decision := policy.Decide(policy.Input{
		Category:           in.Category,
		ExtractionProblems: in.ExtractionProblems,
		Violations:         violations,
		HasEmbedding:       len(in.Embedding) > 0,
		NearestID:          nearestID,
		NearestSeller:      nearestSeller,
		NearestSimilarity:  nearestSim,
		NearestCategory:    nearestCategory,
		NearestAttributes:  nearestAttrs,
		Attributes:         in.Attributes,
		SellerID:           in.SellerID,
	}, cfg.Thresholds)

	duration.Observe(time.Since(started).Seconds())
	decisions.WithLabelValues(decision.Verdict).Inc()
	for _, r := range decision.Reasons {
		reasonsFired.WithLabelValues(codeOf(r)).Inc()
	}

	out := events.ListingModerated{
		ListingID:      in.ListingID,
		Decision:       decision.Verdict,
		Reasons:        decision.Reasons,
		DuplicateOf:    decision.DuplicateOf,
		DuplicateScore: decision.DuplicateScore,
		Confidence:     decision.Confidence,
	}
	payload, err := json.Marshal(out)
	if err != nil {
		return err
	}

	log.Printf("listing %d -> %s (%d reasons, nearest=%d sim=%.3f)",
		in.ListingID, decision.Verdict, len(decision.Reasons), nearestID, nearestSim)

	return writer.WriteMessages(ctx, kafka.Message{
		Topic: events.TopicModerated,
		Key:   []byte(itoa(in.ListingID)),
		Value: payload,
	})
}

// codeOf extracts the leading rule code from a reason string, so the metric
// has bounded cardinality instead of one label value per unique message.
func codeOf(reason string) string {
	for i := 0; i < len(reason); i++ {
		if reason[i] == ':' {
			return reason[:i]
		}
	}
	return "OTHER"
}

func itoa(n int64) string {
	if n == 0 {
		return "0"
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}

func serveAdmin(addr string, pool *pgxpool.Pool) {
	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.Handler())
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		if err := pool.Ping(ctx); err != nil {
			http.Error(w, `{"status":"down"}`, http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"ok"}`))
	})
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Printf("admin server: %v", err)
	}
}
