# Software engineering roadmap

Each `##` is a series and each numbered line is one card.

## big-o: Speed, intuitively
1. Big-O as "how it grows", not "how fast"
2. O(1), O(n), O(n^2) with real examples
3. Why binary search is O(log n)
4. Hash maps: O(1) on average, and the catch
5. Sorting: why O(n log n) is the floor for comparisons
6. Space complexity and the time/memory trade

## data-structures: The handful you actually use
1. Arrays vs. linked lists in memory
2. Stacks and queues
3. Trees and binary search trees
4. Heaps and priority queues
5. Graphs: BFS vs. DFS
6. Tries for autocomplete

## systems: What your code runs on
1. Processes vs. threads
2. The stack, the heap, and where variables live
3. System calls: strace a hello world
4. File descriptors: everything is a file
5. Concurrency bugs: race conditions
6. Async I/O and the event loop

## databases: Where data lives
1. What an index really is (B-trees)
2. Transactions and ACID
3. Isolation levels and the anomalies they prevent
4. EXPLAIN: reading a query plan
5. N+1 queries
6. SQLite WAL mode and why it's fast

## git: Beyond add, commit, push
1. Commits as snapshots, not diffs
2. Branches are just pointers
3. Merge vs. rebase
4. The reflog: undo almost anything
5. git bisect to find the commit that broke it
