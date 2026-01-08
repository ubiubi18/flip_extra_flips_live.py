#!/usr/bin/env python3
"""
Live "extra flips" scan for the current epoch (before validation).

Goal:
- Count how many identities (authors) have published more than N flips (default N=3)
- Compute total extra flips beyond N (sum(max(0, count - N)))
- Optional: fetch stake for those authors (extra flips per stake), if you enable --fetch-stake

Endpoints used (Idena indexer API):
- GET /Epoch/Last
- GET /Epoch/{epoch}/Flips (paged)

No external dependencies (no requests). Uses only Python stdlib.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from collections import Counter
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL_DEFAULT = "https://api.idena.io/api"
MAX_PAGE_SIZE = 100


def utc_ts() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def log(msg: str) -> None:
    print(f"[{utc_ts()}] {msg}", flush=True)


class HttpClient:
    def __init__(self, base_url: str, timeout: int = 25, retries: int = 6, backoff_sec: float = 1.3):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.backoff_sec = backoff_sec

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        qs = ""
        if params:
            qs = "?" + urlencode(params)
        url = f"{self.base_url}{path}{qs}"

        last_err: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                req = Request(url, headers={"Accept": "application/json"})
                with urlopen(req, timeout=self.timeout) as resp:
                    status = getattr(resp, "status", 200)
                    if status == 429:
                        time.sleep(self.backoff_sec * attempt)
                        continue
                    body = resp.read().decode("utf-8", errors="replace")
                js = json.loads(body)

                err = js.get("error")
                if isinstance(err, dict) and err.get("message"):
                    raise RuntimeError(f"API error at {path}: {err.get('message')}")

                return js
            except Exception as e:
                last_err = e
                time.sleep(self.backoff_sec * attempt)

        raise RuntimeError(f"GET failed after {self.retries} retries: {url} ({last_err})")


def get_current_epoch(api: HttpClient) -> int:
    js = api.get_json("/Epoch/Last")
    res = js.get("result")
    if isinstance(res, int):
        return res
    if isinstance(res, dict):
        for k in ("epoch", "Epoch", "number", "Number"):
            if k in res and isinstance(res[k], int):
                return res[k]
    raise RuntimeError(f"Unexpected /Epoch/Last result: {res}")


def paged_flips(api: HttpClient, epoch: int, page_size: int, sleep_per_page: float) -> Iterable[Dict[str, Any]]:
    token: Optional[str] = None
    while True:
        params: Dict[str, Any] = {"limit": page_size}
        if token:
            params["continuationToken"] = token

        js = api.get_json(f"/Epoch/{epoch}/Flips", params=params)
        items = js.get("result") or []
        if not isinstance(items, list):
            raise RuntimeError(f"Unexpected result type at /Epoch/{epoch}/Flips: {type(items)}")

        for it in items:
            if isinstance(it, dict):
                yield it

        token = js.get("continuationToken")
        if not token:
            break

        if sleep_per_page > 0:
            time.sleep(sleep_per_page)


def safe_float(v: Any) -> float:
    try:
        if v is None:
            return 0.0
        return float(v)
    except Exception:
        return 0.0


def try_get_author(flip: Dict[str, Any]) -> str:
    # tolerant to schema changes
    for k in ("author", "authorAddress", "address"):
        a = (flip.get(k) or "")
        if isinstance(a, str) and a.startswith("0x") and len(a) == 42:
            return a.lower()
    return ""


def try_get_cid(flip: Dict[str, Any]) -> str:
    c = flip.get("cid") or flip.get("Cid") or ""
    return c if isinstance(c, str) else ""


def fetch_stake_for_address(api: HttpClient, address: str) -> Tuple[float, str]:
    """
    Best-effort stake fetch.
    If the indexer response schema changes, we keep it safe and return 0.0.
    """
    try:
        js = api.get_json(f"/Address/{address}")
        res = js.get("result") or {}
        # common possible keys
        for k in ("stake", "Stake", "stakeBalance", "stakeAmount"):
            if k in res:
                return safe_float(res.get(k)), ""
        # sometimes nested
        if isinstance(res.get("balance"), dict):
            b = res["balance"]
            for k in ("stake", "Stake"):
                if k in b:
                    return safe_float(b.get(k)), ""
        return 0.0, "stake_not_found"
    except Exception as e:
        return 0.0, f"stake_fetch_error:{e}"


def write_csv(path: str, header: List[str], rows: List[List[Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="Live count of identities with >N published flips in the current epoch.")
    ap.add_argument("--base-url", default=BASE_URL_DEFAULT, help="Idena API base URL (default: https://api.idena.io/api)")
    ap.add_argument("--epoch", type=int, default=0, help="Epoch number. If 0, uses current epoch from /Epoch/Last.")
    ap.add_argument("--threshold", type=int, default=3, help="Count identities with flipCount > threshold (default: 3).")
    ap.add_argument("--page-size", type=int, default=100, help="Pagination page size (limit=). Max is 100.")
    ap.add_argument("--sleep-per-page", type=float, default=0.1, help="Sleep seconds after each page fetch (default: 0.1).")
    ap.add_argument("--top", type=int, default=50, help="Print top N authors by flipCount (default: 50).")
    ap.add_argument("--out-dir", default="./out", help="Output directory (default: ./out).")
    ap.add_argument("--fetch-stake", action="store_true", help="Also query stake for authors with extra flips (slower).")
    args = ap.parse_args()

    if args.page_size < 1 or args.page_size > MAX_PAGE_SIZE:
        raise SystemExit(f"--page-size must be 1..{MAX_PAGE_SIZE}")

    api = HttpClient(base_url=args.base_url)

    epoch = args.epoch
    if epoch == 0:
        epoch = get_current_epoch(api)

    thr = int(args.threshold)

    log(f"Epoch: {epoch} (live)")
    log(f"Fetching flips and counting authors (threshold > {thr}) ...")

    counts: Counter[str] = Counter()
    seen = 0

    for fl in paged_flips(api, epoch=epoch, page_size=args.page_size, sleep_per_page=args.sleep_per_page):
        seen += 1
        author = try_get_author(fl)
        cid = try_get_cid(fl)
        if not author or not cid:
            continue
        counts[author] += 1
        if seen % 2000 == 0:
            log(f"processed flips: {seen} (unique authors so far: {len(counts)})")

    total_authors = len(counts)
    authors_gt = [a for a, c in counts.items() if c > thr]
    authors_gt_n = len(authors_gt)
    total_extra_flips = sum(max(0, counts[a] - thr) for a in authors_gt)

    log(f"Done. flips_seen={seen} unique_authors={total_authors}")
    log(f"authors_with_flipCount>{thr}: {authors_gt_n}")
    log(f"total_extra_flips_over_{thr}: {total_extra_flips}")

    # histogram (small)
    hist = Counter()
    for c in counts.values():
        if c >= 10:
            hist["10+"] += 1
        else:
            hist[str(c)] += 1
    log("flipCount histogram (authors): " + ", ".join([f"{k}={hist[k]}" for k in sorted(hist.keys(), key=lambda x: (999 if x == '10+' else int(x)))]))

    # sort authors by count desc
    ranked = sorted(((a, counts[a]) for a in counts), key=lambda x: (x[1], x[0]), reverse=True)

    topn = max(0, int(args.top))
    if topn:
        print("\n=== TOP AUTHORS by flipCount (live) ===")
        for i, (a, c) in enumerate(ranked[:topn], start=1):
            extra = max(0, c - thr)
            mark = "  EXTRA" if extra > 0 else ""
            print(f"{i:4d}  flips={c:2d}  extra={extra:2d}  {a}{mark}")

    # CSV for authors with > threshold
    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, f"live_extra_flips_epoch{epoch}_gt{thr}.csv")
    meta_path = os.path.join(out_dir, f"live_extra_flips_epoch{epoch}_gt{thr}.meta.json")

    rows: List[List[Any]] = []
    stake_total = 0.0
    stake_rows = 0

    for a in sorted(authors_gt, key=lambda x: (counts[x], x), reverse=True):
        c = counts[a]
        extra = max(0, c - thr)
        stake = ""
        extra_per_stake = ""
        stake_note = ""
        if args.fetch_stake:
            s, note = fetch_stake_for_address(api, a)
            stake = f"{s:.8f}"
            stake_note = note
            if s > 0:
                extra_per_stake = f"{(extra / s):.12f}"
                stake_total += s
                stake_rows += 1
            else:
                extra_per_stake = ""
        rows.append([
            a,
            c,
            extra,
            stake,
            extra_per_stake,
            stake_note,
            f"https://scan.idena.io/address/{a}",
        ])

    header = ["address", "flipCount", "extraFlipsOverThreshold", "stake", "extraFlipsPerStake", "stakeNote", "scan_url"]
    write_csv(csv_path, header=header, rows=rows)

    meta = {
        "epoch": epoch,
        "threshold": thr,
        "baseUrl": args.base_url,
        "counts": {
            "flipsSeen": seen,
            "uniqueAuthors": total_authors,
            "authorsOverThreshold": authors_gt_n,
            "totalExtraFlips": total_extra_flips,
        },
        "stake": {
            "enabled": bool(args.fetch_stake),
            "authorsWithStake>0": stake_rows,
            "totalStakeOfAuthorsWithExtraFlips": stake_total,
            "extraFlipsPerTotalStake": (total_extra_flips / stake_total) if (args.fetch_stake and stake_total > 0) else None,
        },
        "note": "This is a live snapshot. Data can change until flip submission closes. Epoch 177 is not special-cased here because this script is meant for the current epoch.",
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    log(f"Wrote CSV: {csv_path}")
    log(f"Wrote META: {meta_path}")

    if args.fetch_stake and stake_total > 0:
        log(f"extraFlipsPerTotalStake = {total_extra_flips / stake_total:.12f} (extra flips divided by total stake of those authors)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
