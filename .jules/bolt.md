## 2024-05-24 - Performance Testing and Caching
**Learning:** Avoid duplicate data retrieval by properly using sets (`set()`) rather than lists when aggregating identifiers for batch fetching.
**Action:** When aggregating keys (e.g., CIDs) for batch operations in python, use a `set` to guarantee uniqueness and prevent redundant data transmission and processing.
