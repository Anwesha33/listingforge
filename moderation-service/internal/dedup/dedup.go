// Package dedup finds listings that are near-duplicates of something already
// in the catalogue.
package dedup

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/jackc/pgx/v5/pgxpool"
)

// Match is a candidate duplicate.
type Match struct {
	ListingID  int64
	SellerID   string
	Title      string
	Similarity float32

	// Category and Attributes are carried so that the decision can require
	// structural agreement as well as vector similarity. A threshold sweep on
	// real pipeline output showed the two classes overlap: the highest-scoring
	// non-duplicate pair scored 0.945 while a true duplicate scored 0.879, so
	// no single similarity cutoff separates them. Embeddings capture "same kind
	// of product" far more strongly than "same product".
	Category   string
	Attributes map[string]any
}

// Finder queries the vector index.
type Finder struct {
	pool *pgxpool.Pool
}

func New(pool *pgxpool.Pool) *Finder { return &Finder{pool: pool} }

// Nearest returns the most similar existing listings to the given embedding.
//
// Similarity is cosine, computed as 1 - cosine_distance. The query excludes the
// listing being checked, and excludes listings the catalogue has already
// rejected: a seller should not be blocked as a duplicate of something that was
// itself refused.
func (f *Finder) Nearest(ctx context.Context, listingID int64, embedding []float32, limit int) ([]Match, error) {
	if len(embedding) == 0 {
		return nil, nil
	}

	const query = `
		SELECT l.id, l.seller_id, COALESCE(l.normalized_title, l.raw_title),
		       1 - (e.embedding <=> $1::vector) AS similarity,
		       COALESCE(l.category, ''), l.attributes
		  FROM listing_embeddings e
		  JOIN listings l ON l.id = e.listing_id
		 WHERE e.listing_id <> $2
		   AND l.status <> 'REJECTED'
		 ORDER BY e.embedding <=> $1::vector
		 LIMIT $3`

	rows, err := f.pool.Query(ctx, query, Literal(embedding), listingID, limit)
	if err != nil {
		return nil, fmt.Errorf("nearest-neighbour query: %w", err)
	}
	defer rows.Close()

	var out []Match
	for rows.Next() {
		var m Match
		var attrs []byte
		if err := rows.Scan(&m.ListingID, &m.SellerID, &m.Title, &m.Similarity, &m.Category, &attrs); err != nil {
			return nil, fmt.Errorf("scan: %w", err)
		}
		if len(attrs) > 0 {
			// A listing with unreadable attributes is still a usable candidate;
			// it simply contributes no structural agreement.
			_ = json.Unmarshal(attrs, &m.Attributes)
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

// Literal renders an embedding in pgvector's text form, "[0.1,0.2,...]".
//
// pgvector accepts this and casts it server-side, which avoids needing a
// custom pgx type for one column.
func Literal(embedding []float32) string {
	var b strings.Builder
	b.WriteByte('[')
	for i, v := range embedding {
		if i > 0 {
			b.WriteByte(',')
		}
		fmt.Fprintf(&b, "%.6f", v)
	}
	b.WriteByte(']')
	return b.String()
}
