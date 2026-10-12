"""
Pull the Bank of Ghana's FX rates into the service's database once, and
exit: what the scheduled job runs, so the rates stay current even while
the service is off (scripts/register-fx-pull-task.ps1 sets it up on
Windows; on macOS/Linux, a cron line).

    python -m app.fx.pull                  # log to the console
    python -m app.fx.pull --log fx-pull.log

Same pull as the service's own hourly one (app.fx.rates): the history
the first time, otherwise only what's new since the latest stored day,
into the database MARKET_DB_PATH names, relative to the directory it
runs in. Both writing the same day is harmless. Once today's fix is
stored it doesn't contact BoG at all. Exits 0 on success, 1 if the pull
failed (the rates already stored are kept).
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

from app import config
from app.fx.bog import BankOfGhanaClient
from app.fx.rates import FxRates
from app.fx.store import FxStore


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Pull the Bank of Ghana's FX rates into the database once.")
    parser.add_argument("--log", help="append the log to this file instead of printing it")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        # Even if something configured logging first: --log must win.
        force=True,
        **({"filename": args.log, "encoding": "utf-8"} if args.log else {"stream": sys.stdout}),
    )

    store = FxStore(config.DB_PATH)
    # Ghana is on GMT, so BoG's business day is the UTC date. BoG
    # publishes one fix a day: once today's is in, there's nothing to ask
    # for until tomorrow, so the job can run often (at every unlock) for
    # free.
    today = datetime.now(timezone.utc).date()
    span = store.span()
    if span is not None and span[1] >= today:
        logging.info("Already have today's fix (%s); nothing to pull", today)
        return 0

    rates = FxRates(store, BankOfGhanaClient())
    ok = asyncio.run(rates.refresh(today=today))
    status = rates.describe()
    if ok:
        logging.info("FX pull done: %s currencies, latest fix %s, in %s", status["currencies"], status["as_of"], config.DB_PATH)
        new = rates.as_of
        if new is not None and (span is None or new > span[1]):
            # BoG gives no publication time; these lines record when each
            # day's fix first appeared (to within how often this runs).
            logging.info("New fix: %s, first seen %s", new, store.first_fetched(new).strftime("%Y-%m-%d %H:%M UTC"))
    else:
        logging.error("FX pull failed: %s (latest fix held: %s)", status["last_error"], status["as_of"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
