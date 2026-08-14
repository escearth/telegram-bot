## 2026-08-14 - Use ThreadPoolExecutor for Sequential API Fetching
**Learning:** Sequential network I/O operations can be drastically sped up by running them concurrently using `concurrent.futures.ThreadPoolExecutor`. Mutating shared state like dictionaries should be done in the main thread (using `.result()`) instead of inside the thread worker to avoid race conditions.
**Action:** When seeing sequential blocking loops over network or IO operations, evaluate using `ThreadPoolExecutor.map` or `as_completed` for performance gains, and verify thread-safety on shared variables.
