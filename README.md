## Live Extra Flips Scanner

This script performs a live scan for "extra flips" in the current epoch. It counts how many authors published more than a chosen number of flips (default: 3) and calculates how many "extra flips" exist beyond that threshold. Optionally, it can also fetch stake information for those authors.

It is meant for live monitoring before the next validation session, so you can estimate how big the extra-flip pool is and how it might affect rewards.

### What it calculates

For the selected epoch (default: current epoch):

* `flipCount` per author (how many flips they published)
* `authorsOverThreshold` = number of authors with `flipCount > threshold`
* `totalExtraFlips` = sum of `max(0, flipCount - threshold)` across authors over threshold

Optional:

* stake for authors over threshold
* `extraFlipsPerTotalStake` = `totalExtraFlips / totalStakeOfAuthorsWithExtraFlips` (rough live metric)

### Requirements

* Python 3
* Internet access
* No Python packages needed (stdlib only)

Check Python:

```bash
python3 --version
```

### How to run

If your script file is named `flip_extra_flips_live.py`:

```bash
python3 flip_extra_flips_live.py --epoch 0 --threshold 3 --page-size 100 --sleep-per-page 0.1 --top 50 --out-dir ./out
```

* `--epoch 0` means: automatically use the current epoch from the API.
* Outputs will be written into `./out`.

### Output files

After running, you get:

* `out/live_extra_flips_epoch{epoch}_gt{threshold}.csv`

  * list of authors with more than the threshold flips
* `out/live_extra_flips_epoch{epoch}_gt{threshold}.meta.json`

  * summary numbers and settings used

### Example: fetch stake too (slower)

This makes extra API calls, one per author over threshold:

```bash
python3 flip_extra_flips_live.py --epoch 0 --threshold 3 --page-size 100 --sleep-per-page 0.1 --top 50 --out-dir ./out --fetch-stake
```

Hint: If you hit rate limits or it feels slow, increase sleep a bit:

```bash
python3 flip_extra_flips_live.py --epoch 0 --threshold 3 --page-size 100 --sleep-per-page 0.3 --top 50 --out-dir ./out --fetch-stake
```

### Common flags

* `--epoch N`

  * Epoch number to scan.
  * Use `0` for "current epoch" (live).
* `--threshold N`

  * Default: 3
  * Authors with `flipCount > N` are counted as having "extra flips".
* `--page-size N`

  * Default: 100
  * Max: 100 (API limit)
* `--sleep-per-page SECONDS`

  * Default: 0.1
  * Helps avoid rate limiting
* `--top N`

  * Default: 50
  * Prints top authors by flip count to the console
* `--out-dir PATH`

  * Default: `./out`
* `--fetch-stake`

  * Also fetch stake for authors with extra flips (slower)

### How to read the numbers (simple)

* If `authorsOverThreshold` is high, many people published more than 3 flips.
* If `totalExtraFlips` is high, the extra-flip reward pool will be shared across many extra flips.
* If you enable stake:

  * `extraFlipsPerTotalStake` is a rough "extra flips per total stake" number for the authors who have extra flips.
  * This is a live estimate and can change until the flip submission window closes.

### Important notes

* This is live data. Numbers can change until flip submission ends.
* Stake fetching is best-effort and may not represent "stake at validation time" perfectly. It is still useful as a live approximation for modeling.

