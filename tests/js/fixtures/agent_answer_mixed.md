### Fleet check

| Host | Disk | State | Note |
|:---|---:|:---:|---|
| **ardi** | `/data` 72% | ✅ ok | ℹ️ growing ~1 GB/day |
| vader | `/` 91% | ⚠️ **full soon** | run `docker system df` |
| облак | — | ❌ offline | от вчера |

- **Docker**: 3 dangling images
  - **lab1**: `web`, `worker`
  - lab2: *nothing*
- Inline `code` and **bold** in one line

---

```bash
docker image prune -f
echo "<done>"
```

Done.
