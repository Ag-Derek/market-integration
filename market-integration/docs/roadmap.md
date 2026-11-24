# Roadmap

- [ ] Real market-data provider connector (pending provider selection)
- [x] T-bill auction tracker and new-issue monitor (`/auctions`), from
      the Bank of Ghana's published rates. Still to do: auction amounts
      (offered, bid, accepted; BoG press releases only) and BoG's
      quarterly issuance calendar (PDF)
- [x] Economic calendar (`/calendar`): BoG MPC decisions and policy
      rate history, GSS release calendar, auctions, holidays. Still to
      do: the released figures for GSS data (CPI, GDP; GSS publishes them
      only in bulletins and page layout), and implied odds of a rate move
      (needs a market in rate futures or OIS, which Ghana doesn't have)
- [ ] Webhook/event gateway for Symphony workflow triggers (e.g. threshold
      crossings, % change alerts)
- [x] Reconnect/backoff logic for the connector
- [x] Bounded queue + backpressure policy for high-throughput feeds
      (`MarketDataBuffer`, drop-oldest per subscriber)
- [x] OHLCV aggregation + persistence, with CSV export of historical candles
- Charting and analytics (Bloomberg GP/GIP, G, SEAG, CORR equivalents):
  - [x] Technical indicators on the stock chart: SMA, EMA, Bollinger, RSI, MACD
  - [x] Seasonality view on the stock page (`#seasonality`): average
        return by month, year-over-year lines, monthly returns table
  - [x] `/graph` page: several series (equities, GSE-CI, bond yields, FX)
        rebased to 100 on one chart
  - [x] Correlation matrix of the selected series, on returns, measured
        between days both traded (`/graph/correlation`)
  - [x] Spread analysis (`/spreads`, on the Yield Curve tab): fitted GoG
        curve, rich/cheap per bill and bond, corporate spreads by bond and
        issuer. Z-spread and OAS still to do (zero curve, term sheets)
  - [x] Drawing tools on the stock chart (trend lines, levels, Fibonacci),
        saved per user
  - [ ] Finer intraday (1-minute candles or stored ticks)
  - [ ] Adjusted history for corporate actions (splits, bonus issues),
        once real multi-year history is loaded
- [ ] Observability: connection status, dropped messages, queue depth,
      clients and validation rejections are on `/metrics` (messages/sec is
      `rate(market_buffer_received_ticks_total[1m])`); end-to-end latency
      is still to do
