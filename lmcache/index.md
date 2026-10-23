# LMCache L1 P2P report

- Run: 39905593407
- Date: 2026-10-23 06:41 UTC
- RDMA bytes moved: 16777216 (expected 16777216)
- Generated: **2026-10-23 06:41 UTC** &middot; Commit: `c151650` &middot; Status: **pass**

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
  [seq 600/cold] LOOKUP: 2/2 chunks hit (64.7 ms)
  [seq 600/cold] RETRIEVE: retrieved (512 tokens, 2846.6 ms, 1 workers)
  [seq 601/cold] LOOKUP: 2/2 chunks hit (77.8 ms)
  [seq 601/cold] RETRIEVE: retrieved (512 tokens, 2007.9 ms, 1 workers)
```
