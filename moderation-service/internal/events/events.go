// Package events mirrors the Kafka payloads exchanged between the three
// services.
//
// The field names here are duplicated by hand from the Java records and the
// Python worker. That duplication is the price of not running a schema
// registry; it is acceptable at three services and would not be at thirty.
// Unknown fields are ignored by encoding/json by default, so a producer adding
// a field never breaks this consumer.
package events

const (
	TopicEnriched  = "listing.enriched"
	TopicModerated = "listing.moderated"
	TopicDLQ       = "listing.dlq"
)

// ListingEnriched is produced by the enrichment worker.
type ListingEnriched struct {
	ListingID int64  `json:"listingId"`
	SellerID  string `json:"sellerId"`

	// RawTitle and RawDescription are the seller's own words, before the
	// enrichment model rewrote them. Policy is evaluated against these as well
	// as the normalised text, because the rewrite removes exactly the
	// promotional and non-compliant language the rules look for.
	RawTitle       string `json:"rawTitle"`
	RawDescription string `json:"rawDescription"`

	NormalizedTitle    string         `json:"normalizedTitle"`
	Description        string         `json:"description"`
	Category           string         `json:"category"`
	Attributes         map[string]any `json:"attributes"`
	ExtractionProblems []string       `json:"extractionProblems"`
	Embedding          []float32      `json:"embedding"`
	EmbeddingModel     string         `json:"embeddingModel"`
	ExtractionModel    string         `json:"extractionModel"`
}

// Decision values carried by ListingModerated.
const (
	DecisionPublish = "PUBLISH"
	DecisionReview  = "REVIEW"
	DecisionReject  = "REJECT"
)

// ListingModerated is the automated verdict.
type ListingModerated struct {
	ListingID      int64    `json:"listingId"`
	Decision       string   `json:"decision"`
	Reasons        []string `json:"reasons"`
	DuplicateOf    *int64   `json:"duplicateOf"`
	DuplicateScore *float32 `json:"duplicateScore"`
	Confidence     float64  `json:"confidence"`
}
