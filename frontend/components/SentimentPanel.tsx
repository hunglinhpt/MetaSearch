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
  const [useLlmJudge, setUseLlmJudge] = useState(false);
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
            use_llm_judge: useLlmJudge,
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
    [ticker, companyName, fetchQuant, useLlmJudge]
  );

  const bs = data?.benchmark_standards;
  const ohlcv = bs?.quantitative_telemetry?.ohlcv;
  const proxies = bs?.quantitative_telemetry?.market_proxies;
  const fa = bs?.quantitative_telemetry?.fundamentals;
  const flows = data?.institutional_flows;
  const breadth = data?.market_breadth;
  const analyst = data?.analyst_consensus;
  const histS = data?.historical_s_score;
  const corpEvents = data?.corporate_events;
  const llmJudge = data?.llm_judge;

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
            Lấy OHLCV, VIX, CafeF/SSI &amp; BCTC thực tế
          </label>
          <label className="checkbox-inline">
            <input
              type="checkbox"
              checked={useLlmJudge}
              onChange={(e) => setUseLlmJudge(e.target.checked)}
            />
            LLM Judge tổng hợp ({useLlmJudge ? "Bật" : "Tắt"})
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
                {data.sentiment_summary.text_score != null && data.sentiment_summary.quant_score != null
                  ? ` · Text=${data.sentiment_summary.text_score.toFixed(2)} / Quant=${data.sentiment_summary.quant_score.toFixed(2)}`
                  : ""}
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
                {histS?.available && histS.s_score != null
                  ? ` · 20D S-Score=${histS.s_score.toFixed(2)}`
                  : histS
                    ? ` · Tích luỹ ${histS.history_days}/5d`
                    : ""}
              </span>
            </div>
          </div>

          {llmJudge && (
            <div className="quant-bar" style={{ marginTop: "0.75rem" }}>
              <span>
                🧠 <strong>LLM Judge</strong> ({llmJudge.driver_alignment} · Rủi ro ngược:{" "}
                {llmJudge.contrarian_risk} · Δ={llmJudge.score_adjustment >= 0 ? "+" : ""}
                {llmJudge.score_adjustment}): {llmJudge.narrative_summary}
              </span>
            </div>
          )}

          {(flows || breadth || analyst || corpEvents) && (
            <div className="network-grid" style={{ marginTop: "0.75rem" }}>
              {flows && (
                <div className="metric-box">
                  <strong>💸 Dòng tiền Tổ chức &amp; Sổ lệnh</strong>
                  <span className="muted small">
                    {flows.foreign_net_val_5d_bil != null
                      ? `Khối ngoại 5D: ${Number(flows.foreign_net_val_5d_bil).toFixed(1)} tỷ`
                      : "Khối ngoại: —"}
                    {flows.prop_net_val_5d_bil != null
                      ? ` · Tự doanh 5D: ${Number(flows.prop_net_val_5d_bil).toFixed(1)} tỷ`
                      : ""}
                  </span>
                  <span className="muted small">
                    {flows.active_buy_ratio != null
                      ? `Chủ động Mua: ${(Number(flows.active_buy_ratio) * 100).toFixed(1)}%`
                      : ""}
                    {flows.insider_mspr != null
                      ? ` · Nội bộ MSPR: ${Number(flows.insider_mspr).toFixed(0)}%`
                      : ""}
                  </span>
                </div>
              )}
              {breadth && (
                <div className="metric-box">
                  <strong>📊 Độ rộng ({String(breadth.exchange || "Market")})</strong>
                  <span className="muted small">
                    {breadth.advancing_issues != null
                      ? `Tăng ${String(breadth.advancing_issues)} / Giảm ${String(breadth.declining_issues)}`
                      : ""}
                    {breadth.arms_trin != null ? ` · TRIN=${String(breadth.arms_trin)}` : ""}
                    {breadth.mcclellan_rana != null
                      ? ` · RANA=${String(breadth.mcclellan_rana)}`
                      : ""}
                    {breadth.cnn_fear_greed_score != null
                      ? `CNN F&G=${String(breadth.cnn_fear_greed_score)} (${String(breadth.cnn_fear_greed_rating || "")})`
                      : ""}
                  </span>
                  <span className="muted small">
                    {breadth.cboe_equity_put_call_ratio != null
                      ? `CBOE Eq PCR=${String(breadth.cboe_equity_put_call_ratio)}`
                      : ""}
                    {breadth.fred_hy_oas_bps != null
                      ? ` · FRED HY OAS=${String(breadth.fred_hy_oas_bps)} bps`
                      : ""}
                  </span>
                </div>
              )}
              {analyst && (
                <div className="metric-box">
                  <strong>🎯 Khuyến nghị CTCK ({String(analyst.report_count ?? 0)} BCPT)</strong>
                  <span>
                    {String(analyst.consensus_rating || "NEUTRAL")} · Mua {String(analyst.buy_count ?? 0)} / Giữ{" "}
                    {String(analyst.hold_count ?? 0)} / Bán {String(analyst.sell_count ?? 0)}
                  </span>
                  <span className="muted small">
                    {analyst.avg_target_price != null
                      ? `Giá MT TB: ${Number(analyst.avg_target_price).toLocaleString("vi-VN")}`
                      : "Chưa có giá MT"}
                    {analyst.upside_pct != null
                      ? ` (${Number(analyst.upside_pct) > 0 ? "+" : ""}${String(analyst.upside_pct)}%)`
                      : ""}
                  </span>
                </div>
              )}
              {corpEvents && (
                <div className="metric-box">
                  <strong>📅 Sự kiện &amp; Cảnh báo Niêm yết</strong>
                  <span className="muted small">
                    {String(corpEvents.count ?? 0)} sự kiện gần đây / sắp tới
                  </span>
                  {Array.isArray(corpEvents.risk_alerts) && corpEvents.risk_alerts.length > 0 && (
                    <span className="badge badge-warn">
                      {String(corpEvents.risk_alerts[0])}
                    </span>
                  )}
                </div>
              )}
            </div>
          )}

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
