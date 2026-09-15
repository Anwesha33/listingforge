JAVA17 ?= /Library/Java/JavaVirtualMachines/zulu-17.jdk/Contents/Home
MVN    := mvn -s deploy/maven-settings.xml

.PHONY: up down topics build run-catalog run-enrichment run-moderation benchmark sweep test clean reset

## Infrastructure -------------------------------------------------------------
up:
	docker compose up -d

down:
	docker compose down

topics:
	docker exec listingforge-redpanda-1 rpk topic create \
	  listing.created listing.enrichment-result listing.enriched listing.moderated listing.dlq \
	  -p 3 -r 1

## Build ----------------------------------------------------------------------
build:
	cd catalog-api && JAVA_HOME=$(JAVA17) $(MVN) -q -DskipTests package
	cd moderation-service && go build -o bin/moderation ./cmd/moderation
	cd enrichment-worker && python3 -m venv .venv && ./.venv/bin/pip install -q -r requirements.txt

## Run ------------------------------------------------------------------------
run-catalog:
	$(JAVA17)/bin/java -jar catalog-api/target/catalog-api-1.0.0.jar

run-enrichment:
	cd enrichment-worker && PYTHONPATH=src ./.venv/bin/python -m enrichment

run-moderation:
	cd moderation-service && ./bin/moderation

## Measure --------------------------------------------------------------------
benchmark:
	python3 benchmark/run.py --json benchmark/results.json

sweep:
	python3 benchmark/sweep_thresholds.py --json benchmark/threshold-sweep.json

test:
	cd moderation-service && go test ./... -count=1
	cd catalog-api && JAVA_HOME=$(JAVA17) $(MVN) -q test

## Reset ----------------------------------------------------------------------
# Wipes all pipeline state so a benchmark run starts from a known baseline.
reset:
	docker exec listingforge-postgres-1 psql -U listingforge -d listingforge \
	  -c "TRUNCATE listing_events, listing_embeddings, listings RESTART IDENTITY CASCADE;"
	for t in listing.created listing.enrichment-result listing.enriched listing.moderated listing.dlq; do \
	  docker exec listingforge-redpanda-1 rpk topic delete $$t >/dev/null 2>&1 || true; done
	$(MAKE) topics

clean:
	rm -rf catalog-api/target moderation-service/bin enrichment-worker/.venv
