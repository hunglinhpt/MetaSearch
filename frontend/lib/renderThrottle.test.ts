import { describe, expect, it } from "vitest";

import { AdaptiveRenderGate, gapForCost } from "./renderThrottle";

describe("gapForCost", () => {
  it("clamps to 16ms floor and 2000ms cap", () => {
    expect(gapForCost(0)).toBe(16);
    expect(gapForCost(8)).toBe(16);
    expect(gapForCost(100)).toBe(200);
    expect(gapForCost(1500)).toBe(2000);
    expect(gapForCost(5000)).toBe(2000);
  });
});

describe("AdaptiveRenderGate", () => {
  it("always accepts the first render", () => {
    let t = 0;
    const gate = new AdaptiveRenderGate(120, () => t);
    expect(gate.accepts()).toBe(true);
    gate.accept(50);
    // cost 50 → gap 100 → accepted again at t=100
    t = 100;
    expect(gate.accepts()).toBe(true);
  });

  it("rejects snapshots arriving faster than cost×2", () => {
    let t = 0;
    const gate = new AdaptiveRenderGate(120, () => t);
    gate.accept(200); // gap 400
    t = 399;
    expect(gate.accepts()).toBe(false);
    t = 400;
    expect(gate.accepts()).toBe(true);
  });

  it("adapts upward and downward with measured cost", () => {
    let t = 0;
    const gate = new AdaptiveRenderGate(120, () => t);
    gate.accept(500); // expensive render → gap 1000
    t = 500;
    expect(gate.accepts()).toBe(false);
    t = 1000;
    expect(gate.accepts()).toBe(true);
    gate.accept(5); // cheap render → gap 16
    t = 1016;
    expect(gate.accepts()).toBe(true);
  });

  it("nextDelayMs is never negative", () => {
    let t = 0;
    const gate = new AdaptiveRenderGate(120, () => t);
    gate.accept(300); // gap 600
    t = 800; // overdue
    expect(gate.nextDelayMs()).toBe(0);
  });
});
