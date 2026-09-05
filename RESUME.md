# Hermes H.2 Resume

## Objective

Complete the Gmail Intelligence and Commitment Ingestion phase without
weakening the completed Calendar, gateway, or credential boundaries.

## Completed: COMPLETED (2026-09-05)

- The isolated Gmail service, read-only OAuth flow, Pub/Sub pull path, bounded
  baseline, incremental history sync, canonical store, and published search
  snapshot are deployed and operational.
- A production restart recovered from an index-write stall. Published-index
  writes are content-change-gated, and the Gmail service resumed normal
  incremental operation (1,215 messages, 1,088 threads, 0 failures).
- Published-index reader permissions validated (2750/0440 hermesreconcile,
  ro-mounted to container, write rejected with EROFS, sensitive state inaccessible).
- hermes-gmail-event-ingest timer/service operational (scanned 1,017 items to Event Layer).
- Reconcile projection (calendar-reconcile.db) operational; Calendar service links cleanly.
- Attention Engine probe_gmail verified (ok=True, watch_state=ACTIVE); proactive notifications operational.
- Existing services regression clean (Gateway, Discord Knowledge, Calendar, Attention, Hermes agent all active).
- 379 tests passing on Pi (tests-gateway 181, tests-attention 152, tests-gmail 31, tests-calendar 15).
- Test environment isolation fixed in tests-attention/test_engine.py.

## Next phase

- Next: Phase H.3 (or next scheduled capability / hardening)

