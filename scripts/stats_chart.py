#!/usr/bin/env python3
"""Render a GitHub-usage transformation chart from the `stats` branch history.

Reads the commit log of the `stats` branch (each commit message is
"stats: <clones> clones / <uniques> unique (14d)") and draws a dark-mode
line chart of total clones + unique cloners over time, with the peak and
the largest day-over-day drop annotated. Output: a single self-contained
SVG, ready to commit to the branch and embed in the README.

Usage:  python3 stats_chart.py --repo <owner/name> --token <gh-token> --out usage.svg
"""
import argparse, json, re, sys, urllib.request

def fetch_commits(repo, token, per_page=100):
    """Return [(date, clones, uniques)] oldest-first from the stats branch."""
    rows = []
    page = 1
    pat = re.compile(r"stats:\s*(\d+)\s*clones\s*/\s*(\d+)\s*unique")
    while True:
        url = (f"https://api.github.com/repos/{repo}/commits"
               f"?sha=stats&per_page={per_page}&page={page}")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                                   "Accept": "application/vnd.github+json",
                                                   "User-Agent": "stats-chart"})
        with urllib.request.urlopen(req) as r:
            batch = json.load(r)
        if not batch:
            break
        for c in batch:
            m = pat.search(c["commit"]["message"])
            if m:
                rows.append((c["commit"]["committer"]["date"][:10],
                             int(m.group(1)), int(m.group(2))))
        if len(batch) < per_page:
            break
        page += 1
    rows.sort(key=lambda x: x[0])
    # de-dup by date, keep last occurrence
    seen = {}
    for d, c, u in rows:
        seen[d] = (c, u)
    return [(d, c, u) for d, (c, u) in sorted(seen.items())]

def render_svg(rows, title="HomeLab Monitor — GitHub usage transformation"):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from datetime import datetime

    dates  = [mdates.date2num(datetime.strptime(d, "%Y-%m-%d").date()) for d, _, _ in rows]
    clones = [c for _, c, _ in rows]
    unique = [u for _, _, u in rows]

    fig, ax = plt.subplots(figsize=(11, 5.2), dpi=130)
    fig.patch.set_facecolor("#0d1117"); ax.set_facecolor("#0d1117")
    ax.plot(dates, unique, color="#58a6ff", lw=2.2, marker="o", ms=3,
            label="Unique cloners (14d window)")
    ax.plot(dates, clones, color="#3fb950", lw=1.4, alpha=0.55,
            label="Total clones (14d window)")
    ax.set_title(title, color="#e6edf3", fontsize=13, loc="left")
    ax.tick_params(colors="#8b949e")
    for s in ax.spines.values():
        s.set_color("#30363d")
    ax.grid(color="#21262d", lw=0.6, alpha=0.7)
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.set_ylabel("people / clones (rolling 14d)", color="#8b949e")
    ax.legend(facecolor="#161b22", edgecolor="#30363d", labelcolor="#e6edf3", fontsize=9)

    # annotate the peak
    i = clones.index(max(clones))
    ax.annotate(f"peak {max(clones):,} clones\n{unique[i]:,} unique",
                xy=(dates[i], clones[i]), xytext=(12, -34), color="#e6edf3",
                fontsize=8, arrowprops=dict(color="#8b949e", arrowstyle="->"))
    # annotate the largest day-over-day drop in uniques
    worst, worst_i = 0, -1
    for k in range(1, len(unique)):
        drop = unique[k-1] - unique[k]
        if drop > worst:
            worst, worst_i = drop, k
    if worst_i > 0:
        ax.annotate(f"−{worst:,} in a day\n({rows[worst_i][0]})",
                    xy=(dates[worst_i], unique[worst_i]), xytext=(-60, 26),
                    color="#f85149", fontsize=8,
                    arrowprops=dict(color="#f85149", arrowstyle="->"))

    plt.tight_layout()
    return fig

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="HomeLab Monitor — GitHub usage transformation")
    a = ap.parse_args()
    rows = fetch_commits(a.repo, a.token)
    if len(rows) < 2:
        print("not enough history to chart", file=sys.stderr)
        sys.exit(1)
    fig = render_svg(rows, a.title)
    fig.savefig(a.out)
    print(f"chart: {len(rows)} points, {rows[0][0]} → {rows[-1][0]}, "
          f"peak {max(c for _,c,_ in rows):,} clones, "
          f"last {rows[-1][2]} unique")

if __name__ == "__main__":
    main()
