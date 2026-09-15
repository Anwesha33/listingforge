package policy

import (
	"fmt"
	"strings"
)

// Thresholds control the automated decision. They are the tuning surface of
// the whole service, and both have a real business cost on either side:
// lowering DuplicateReject wrongly blocks honest sellers, raising it lets the
// catalogue fill with near-identical listings.
type Thresholds struct {
	// DuplicateReject: at or above this cosine similarity the listing is
	// treated as the same product and refused.
	DuplicateReject float32

	// DuplicateReview: at or above this, a human decides.
	DuplicateReview float32

	// MaxExtractionProblems: how many missing or invalid attributes are
	// tolerated before a human is asked to look.
	MaxExtractionProblems int

	// MinAttributeAgreement is the share of shared attribute keys that must
	// hold the same value before two similar listings are treated as the same
	// product rather than merely the same kind of product. Similarity alone is
	// not separable -- see the threshold sweep in benchmark/.
	MinAttributeAgreement float64
}

// DefaultThresholds are the values used in the benchmark. The duplicate
// numbers come from the threshold sweep documented in the README, not from
// intuition.
func DefaultThresholds() Thresholds {
	return Thresholds{
		// Measured, not guessed. The sweep placed the review boundary just
		// below the lowest observed true-duplicate similarity so that no known
		// duplicate escapes a human, and the reject boundary high enough that
		// an automatic refusal needs strong evidence.
		DuplicateReject:       0.93,
		DuplicateReview:       0.86,
		MaxExtractionProblems: 1,
		MinAttributeAgreement: 0.6,
	}
}

// Input is everything the decision depends on.
type Input struct {
	Category           string
	ExtractionProblems []string
	Violations         []Violation
	HasEmbedding       bool
	NearestID          int64
	NearestSeller      string
	NearestSimilarity  float32
	NearestCategory    string
	NearestAttributes  map[string]any
	Attributes         map[string]any
	SellerID           string
}

// Decision is the automated verdict.
type Decision struct {
	Verdict        string
	Reasons        []string
	DuplicateOf    *int64
	DuplicateScore *float32
	Confidence     float64
}

// Decide produces the verdict.
//
// The ordering is deliberate: a prohibited item is refused even if it is also a
// duplicate, because the reason the seller is shown should be the most
// important one. Everything else accumulates into a review decision rather than
// short-circuiting, so a reviewer sees the full picture at once.
func Decide(in Input, t Thresholds) Decision {
	var reasons []string

	if sev, any := WorstSeverity(in.Violations); any && sev == Reject {
		return Decision{
			Verdict:    "REJECT",
			Reasons:    Describe(in.Violations),
			Confidence: 0.99,
		}
	}
	reasons = append(reasons, Describe(in.Violations)...)

	var dupID *int64
	var dupScore *float32

	// Structural agreement gates the duplicate check. Two listings can be
	// highly similar in embedding space while being different products -- a
	// blue cotton kurti and a red cotton kurti sit close together because the
	// embedding is dominated by product type. Requiring the same category and
	// broadly the same attribute values is what distinguishes "same product"
	// from "same kind of product".
	sameCategory := in.NearestCategory != "" && in.NearestCategory == in.Category
	agreement := attributeAgreement(in.Attributes, in.NearestAttributes)
	structurallySame := sameCategory && agreement >= t.MinAttributeAgreement

	if in.NearestID != 0 && in.NearestSimilarity >= t.DuplicateReview && structurallySame {
		id, score := in.NearestID, in.NearestSimilarity
		dupID, dupScore = &id, &score

		// A seller re-listing their own product is a different problem from two
		// sellers colliding: the former is usually a mistake worth refusing
		// outright, the latter is legitimate competition on the same product.
		sameSeller := in.SellerID != "" && in.SellerID == in.NearestSeller

		switch {
		case score >= t.DuplicateReject && sameSeller:
			return Decision{
				Verdict: "REJECT",
				Reasons: append(reasons, fmt.Sprintf(
					"DUPLICATE_SELF: this is %.1f%% similar to your existing listing #%d", score*100, id)),
				DuplicateOf:    dupID,
				DuplicateScore: dupScore,
				Confidence:     float64(score),
			}
		case score >= t.DuplicateReject:
			reasons = append(reasons, fmt.Sprintf(
				"DUPLICATE_CATALOGUE: %.1f%% similar to existing listing #%d from another seller", score*100, id))
		default:
			reasons = append(reasons, fmt.Sprintf(
				"POSSIBLE_DUPLICATE: %.1f%% similar to listing #%d", score*100, id))
		}
	}

	if in.Category == "" || in.Category == "unknown" {
		reasons = append(reasons, "UNKNOWN_CATEGORY: the listing could not be assigned to a catalogue category")
	}
	if n := len(in.ExtractionProblems); n > t.MaxExtractionProblems {
		reasons = append(reasons, fmt.Sprintf(
			"INCOMPLETE_ATTRIBUTES: %d attribute problems (%v)", n, in.ExtractionProblems))
	}
	if !in.HasEmbedding {
		// Absence of an embedding means duplicate detection did not run. That
		// is reported rather than silently treated as "no duplicate found",
		// which would let every listing through whenever embedding is down.
		reasons = append(reasons, "NO_EMBEDDING: duplicate detection could not run for this listing")
	}

	if len(reasons) > 0 {
		return Decision{
			Verdict:        "REVIEW",
			Reasons:        reasons,
			DuplicateOf:    dupID,
			DuplicateScore: dupScore,
			Confidence:     0.5,
		}
	}

	return Decision{
		Verdict:        "PUBLISH",
		Reasons:        []string{},
		DuplicateOf:    dupID,
		DuplicateScore: dupScore,
		Confidence:     0.95,
	}
}

// attributeAgreement is the share of attribute keys present in both listings
// that carry the same value.
//
// Returns 1.0 when the two share no keys at all: absence of disagreement is not
// evidence of difference, and letting an empty overlap veto a 0.95-similarity
// match would reintroduce the false negatives the gate exists to avoid. The
// similarity threshold is still doing the primary work.
func attributeAgreement(a, b map[string]any) float64 {
	if len(a) == 0 || len(b) == 0 {
		return 1.0
	}
	shared, same := 0, 0
	for key, av := range a {
		bv, ok := b[key]
		if !ok {
			continue
		}
		shared++
		if strings.EqualFold(fmt.Sprint(av), fmt.Sprint(bv)) {
			same++
		}
	}
	if shared == 0 {
		return 1.0
	}
	return float64(same) / float64(shared)
}
