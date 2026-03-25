## 2024-03-24 - [Optimize instance_manager.list_all O(n) to O(m)]
**Learning:** `InstanceManager.list_all()` was frequently called (21+ times in `server.py`) and did an O(N) iteration over all instances across all users just to retrieve instances for a single owner. As the total instance count across all users grows, this becomes a bottleneck, especially inside tight loops and message processing checks.
**Action:** Introduced an `_owner_to_ids` dictionary index to maintain an O(1) mapping of `owner_id` to a set of their `instance_id`s, reducing the single-owner query from O(N) over all instances to O(M) where M is the small subset of instances for that specific user.

## 2024-05-18 - [Optimize sequential await in loops to concurrent asyncio.gather]
**Learning:** Calling independent `await` statements inside a `for` loop across multiple network peers causes an O(n) blocking bottleneck on the handler, significantly slowing down features like `/collab broadcast` and `/bridgenet broadcast`.
**Action:** Always collect independent coroutines in a list and invoke them concurrently using `await asyncio.gather(*coroutines)` to change the total execution time from the sum of all calls to just the time of the slowest call.
