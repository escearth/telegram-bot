## 2024-05-23 - Concurrent API requests optimization in WebApp wallets
**Learning:** Sequential network calls inside a loop can be extremely slow and O(N) bounded. By using Python's `concurrent.futures.ThreadPoolExecutor`, these can easily be optimized to happen concurrently with minimal code changes, retaining ordering logic by separating future fetching from list building.
**Action:** Always look for O(N) sequential API calls in loops and propose converting them to concurrent tasks utilizing thread pools to easily drop processing latency.
