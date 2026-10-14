/*
 * Technical indicators over a price series, for the stock chart.
 *
 * Every function takes an array of numbers (closes, oldest first) and
 * returns arrays of the same length, null where the indicator has no
 * value yet (fewer than `period` points before it). They follow the
 * conventions charting platforms share, so values match theirs:
 *
 *   sma(xs, n)              simple moving average
 *   ema(xs, n)              exponential, alpha 2/(n+1), seeded with the
 *                           SMA of the first n points
 *   bollinger(xs, n, k)     {mid, upper, lower}: SMA ± k population
 *                           standard deviations over the same n points
 *   rsi(xs, n)              Wilder's RSI: average gain and loss seeded
 *                           with the mean of the first n changes, then
 *                           smoothed with alpha 1/n
 *   macd(xs, f, s, sig)     {macd, signal, hist}: EMA(f) - EMA(s), its
 *                           EMA(sig), and the difference
 *
 * Pure functions with no DOM, so tests run them under Node.
 */
(function (root) {
  "use strict";

  function nulls(n) {
    const out = new Array(n);
    for (let i = 0; i < n; i++) out[i] = null;
    return out;
  }

  function sma(xs, n) {
    const out = nulls(xs.length);
    let sum = 0;
    for (let i = 0; i < xs.length; i++) {
      sum += xs[i];
      if (i >= n) sum -= xs[i - n];
      if (i >= n - 1) out[i] = sum / n;
    }
    return out;
  }

  // Skips leading nulls, so it can run over another indicator's output
  // (MACD's signal line is an EMA of the MACD line).
  function ema(xs, n) {
    const out = nulls(xs.length);
    let first = 0;
    while (first < xs.length && xs[first] == null) first++;
    if (xs.length - first < n) return out;
    let sum = 0;
    for (let i = first; i < first + n; i++) sum += xs[i];
    let prev = sum / n;
    out[first + n - 1] = prev;
    const alpha = 2 / (n + 1);
    for (let i = first + n; i < xs.length; i++) {
      prev = alpha * xs[i] + (1 - alpha) * prev;
      out[i] = prev;
    }
    return out;
  }

  function bollinger(xs, n, k) {
    const mid = sma(xs, n);
    const upper = nulls(xs.length), lower = nulls(xs.length);
    for (let i = n - 1; i < xs.length; i++) {
      let ss = 0;
      for (let j = i - n + 1; j <= i; j++) ss += (xs[j] - mid[i]) * (xs[j] - mid[i]);
      const sd = Math.sqrt(ss / n);
      upper[i] = mid[i] + k * sd;
      lower[i] = mid[i] - k * sd;
    }
    return { mid: mid, upper: upper, lower: lower };
  }

  function rsi(xs, n) {
    const out = nulls(xs.length);
    if (xs.length <= n) return out;
    let gain = 0, loss = 0;
    for (let i = 1; i <= n; i++) {
      const d = xs[i] - xs[i - 1];
      if (d > 0) gain += d; else loss -= d;
    }
    gain /= n; loss /= n;
    out[n] = rsiOf(gain, loss);
    for (let i = n + 1; i < xs.length; i++) {
      const d = xs[i] - xs[i - 1];
      gain = (gain * (n - 1) + (d > 0 ? d : 0)) / n;
      loss = (loss * (n - 1) + (d < 0 ? -d : 0)) / n;
      out[i] = rsiOf(gain, loss);
    }
    return out;
  }

  // No losses at all is 100; a flat stretch (no gains either) is 50.
  function rsiOf(gain, loss) {
    if (loss === 0) return gain === 0 ? 50 : 100;
    return 100 - 100 / (1 + gain / loss);
  }

  function macd(xs, fast, slow, signalPeriod) {
    const f = ema(xs, fast), s = ema(xs, slow);
    const line = xs.map(function (_, i) { return f[i] == null || s[i] == null ? null : f[i] - s[i]; });
    const signal = ema(line, signalPeriod);
    const hist = line.map(function (v, i) { return v == null || signal[i] == null ? null : v - signal[i]; });
    return { macd: line, signal: signal, hist: hist };
  }

  const api = { sma: sma, ema: ema, bollinger: bollinger, rsi: rsi, macd: macd };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Indicators = api;
})(this);
