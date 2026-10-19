# Daily Log

## 2026-10-17
Ambiguous brand names (ordinary English words) require case-sensitive matching AND a 'proper noun position' check (emphasized, in a list, or carrying a domain suffix).

## 2026-10-17
The Wilson interval calculation in `score.py` uses Z95 = 1.959963985 (hardcoded constant) and returns (point, low, high) as proportions.

## 2026-10-17
A regex-based list detector `LIST_RE = re.compile(r'^\s*(?:[-*•]|\d+[\.\)])\s+', re.M)` identifies recommendation list structures.

## 2026-10-19
The `visibility()` method returns per-brand presence rate, recommendation rate, share of voice, mean rank, and times recommended, all with 95% CIs.
