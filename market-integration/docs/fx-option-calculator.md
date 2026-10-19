# Hedging page and FX option pricing

## The page (`/hedging`)

The page is for someone with a foreign-currency payment to make, or money
coming in, on a future date. It compares three choices, in cedis:

| Choice | What it is | Total on the day (paying) |
|---|---|---|
| **Wait** | Use the market rate on the day | amount × rate, unknown today; shown as a likely range |
| **Lock in** | A forward | amount × forward, fixed today |
| **Buy protection** | An option: a call when paying, a put when receiving | amount × min(rate, protection rate) + fee (receiving: max(rate, protection rate) − fee) |

A "what happens if the rate on the day is…" table shows each choice's total
at rates across the likely range, plus a shock and any rate the user types.
The best choice at each rate is highlighted. The table's note gives the point
of protection: it's rarely the best at any one rate, but it never trails the
better of the other two by more than its fee (plus n × how far its rate is
less generous than the forward's). A chart draws the same three totals
against the rate.

The "likely range" is the middle 90% of the rate's distribution at expiry
under the same model and volatility the fee uses (`likely_range()`). A user
can paste a bank's quoted fee and see how it compares with the estimate, and
the volatility it assumes. The option detail (the market inputs with their
sources and the Greeks) is in collapsed "for specialists" panels.
`/hedging?dir=receive&currency=EUR` sets the starting direction and currency.
The old `/calculators` address redirects here.

## The pricing (`POST /calc/fx-option`)

The page prices through one endpoint, a European option on a currency against
the cedi: the right, but not the obligation, to buy (a call) or sell (a put)
an amount of a currency at an agreed rate (the strike) on the expiry date. It's
a tool, not a monitor. Nothing streams or is stored: each change to the form
sends one request, and the reply is the answer. The code is in
`app/derivatives/fx_option.py`, priced with [QuantLib](https://www.quantlib.org/).

## What you enter, what it fills in

Only the terms are needed: currency, call or put, amount (in the foreign
currency), and expiry (a tenor from 1W to 1Y, or a date). The strike defaults
to the forward rate for the expiry date, an "at-the-money" option.

The four market inputs are filled from data the service already holds, and
each can be overridden. The reply labels every input with where it came from:

| Input | Default | Label |
|---|---|---|
| Spot | The BoG interbank mid in force on the valuation date (`app/fx/`) | Market data |
| Cedi interest rate | The latest 91, 182 and 364-day T-bill auction yields, read at the option's life (`app/auctions/`) | Market data |
| Foreign interest rate | SOFR, €STR or SONIA, held flat to expiry (`app/fx/overnight.py`). Other currencies have none, so it must be entered | Market data |
| Volatility | How much the BoG fix has actually moved: the standard deviation of daily log changes over the window closest to the option's life (1M, 3M, 6M or 1Y of fixes), annualised on 252 fixes a year | **Estimate** |

Volatility is the one input with no market source. Ghana has no traded cedi
options, so nobody publishes the volatility that option prices assume. The
historical figure is a starting point, not a quote. A dealer will usually
charge for more uncertainty than a managed, once-a-day fix shows. Enter a
quoted price under "Got a quote?" to see the volatility that price assumes.

## What it returns

- **Premium:** the option's estimated value today, for the whole amount and
  per unit, in cedis. Also as a percentage of the amount at spot, and in pips.
- **Forward:** the rate a forward would lock in for the same date, with
  nothing paid up front. It's the same number the FX page's synthetic
  forwards give.
- **Break-even:** strike ± premium per unit. At expiry the rate must be beyond
  this for the option to have paid for itself.
- **Likely range:** the middle 90% of where the rate may be at expiry.
- **Sensitivities (the Greeks),** in cedis for the whole amount:

  | Page wording | Greek | Measured as |
  |---|---|---|
  | The exchange rate | Delta | Per GH₵0.10 rise in spot, and the amount of currency the option behaves like |
  | How fast that changes | Gamma | The extra change over a second GH₵0.10 rise |
  | Uncertainty | Vega | Per 1 point of volatility |
  | Time passing | Theta | Per calendar day |
  | Cedi / foreign interest rates | Rho / foreign rho | Per 1 percentage point |

- **Profit or loss** after the premium, across spot rates, two ways: at
  expiry, and if spot moved today.
- **Implied volatility,** if a quoted premium is given, or why no volatility
  explains it (for example, a price below what the option is worth with no
  uncertainty at all).

## Model and conventions

Garman-Kohlhagen, the Black-Scholes model for currencies: the foreign interest
rate plays the part of a dividend yield. The rates are set up exactly as for
the synthetic forwards on `/fx` (see [fx-rates.md](fx-rates.md)): the cedi
rate simple on ACT/364, and the foreign rate simple on its benchmark's day
count (ACT/360 for SOFR and €STR, ACT/365 for SONIA, and ACT/365 assumed for
an entered rate). So the forward inside the option price equals the FX page's
forward. Volatility time is ACT/365. The tests check every premium against
the closed-form formula, written out separately, and check put-call parity,
the Greeks against small bumps, and the implied-volatility round trip.

QuantLib's evaluation date is global to the process. The endpoint sets it and
prices without awaiting anything in between, so on the event loop's single
thread two calculations can't interleave.

## Not modelled

- Exercise before expiry (American options). Currency options are almost
  always European.
- Settlement lag (expiry vs delivery date) and holidays: dates are calendar
  days from the valuation date.
- Fees, the bid/offer spread, and the dealer's margin.
- A volatility smile: one volatility is used for every strike.
- A live dealing rate: spot is the latest BoG fix, which may be a day or
  more old. Its date is shown.
