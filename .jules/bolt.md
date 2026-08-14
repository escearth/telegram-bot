## 2023-10-27 - [Dictionary `.get()` lookup vs `not in`]
**Learning:** [Replacing `if key not in dict` followed by `dict[key]` lookup with `dict.get()` significantly speeds up caching operations under high load because it avoids redundant hash calculations and memory access, particularly inside locks.]
**Action:** [Always use `.get()` to conditionally retrieve a dictionary value instead of first checking for existence with `not in` and then indexing.]

## 2023-10-27 - [Lazy list comprehension optimization in Rate Limiting]
**Learning:** [Pre-filtering lists on every request (via list comprehensions) when evaluating constraints (like rate limits) creates unnecessary memory churn and overhead. Checking bounding conditions (`len >= limit`) before allocating and calculating new lists dramatically enhances regular path efficiency.]
**Action:** [Skip memory-allocating list filtering steps if initial bounding condition checks show the limit is far from being met.]
