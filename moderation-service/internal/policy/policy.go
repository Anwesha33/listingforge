// Package policy applies deterministic content rules to an enriched listing.
//
// There is deliberately no model in this path. Two reasons: a moderation
// decision has to be explainable to the seller whose listing was refused, and
// "the classifier said so" is not an explanation; and policy runs on every
// listing, so a rule that takes microseconds instead of a second is what keeps
// the pipeline's cost dominated by enrichment rather than by moderation.
package policy

import (
	"fmt"
	"regexp"
	"strings"
)

// Severity determines how a violation affects the final decision.
type Severity int

const (
	// Review sends the listing to a human.
	Review Severity = iota
	// Reject refuses it outright, with no human in the loop.
	Reject
)

// Rule is one content check.
type Rule struct {
	Code     string
	Severity Severity
	// Explanation is written for the seller, not for an engineer.
	Explanation string
	pattern     *regexp.Regexp
}

// Violation is a rule that fired.
type Violation struct {
	Code        string   `json:"code"`
	Severity    Severity `json:"severity"`
	Explanation string   `json:"explanation"`
	Matched     string   `json:"matched"`
}

// rules are evaluated in order; all matches are collected rather than stopping
// at the first, because a seller fixing one problem at a time is a bad
// experience and a needless extra round trip.
var rules = []Rule{
	{
		Code:        "PROHIBITED_ITEM",
		Severity:    Reject,
		Explanation: "Listings for this type of product are not allowed on the marketplace.",
		pattern:     regexp.MustCompile(`(?i)\b(pistol|firearm|ammunition|pepper spray|switchblade|narcotic|cocaine|cannabis|mdma|steroid|human hair extension from temple)\b`),
	},
	{
		Code:        "COUNTERFEIT_SIGNAL",
		Severity:    Reject,
		Explanation: "Listings offering replica or first-copy branded goods are not allowed.",
		pattern:     regexp.MustCompile(`(?i)\b(first copy|1st copy|replica|master copy|7a quality|aaa copy|duplicate of (nike|adidas|gucci|rolex))\b`),
	},
	{
		Code:        "MEDICAL_CLAIM",
		Severity:    Reject,
		Explanation: "Products may not claim to cure, treat or prevent any medical condition.",
		pattern:     regexp.MustCompile(`(?i)\b(cures?|treats?|prevents?)\s+(cancer|diabetes|covid|asthma|infertility|baldness)\b|\bmiracle (cure|remedy)\b`),
	},
	{
		Code:        "UNSUPPORTED_GUARANTEE",
		Severity:    Review,
		Explanation: "Absolute guarantees such as \"100% guaranteed\" need evidence before they can be published.",
		// "colou?r" rather than "colour?": the latter matches "colou" and
		// "colour" but not the American spelling, which is what sellers write.
		// An optional intervening word lets "100% color guarantee" match as
		// well as "100% guaranteed".
		pattern: regexp.MustCompile(`(?i)\b(100\s*%\s*(\w+\s+)?(guarantee[d]?|original|genuine|colou?r\s*fast(ness)?)|lifetime guarantee|money.?back guarantee|unbreakable)\b`),
	},
	{
		Code:        "SUPERLATIVE_CLAIM",
		Severity:    Review,
		Explanation: "Unverifiable superlatives such as \"best in market\" need review before publishing.",
		pattern:     regexp.MustCompile(`(?i)\b(best (in|on) (the )?(market|india|world)|number one brand|world'?s best|highest quality guaranteed)\b`),
	},
	{
		Code:        "CONTACT_DETAILS",
		Severity:    Review,
		Explanation: "Listings may not contain phone numbers, email addresses or links that take buyers off the platform.",
		pattern:     regexp.MustCompile(`(?i)(\b[6-9]\d{9}\b|\b[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}\b|\bwa\.me/|\bwhats?app\b.{0,12}\d{6,})`),
	},
}

// Evaluate runs every rule against the listing's text.
//
// Both the seller's original text and the model's normalized output are
// checked. Checking only the normalized text would be a hole: the enrichment
// prompt asks the model to strip promotional noise, so a banned phrase can
// disappear from the normalized title while still being what the seller
// actually wrote and what a reviewer needs to see.
func Evaluate(texts ...string) []Violation {
	combined := strings.Join(texts, "\n")
	var out []Violation

	for _, rule := range rules {
		if m := rule.pattern.FindString(combined); m != "" {
			out = append(out, Violation{
				Code:        rule.Code,
				Severity:    rule.Severity,
				Explanation: rule.Explanation,
				Matched:     strings.TrimSpace(m),
			})
		}
	}
	return out
}

// WorstSeverity reports the most severe outcome among the violations.
func WorstSeverity(vs []Violation) (Severity, bool) {
	worst, found := Review, false
	for _, v := range vs {
		found = true
		if v.Severity > worst {
			worst = v.Severity
		}
	}
	return worst, found
}

// Describe renders violations as seller-facing reasons.
func Describe(vs []Violation) []string {
	out := make([]string, 0, len(vs))
	for _, v := range vs {
		out = append(out, fmt.Sprintf("%s: %s (matched %q)", v.Code, v.Explanation, v.Matched))
	}
	return out
}

// RuleCodes lists every rule, used by the benchmark report.
func RuleCodes() []string {
	out := make([]string, 0, len(rules))
	for _, r := range rules {
		out = append(out, r.Code)
	}
	return out
}
