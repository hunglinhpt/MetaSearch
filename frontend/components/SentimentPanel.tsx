"use client";

/**
 * Sentiment & Money Flow Panel (docs/27): POST /v1/market/sentiment
 * Renders CNN Fear & Greed (0..100), SentimenTrader Smart vs Dumb Money
 * (explicitly showing "—" when unmeasured — never placeholder 50),
 * 7-pillar checklist, risk circuit breakers, OHLCV/FA quant telemetry,
 * and verbatim top verified drivers across official/VN/global ecosystems.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  marketSentiment,
  safeHref,
  type MarketSentimentResponse,
} from "../lib/api";

const PRESET_TICKERS = ["FPT", "VNM", "HPG", "VCB", "NVDA", "TSLA"];

function scoreTone(score: number | null | undefined, center = 0): string {
  if (score === null || score === undefined) return "muted";
  if (score > center + 0.15) return "tone-bull";
  if (score < center - 0.15) return "tone-bear";
  return "tone-neutral";
}

function fgTone(fg: number | null): string {
  if (fg === null) return "muted";
  if (fg >= 56) return "tone-bull";
  if (fg <= 44) return "tone-bear";
  return "tone-neutral";
}

export default function SentimentPanel({ initialTicker = "" }: { initialTicker?: string }) {
  const [ticker, setTicker] = useState(initialTicker || "FPT");
  const [companyName, setCompanyName] = useState("");
  const [fetchQuant, setFetchQuant] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [data, setData] = useState<MarketSentimentResponse | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (initialTicker && initialTicker.trim()) {
      setTicker(initialTicker.trim().toUpperCase());
    }
  }, [initialTicker]);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  const analyze = useCallback(
    async (overrideTicker?: string) => {
      const sym = (overrideTicker ?? ticker).trim().toUpperCase();
      if (!sym) return;
      if (overrideTicker) setTicker(sym);
      abortRef.current?.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      setLoading(true);
      setError("");
      try {
        const res = await marketSentiment(
          {
            ticker: sym,
            company_name: companyName.trim() || undefined,
            fetch_market_data: fetchQuant,
            max_queries: 10,
            k: 5,
          },
          ctrl.signal
        );
        if (ctrl.signal.aborted) return;
        setData(res);
      } catch (e) {
        if (ctrl.signal.aborted) return;
        setError(e instanceof Error ? e.message : String(e));
        setData(null);
      } finally {
        if (!ctrl.signal.aborted) setLoading(false);
      }
    },
    [ticker, companyName, fetchQuant]
  );

  const bs = data?.benchmark_standards;
  const ohlcv = bs?.quantitative_telemetry?.ohlcv;
  const proxies = bs?.quantitative_telemetry?.market_proxies;
  const fa = bs?.quantitative_telemetry?.fundamentals;

  return (
    <section className="card">
      <div className="compare-head">
        <div>
          <h2 className="section-title">📊 Tâm lý Thị trường &amp; Dòng tiền Đa kênh (VI/EN)</h2>
          <p className="muted small">
            Quét 5 tầng mạng lưới (Sở GD &amp; BCTC · CTCK/Báo chí · MXH Việt Nam · Cashtag Toàn cầu · Video/KOL),
            lọc seeding/tin đồn và đối chiếu 7 trụ cột định lượng thực tế.
          </p>
        </div>
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          void analyze();
        }}
      >
        <div className="search-row">
          <input
            type="text"
            placeholder="Mã cổ phiếu (VD: FPT, VNM, HPG, NVDA, TSLA)"
            value={ticker}
            onChange={(e) => setTicker(e.target.value)}
            disabled={loading}
          />
          <input
            type="text"
            placeholder="Tên công ty (tuỳ chọn, VD: Vinamilk)"
            value={companyName}
            onChange={(e) => setCompanyName(e.target.value)}
            disabled={loading}
          />
          <button type="submit" disabled={loading || !ticker.trim()}>
            {loading ? "Đang phân tích..." : "Phân tích Tâm lý"}
          </button>
        </div>
        <div className="options-row" style={{ marginTop: "0.5rem" }}>
          <label className="checkbox-inline">
            <input
              type="checkbox"
              checked={fetchQuant}
              onChange={(e) => setFetchQuant(e.target.checked)}
            />
            Lấy OHLCV, VIX &amp; BCTC thực tế (<code>fetch_market_data</code>)
          </label>
          <div className="preset-chips">
            {PRESET_TICKERS.map((sym) => (
              <button
                key={sym}
                type="button"
                className={`chip ${ticker.trim().toUpperCase() === sym ? "active" : ""}`}
                disabled={loading}
                onClick={() => void analyze(sym)}
              >
                {sym}
              </button>
            ))}
          </div>
        </div>
      </form>

      {error && <div className="card error">{error}</div>}

      {data && bs && (
        <div className="sentiment-results">
          <div className="sentiment-summary-grid">
            <div className="metric-box">
              <span className="muted small">Chỉ số Fear &amp; Greed (0–100)</span>
              <strong className={`metric-val ${fgTone(bs.fear_greed_index)}`}>
                {bs.fear_greed_index !== null ? `${bs.fear_greed_index}/100` : "—"}
              </strong>
              <span className="muted small">
                {bs.fear_greed_zone} · {bs.vietstock_psychology_state}
              </span>
            </div>
            <div className="metric-box">
              <span className="muted small">Composite &amp; Market Regime</span>
              <strong className={`metric-val ${scoreTone(data.sentiment_summary.composite_score)}`}>
                {data.sentiment_summary.composite_score.toFixed(2)} ({data.sentiment_summary.label})
              </strong>
              <span className="muted small">
                {data.sentiment_summary.market_regime} · Tin cậy {(data.sentiment_summary.confidence * 100).toFixed(0)}%
              </span>
            </div>
            <div className="metric-box">
              <span className="muted small">Smart vs Dumb Money</span>
              <strong className="metric-val">
                {bs.smart_money_confidence !== null ? bs.smart_money_confidence : "—"} /{" "}
                {bs.dumb_money_confidence !== null ? bs.dumb_money_confidence : "—"}
              </strong>
              <span className="muted small">
                Contrarian: {bs.contrarian_signal}
              </span>
            </div>
            <div className="metric-box">
              <span className="muted small">Buzz Velocity &amp; Insider</span>
              <strong className="metric-val">{bs.buzz_velocity}</strong>
              <span className="muted small">
                Insider: {bs.insider_signal} · Bất định {(bs.uncertainty_and_litigation_score * 100).toFixed(0)}%
              </span>
            </div>
          </div>

          {data.risk_circuit_breakers.length > 0 && (
            <div className="breaker-row">
              <strong>Cảnh báo Circuit Breakers: </strong>
              {data.risk_circuit_breakers.map((b) => (
                <span key={b} className="badge badge-warn">
                  {b}
                </span>
              ))}
            </div>
          )}

          <p className="muted small" style={{ marginTop: "0.5rem" }}>
            {data.entity.ticker} ({data.entity.resolved_name} · {data.entity.exchange}) · Kiểm toán MXH:{" "}
            {data.social_integrity_audit.audit_notes}
          </p>

          {(ohlcv?.available || proxies?.available || fa) && (
            <div className="quant-bar">
              {ohlcv?.available && (
                <span>
                  📈 <strong>{ohlcv.source_name || "OHLCV"}</strong> ({ohlcv.bars_count} phiên
                  {ohlcv.as_of ? ` · ${ohlcv.as_of.slice(0, 10)}` : ""}): Giá{" "}
                  {ohlcv.last_close ?? "—"} · Ret20D{" "}
                  {ohlcv.return_20d_pct != null ? `${ohlcv.return_20d_pct}%` : "—"} · RSI14{" "}
                  {ohlcv.rsi_14 ?? "—"} · MFI14 {ohlcv.mfi_14 ?? "—"} · CMF21{" "}
                  {ohlcv.cmf_21 ?? "—"}
                  {ohlcv.source_url && safeHref(ohlcv.source_url) && (
                    <>
                      {" "}
                      (<a href={safeHref(ohlcv.source_url)} target="_blank" rel="noreferrer">nguồn</a>)
                    </>
                  )}
                </span>
              )}
              {proxies?.available && (
                <span>
                  🌐 <strong>Proxies</strong>:
                  {proxies.vix_term_regime ? ` VIX=${proxies.vix_term_regime}` : ""}
                  {proxies.vix_term_ratio != null ? ` (${proxies.vix_term_ratio})` : ""}
                  {proxies.social_s_score != null ? ` S-Score=${proxies.social_s_score}` : ""}
                  {proxies.source_note ? ` · ${proxies.source_note}` : ""}
                </span>
              )}
              {fa && (
                <span>
                  🏛️ <strong>FA ({fa.source || "nguồn thực"})</strong>: P/E {fa.pe ?? "—"} · P/B{" "}
                  {fa.pb ?? "—"} · ROE {fa.roe != null ? `${fa.roe}%` : "—"} · Vốn hóa{" "}
                  {fa.market_cap != null ? `${fa.market_cap} tỷ` : "—"}
                </span>
              )}
            </div>
          )}

          {bs.seven_pillar_checklist.length > 0 && (
            <div className="md-table-wrap" style={{ marginTop: "0.9rem" }}>
              <table className="md-table">
                <thead>
                  <tr>
                    <th>Trụ cột</th>
                    <th>Chuẩn tham chiếu</th>
                    <th>Điểm (0–100)</th>
                    <th>Trạng thái</th>
                    <th>Số bằng chứng</th>
                  </tr>
                </thead>
                <tbody>
                  {bs.seven_pillar_checklist.map((p) => (
                    <tr key={p.pillar_id}>
                      <td>{p.name}</td>
                      <td className="muted small">{p.standard_ref}</td>
                      <td>{p.score_0_100}</td>
                      <td>{p.status}</td>
                      <td>{p.evidence_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="network-grid">
            <div className="metric-box">
              <strong>Tầng Tổ chức &amp; Báo chí</strong>
              <span>Điểm: {data.network_telemetry.official_and_media.weighted_score.toFixed(2)} ({data.network_telemetry.official_and_media.hit_count} tin)</span>
              <span className="muted small">
                {data.network_telemetry.official_and_media.key_catalysts.join(", ") || "Chưa có catalyst nổi bật"}
              </span>
            </div>
            <div className="metric-box">
              <strong>Cộng đồng VN ({data.network_telemetry.vn_social_ecosystem.hit_count} tin)</strong>
              <span>Điểm: {data.network_telemetry.vn_social_ecosystem.weighted_score.toFixed(2)}</span>
              <span className="muted small">{data.network_telemetry.vn_social_ecosystem.crowd_narrative_vi}</span>
            </div>
            <div className="metric-box">
              <strong>MXH Toàn cầu ({data.network_telemetry.global_social_ecosystem.hit_count} tin)</strong>
              <span>Điểm: {data.network_telemetry.global_social_ecosystem.weighted_score.toFixed(2)}</span>
              <span className="muted small">{data.network_telemetry.global_social_ecosystem.crowd_narrative_en}</span>
            </div>
          </div>

          {data.top_verified_drivers.length > 0 && (
            <div style={{ marginTop: "1rem" }}>
              <h3 className="small" style={{ marginBottom: "0.5rem" }}>
                Dẫn chứng đã xác thực ({data.top_verified_drivers.length})
              </h3>
              <ul className="driver-list">
                {data.top_verified_drivers.map((d) => {
                  const href = d.url ? safeHref(d.url) : undefined;
                  return (
                    <li key={`${d.rank}-${d.platform_or_domain}`} className="driver-item">
                      <div className="driver-head">
                        <span className="badge">{d.tier}</span>
                        <span className="badge">{d.polarity}</span>
                        <span className="muted small">
                          {d.platform_or_domain} · {d.freshness} · w={d.effective_weight.toFixed(2)}
                        </span>
                      </div>
                      <div className="driver-title">
                        {href ? (
                          <a href={href} target="_blank" rel="noreferrer">
                            {d.headline_or_post_summary}
                          </a>
                        ) : (
                          d.headline_or_post_summary
                        )}
                      </div>
                      {d.evidence_quote && (
                        <blockquote className="md-quote">{d.evidence_quote}</blockquote>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
