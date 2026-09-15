package policy

import "testing"

func TestRejectRulesFire(t *testing.T) {
	cases := []struct {
		name string
		text string
		code string
	}{
		{"medical claim", "herbal oil cures diabetes permanently", "MEDICAL_CLAIM"},
		{"miracle remedy", "ayurvedic miracle cure for hair", "MEDICAL_CLAIM"},
		{"prohibited item", "pepper spray for self defence", "PROHIBITED_ITEM"},
		{"counterfeit", "Rolex replica watch first copy master quality", "COUNTERFEIT_SIGNAL"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			vs := Evaluate(tc.text)
			if len(vs) == 0 {
				t.Fatalf("expected a violation for %q", tc.text)
			}
			found := false
			for _, v := range vs {
				if v.Code == tc.code {
					found = true
					if v.Severity != Reject {
						t.Errorf("%s should be a reject-severity rule", tc.code)
					}
				}
			}
			if !found {
				t.Errorf("expected %s, got %v", tc.code, vs)
			}
		})
	}
}

func TestReviewRulesFire(t *testing.T) {
	cases := []struct{ name, text, code string }{
		{"guarantee", "cotton saree 100% color guarantee", "UNSUPPORTED_GUARANTEE"},
		{"superlative", "best in market quality kurti", "SUPERLATIVE_CLAIM"},
		{"phone number", "contact 9876543210 for bulk order", "CONTACT_DETAILS"},
		{"email", "mail us at seller@example.com for rates", "CONTACT_DETAILS"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			for _, v := range Evaluate(tc.text) {
				if v.Code == tc.code {
					if v.Severity != Review {
						t.Errorf("%s should be review severity", tc.code)
					}
					return
				}
			}
			t.Errorf("expected %s for %q", tc.code, tc.text)
		})
	}
}

// TestCleanListingsPassUntouched guards the failure mode that actually hurts:
// over-blocking honest sellers.
func TestCleanListingsPassUntouched(t *testing.T) {
	clean := []string{
		"Cotton Saree Combo Pack of 2",
		"Men Slim Fit Blue Jeans Denim",
		"Steel Water Bottle 1000ml Pack of 2",
		"Gold Plated Jhumka Earrings Brass",
		"Wooden Wall Shelf Brown Set of 3",
	}
	for _, text := range clean {
		if vs := Evaluate(text); len(vs) != 0 {
			t.Errorf("clean listing %q was flagged: %v", text, vs)
		}
	}
}

func TestRawTextIsCheckedNotJustNormalised(t *testing.T) {
	// The enrichment model strips policy-violating language, so moderation is
	// given the seller's original text as well. This pins that behaviour.
	raw := "Herbal oil cures diabetes miracle remedy 100% guaranteed"
	normalised := "Herbal oil remedy"

	if vs := Evaluate(normalised); len(vs) != 0 {
		t.Fatalf("precondition failed: normalised text should look clean, got %v", vs)
	}
	vs := Evaluate(raw, normalised)
	if sev, any := WorstSeverity(vs); !any || sev != Reject {
		t.Errorf("expected a reject when raw text is included, got %v", vs)
	}
}

func TestDecideRejectsProhibitedAheadOfEverythingElse(t *testing.T) {
	d := Decide(Input{
		Category:   "unknown",
		Violations: Evaluate("pepper spray self defence"),
	}, DefaultThresholds())

	if d.Verdict != "REJECT" {
		t.Fatalf("expected REJECT, got %s", d.Verdict)
	}
	if len(d.Reasons) != 1 {
		t.Errorf("a rejected listing should carry only the reason that caused it, got %v", d.Reasons)
	}
}

func TestDecidePublishesACleanListing(t *testing.T) {
	d := Decide(Input{
		Category:     "sarees",
		HasEmbedding: true,
		Attributes:   map[string]any{"fabric": "cotton", "color": "red"},
	}, DefaultThresholds())

	if d.Verdict != "PUBLISH" {
		t.Fatalf("expected PUBLISH, got %s with reasons %v", d.Verdict, d.Reasons)
	}
}

func TestMissingEmbeddingNeverSilentlyPublishes(t *testing.T) {
	// If duplicate detection could not run, the listing must not be treated as
	// unique. Otherwise an embedding outage publishes everything.
	d := Decide(Input{Category: "sarees", HasEmbedding: false}, DefaultThresholds())
	if d.Verdict != "REVIEW" {
		t.Errorf("expected REVIEW when no embedding exists, got %s", d.Verdict)
	}
}

func TestSimilarButStructurallyDifferentIsNotADuplicate(t *testing.T) {
	// A red kurti and a blue kurti sit close together in embedding space. The
	// attribute gate is what stops that from being called a duplicate.
	d := Decide(Input{
		Category:          "kurtis",
		HasEmbedding:      true,
		Attributes:        map[string]any{"color": "red", "fabric": "cotton", "size": "L"},
		NearestID:         42,
		NearestSimilarity: 0.94,
		NearestCategory:   "kurtis",
		NearestAttributes: map[string]any{"color": "blue", "fabric": "cotton", "size": "M"},
	}, DefaultThresholds())

	if d.Verdict != "PUBLISH" {
		t.Errorf("expected PUBLISH for a differently-attributed listing, got %s (%v)", d.Verdict, d.Reasons)
	}
}

func TestSameSellerSameProductIsRejected(t *testing.T) {
	attrs := map[string]any{"color": "red", "fabric": "rayon", "size": "L"}
	d := Decide(Input{
		Category:          "kurtis",
		HasEmbedding:      true,
		SellerID:          "S110",
		Attributes:        attrs,
		NearestID:         7,
		NearestSeller:     "S110",
		NearestSimilarity: 0.95,
		NearestCategory:   "kurtis",
		NearestAttributes: attrs,
	}, DefaultThresholds())

	if d.Verdict != "REJECT" {
		t.Errorf("a seller re-listing their own product should be rejected, got %s (%v)", d.Verdict, d.Reasons)
	}
}

func TestCrossSellerDuplicateGoesToReviewNotReject(t *testing.T) {
	// Two sellers legitimately compete on the same product; that is a human
	// decision, not an automatic refusal.
	attrs := map[string]any{"color": "red", "fabric": "rayon"}
	d := Decide(Input{
		Category:          "kurtis",
		HasEmbedding:      true,
		SellerID:          "S999",
		Attributes:        attrs,
		NearestID:         7,
		NearestSeller:     "S110",
		NearestSimilarity: 0.95,
		NearestCategory:   "kurtis",
		NearestAttributes: attrs,
	}, DefaultThresholds())

	if d.Verdict != "REVIEW" {
		t.Errorf("expected REVIEW for a cross-seller duplicate, got %s", d.Verdict)
	}
	if d.DuplicateOf == nil || *d.DuplicateOf != 7 {
		t.Errorf("expected the duplicate to be recorded, got %v", d.DuplicateOf)
	}
}

func TestAttributeAgreement(t *testing.T) {
	cases := []struct {
		name string
		a, b map[string]any
		want float64
	}{
		{"identical", map[string]any{"a": "1", "b": "2"}, map[string]any{"a": "1", "b": "2"}, 1.0},
		{"half", map[string]any{"a": "1", "b": "2"}, map[string]any{"a": "1", "b": "3"}, 0.5},
		{"none", map[string]any{"a": "1"}, map[string]any{"a": "9"}, 0.0},
		{"case insensitive", map[string]any{"a": "Cotton"}, map[string]any{"a": "cotton"}, 1.0},
		{"no shared keys", map[string]any{"a": "1"}, map[string]any{"z": "1"}, 1.0},
		{"empty", map[string]any{}, map[string]any{"a": "1"}, 1.0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := attributeAgreement(tc.a, tc.b); got != tc.want {
				t.Errorf("got %.2f, want %.2f", got, tc.want)
			}
		})
	}
}
