## 2026-08-14 - Batch API calls to avoid N+1
**Learning:** When an API accepts a comma-separated list of IDs in a query parameter, use it to batch requests instead of making a separate request for each ID.
**Action:** Always look for opportunities to batch network requests to reduce overhead and latency.
