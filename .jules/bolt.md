## 2024-05-15 - [Missing DB Indexes in SQLite init]
**Learning:** Found that local SQLite databases often miss indexes on frequently queried fields like `user_id` on the `alerts` table and `enabled` on the `digest_prefs` table, causing O(N) full table scans over time.
**Action:** Always check schema definitions for missing indexes on fields used in background loops or per-user lookups.
