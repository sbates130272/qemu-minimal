# LMCache L1 P2P report

- Run: 40942710843
- Date: 2026-10-29 20:16 UTC
- RDMA bytes moved: 16777216 (expected 16777216)
- Generated: **2026-10-29 20:16 UTC** &middot; Commit: `15a9b83` &middot; Status: **pass**

## Guest device state
```
ssh: connect to host 172.31.3.1 port 22: Connection refused
```

## ernic counters
```
Error response from daemon: No such container: lmcache-ci-ernic-stats-exporter-1
```

## Pull output
```
  [seq 600/cold] LOOKUP: 2/2 chunks hit (131.4 ms)
  [seq 600/cold] RETRIEVE: retrieved (512 tokens, 3736.4 ms, 1 workers)
  [seq 601/cold] LOOKUP: 2/2 chunks hit (132.2 ms)
  [seq 601/cold] RETRIEVE: retrieved (512 tokens, 3480.2 ms, 1 workers)
```
