## 2023-10-25 - Security Validation in DB Functions
**Learning:** Adding validation at the data layer ensures defense in depth. However, it's critical to ensure upstream callers properly handle new exceptions thrown by this layer.
**Action:** When adding validation and throwing errors (like ValueError) in low level db methods, trace back caller functions (e.g. `_process_add_wallet`) and add `try...except` to prevent crashes.
