#!/usr/bin/env python3
"""
Live "extra flips" scan for the current epoch (before validation).

Goal:
- Count how many identities (authors) have published more than N flips (default N=3)
- Compute total extra flips beyond N (sum(max(0, count - N)))
- Optional: fetch stake information:
  - --fetch-stake: fetch stake only for authors with extra flips (flipCount > threshold)
  - --fetch-stake-all: fetch stake for ALL authors who submitted at least 1 flip (heavy)

Endpoints used (Idena indexer API):
- GET /Epoch/Last
- GET /Epoch/{epoch}/Flips (paged)
- GET /Address/{address} (best-effort) OR /Identity/{address} fallback (best-effort)

No external dependencies (stdlib only).
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
from urllib.error import HTTPError, URLError


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
                req = Request(url, headers={"Accept": "application/json", "User-Agent": "idena-extra-flips-live/1.1"})
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
            except (HTTPError, URLError, json.JSONDecodeError, RuntimeError) as e:
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
    for k in ("author", "authorAddress", "address"):
        a = flip.get(k)
        if isinstance(a, str) and a.startswith("0x") and len(a) == 42:
            return a.lower()
    return ""


def try_get_cid(flip: Dict[str, Any]) -> str:
    c = flip.get("cid") or flip.get("Cid") or ""
    return c if isinstance(c, str) else ""


def extract_stake_from_result(res: Any) -> Tuple[float, str]:
    """
    Try multiple known-ish shapes. Returns (stake, note).
    """
    if not isinstance(res, dict):
        return 0.0, "result_not_dict"

    # common top-level keys
    for k in ("stake", "Stake", "stakeBalance", "stakeAmount"):
        if k in res:
            return safe_float(res.get(k)), ""

    # nested balance object
    bal = res.get("balance")
    if isinstance(bal, dict):
        for k in ("stake", "Stake"):
            if k in bal:
                return safe_float(bal.get(k)), ""

    # nested profile/state object
    for k in ("profile", "state"):
        obj = res.get(k)
        if isinstance(obj, dict):
            for kk in ("stake", "Stake"):
                if kk in obj:
                    return safe_float(obj.get(kk)), ""

    return 0.0, "stake_not_found"


def fetch_stake_for_address_best_effort(api: HttpClient, address: str) -> Tuple[float, str]:
    """
    Best-effort stake fetch with fallback endpoints.
    Returns (stake, note).
    """
    # Try /Address/{addr}
    try:
        js = api.get_json(f"/Address/{address}")
        stake, note = extract_stake_from_result(js.get("result"))
        if stake > 0 or note != "stake_not_found":
            return stake, note
    except Exception as e:
        # keep going, try fallback
        pass

    # Try /Identity/{addr} as fallback
    try:
        js = api.get_json(f"/Identity/{address}")
        stake, note = extract_stake_from_result(js.get("result"))
        return stake, note
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
    ap.add_argument("--sleep-per-page", type=float, default=0.1, help="Delay between list pages (sec).")
    ap.add_argument("--top", type=int, default=50, help="Print top N authors by flipCount to console.")
    ap.add_argument("--out-dir", default="./out", help="Output directory.")
    ap.add_argument("--fetch-stake", action="store_true", help="Fetch stake for authors with extra flips (flipCount > threshold).")
    ap.add_argument("--fetch-stake-all", action="store_true", help="Fetch stake for ALL authors who submitted flips (heavy).")
    ap.add_argument("--stake-sleep", type=float, default=0.10, help="Sleep between stake calls (sec). Helps avoid rate limits.")
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

    ranked = sorted(((a, counts[a]) for a in counts), key=lambda x: (x[1], x[0]), reverse=True)

    topn = max(0, int(args.top))
    if topn:
        print("\n=== TOP AUTHORS by flipCount (live) ===")
        for i, (a, c) in enumerate(ranked[:topn], start=1):
            extra = max(0, c - thr)
            mark = "  EXTRA" if extra > 0 else ""
            print(f"{i:4d}  flips={c:2d}  extra={extra:2d}  {a}{mark}")

    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)

    # Always write CSV for authors over threshold
    csv_path_over = os.path.join(out_dir, f"live_extra_flips_epoch{epoch}_gt{thr}.csv")

    # Optional new CSV for all authors with flips + stake
    csv_path_all = os.path.join(out_dir, f"live_flip_authors_epoch{epoch}.csv")

    meta_path = os.path.join(out_dir, f"live_extra_flips_epoch{epoch}_gt{thr}.meta.json")

    # Stake fetching plan
    do_stake_over = bool(args.fetch_stake)
    do_stake_all = bool(args.fetch_stake_all)

    if do_stake_all:
        do_stake_over = False  # avoid double work, we will already cover everyone

    stake_cache: Dict[str, Tuple[float, str]] = {}

    def get_stake_cached(addr: str) -> Tuple[float, str]:
        if addr in stake_cache:
            return stake_cache[addr]
        s, note = fetch_stake_for_address_best_effort(api, addr)
        stake_cache[addr] = (s, note)
        if args.stake_sleep > 0:
            time.sleep(args.stake_sleep)
        return s, note

    # 1) Write authors over threshold CSV
    rows_over: List[List[Any]] = []
    stake_total_over = 0.0
    stake_rows_over = 0

    for a in sorted(authors_gt, key=lambda x: (counts[x], x), reverse=True):
        c = counts[a]
        extra = max(0, c - thr)
        stake = ""
        extra_per_stake = ""
        stake_note = ""

        if do_stake_over:
            s, note = get_stake_cached(a)
            stake_note = note
            stake = f"{s:.8f}"
            if s > 0:
                extra_per_stake = f"{(extra / s):.12f}"
                stake_total_over += s
                stake_rows_over += 1

        rows_over.append([
            a,
            c,
            extra,
            stake,
            extra_per_stake,
            stake_note,
            f"https://scan.idena.io/address/{a}",
        ])

    write_csv(
        csv_path_over,
        header=["address", "flipCount", "extraFlipsOverThreshold", "stake", "extraFlipsPerStake", "stakeNote", "scan_url"],
        rows=rows_over,
    )

    # 2) Optionally write all authors stake CSV
    stake_total_all = 0.0
    stake_rows_all = 0

    if do_stake_all:
        log(f"Fetching stake for ALL flip authors: {len(counts)} addresses")
        rows_all: List[List[Any]] = []
        for i, (a, c) in enumerate(ranked, start=1):
            s, note = get_stake_cached(a)
            if s > 0:
                stake_total_all += s
                stake_rows_all += 1
            rows_all.append([
                a,
                c,
                f"{s:.8f}",
                note,
                f"https://scan.idena.io/address/{a}",
            ])
            if i % 200 == 0:
                log(f"stake progress: {i}/{len(counts)}")

        write_csv(
            csv_path_all,
            header=["address", "flipCount", "stake", "stakeNote", "scan_url"],
            rows=rows_all,
        )
        log(f"Wrote ALL authors CSV: {csv_path_all}")

    # Meta
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
            "fetchStakeOverThresholdEnabled": bool(do_stake_over),
            "fetchStakeAllEnabled": bool(do_stake_all),
            "stakeSleepSeconds": float(args.stake_sleep),
            "overThreshold": {
                "authorsWithStake>0": stake_rows_over,
                "totalStake": stake_total_over,
                "extraFlipsPerTotalStake": (total_extra_flips / stake_total_over) if (do_stake_over and stake_total_over > 0) else None,
            },
            "allAuthors": {
                "authorsWithStake>0": stake_rows_all,
                "totalStake": stake_total_all,
            },
        },
        "note": "Live snapshot. Data can change until flip submission closes.",
    }

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    log(f"Wrote CSV (over threshold): {csv_path_over}")
    log(f"Wrote META: {meta_path}")

    if do_stake_over and stake_total_over > 0:
        log(f"extraFlipsPerTotalStake(over-threshold authors) = {total_extra_flips / stake_total_over:.12f}")

    if do_stake_all:
        log(f"totalStake(all flip authors with stake>0) = {stake_total_all:.8f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

