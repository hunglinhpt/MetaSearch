/** Adaptive render-cost gate for streaming markdown (ported from
 * deepseek-ai/chat-demo `useThrottleMarkdownRender`): during a token storm,
 * accept a new text snapshot only after `max(cost * 2, 16)` ms since the last
 * accepted render, capped at 2 s — cheap renders keep up with the stream,
 * expensive ones (long reports) repaint at most ~1 frame per 2 s.
 *
 * The pure gate is clock-injectable so vitest can drive it; the React hook
 * lives in the component (client-only). */

const MIN_GAP_MS = 16;
const MAX_GAP_MS = 2000;

export function gapForCost(costMs: number): number {
  // Audit round 2: NaN must not produce a NaN gap (a NaN delay makes
  // setTimeout fire immediately → busy repaint loop); Infinity clamps to
  // the cap like any other huge cost.
  const cost = Number.isNaN(costMs) ? MIN_GAP_MS : costMs;
  return Math.min(Math.max(cost * 2, MIN_GAP_MS), MAX_GAP_MS);
}

export class AdaptiveRenderGate {
  private costMs: number;
  private lastAcceptedAt: number;
  private readonly now: () => number;

  constructor(initialCostMs = 120, now: () => number = () => performance.now()) {
    this.costMs = initialCostMs;
    this.lastAcceptedAt = -Infinity; // first render always passes
    this.now = now;
  }

  /** True when a new snapshot may be painted now. */
  accepts(): boolean {
    return this.now() - this.lastAcceptedAt >= gapForCost(this.costMs);
  }

  /** Record that a render was accepted now, and what it cost. */
  accept(costMs: number): void {
    this.costMs = costMs;
    this.lastAcceptedAt = this.now();
  }

  /** Delay until the next acceptance window opens (≥ 0). */
  nextDelayMs(): number {
    const wait = gapForCost(this.costMs) - (this.now() - this.lastAcceptedAt);
    return Math.max(0, wait);
  }
}
