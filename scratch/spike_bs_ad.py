"""TASK-003 spike: is nepali-datetime accurate enough for the date normalizer?

THROWAWAY script (not part of the app). Prints BS<->AD conversions and how
printed date strings would be read. Every value is library output only unless it
appears in VERIFIED below, which holds pairs San checked in Hamro Patro.

Usage:  uv run python scratch/spike_bs_ad.py
"""

import sys
from datetime import date
from pathlib import Path

import nepali_datetime as nd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# (BS "YYYY-MM-DD", AD "YYYY-MM-DD") pairs checked by San in Hamro Patro.
# TODO(San): fill in. Until then nothing below is verified.
VERIFIED: list[tuple[str, str]] = [
    # ("<BS for 2026-10-03>", "2026-10-03"),   # today
    # ("2083-01-01", "<AD for Baisakh 1, 2083>"),
]

UNVERIFIED = "UNVERIFIED (library output only)"


def bs(text: str) -> nd.date:
    y, m, d = (int(p) for p in text.split("-"))
    return nd.date(y, m, d)


def status(bs_text: str, ad_text: str) -> str:
    return "VERIFIED (Hamro Patro, San)" if (bs_text, ad_text) in VERIFIED else UNVERIFIED


def bs_to_ad_row(bs_text: str) -> None:
    try:
        ad = bs(bs_text).to_datetime_date().isoformat()
    except ValueError as exc:
        print(f"  BS {bs_text} -> ERROR {type(exc).__name__}: {exc}")
        return
    print(f"  BS {bs_text} -> AD {ad}   [{status(bs_text, ad)}]")


def ad_to_bs_row(ad_text: str) -> None:
    result = nd.date.from_datetime_date(date.fromisoformat(ad_text)).strftime("%Y-%m-%d")
    print(f"  AD {ad_text} -> BS {result}   [{status(result, ad_text)}]")


def main() -> int:
    print(f"nepali-datetime supports BS years {nd.MINYEAR}..{nd.MAXYEAR}\n")

    print("Verified facts (asserted):")
    if not VERIFIED:
        print("  none yet -- fill VERIFIED from Hamro Patro")
    for bs_text, ad_text in VERIFIED:
        got = bs(bs_text).to_datetime_date().isoformat()
        assert got == ad_text, f"library says {bs_text} BS = {got} AD, Hamro Patro says {ad_text}"
        print(f"  BS {bs_text} == AD {ad_text}  OK")

    print("\nBS -> AD:")
    for bs_text in ("2083-01-01", "2083-06-14", "2075-05-10", "2076-09-15"):
        bs_to_ad_row(bs_text)

    # Month boundary: last day of Bhadra 2083 and 1 Asoj 2083.
    last_bhadra = max(d for d in range(29, 33) if _valid(2083, 5, d))
    print(f"\nMonth boundary (library says Bhadra 2083 has {last_bhadra} days -- {UNVERIFIED}):")
    bs_to_ad_row(f"2083-05-{last_bhadra:02d}")
    bs_to_ad_row("2083-06-01")

    print("\nInvalid days/months (error type):")
    bs_to_ad_row(f"2083-05-{last_bhadra + 1:02d}")
    bs_to_ad_row("2083-08-30")  # library: Mangsir 2083 has 29 days
    bs_to_ad_row("2083-06-32")
    bs_to_ad_row("2083-13-01")
    bs_to_ad_row("2101-01-01")

    print("\nAD -> BS:")
    for ad_text in ("2026-10-03", "2026-09-30", "2019-01-01"):
        ad_to_bs_row(ad_text)

    print("\nPrinted strings, read by bahikhata.normalize.normalize_date (hint=None):")
    try:
        from bahikhata.normalize import normalize_date
    except ImportError:
        print("  (normalize_date not written yet -- TASK-008)")
        return 0
    for raw in ("2083-06-14 B.S.", "2083/06/14", "14/06/2083", "२०८३/०६/१४", "2026-09-30", "30/09/2026"):
        r = normalize_date(raw, None)
        flags = ", ".join(f.rule_id for f in r.flags) or "-"
        print(f"  {raw!r:22} -> AD {r.date_ad}  BS {r.date_bs}  flags: {flags}   [{UNVERIFIED}]")
    return 0


def _valid(y: int, m: int, d: int) -> bool:
    try:
        nd.date(y, m, d)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
