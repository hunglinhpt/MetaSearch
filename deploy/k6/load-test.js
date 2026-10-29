// MetaSearch k6 load test (docs/12-roadmap-plan.md Phase 4).
//
// Requires the compose stack (real SearXNG):
//   cd deploy && docker compose --profile observability up -d
//   k6 run -e BASE_URL=http://localhost:8000 k6/load-test.js
//
// Scenarios: steady search traffic + answer streaming + a small spike.
// Thresholds mirror the KPIs in docs/12 §3.

import http from "k6/http";
import { check, sleep } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";

export const options = {
  scenarios: {
    steady_search: {
      executor: "ramping-vus",
      startVUs: 1,
      stages: [
        { duration: "30s", target: 10 },
        { duration: "1m", target: 10 },
        { duration: "15s", target: 0 },
      ],
      exec: "searchFlow",
    },
    spike: {
      executor: "ramping-arrival-rate",
      startRate: 1,
      timeUnit: "1s",
      preAllocatedVUs: 20,
      stages: [
        { duration: "10s", target: 5 },
        { duration: "10s", target: 30 },
        { duration: "10s", target: 5 },
      ],
      startTime: "2m",
      exec: "searchFlow",
    },
  },
  thresholds: {
    "http_req_duration{scenario:steady_search}": ["p(95)<2500"], // raw search KPI
    "http_req_failed": ["rate<0.05"],
  },
};

const QUERIES = [
  "so sánh PostgreSQL và MySQL",
  "what is retrieval augmented generation",
  "fastapi streaming best practices",
  "searxng configuration json format",
  "postgres index types explained",
];

export function searchFlow() {
  const query = QUERIES[Math.floor(Math.random() * QUERIES.length)];
  const res = http.post(`${BASE_URL}/v1/search`, JSON.stringify({ query, k: 10 }), {
    headers: { "Content-Type": "application/json" },
  });
  check(res, {
    "search 200": (r) => r.status === 200,
    "has results": (r) => r.status !== 200 || r.json("results") !== undefined,
  });
  sleep(1);
}

export function answerFlow() {
  // SSE streaming: k6 measures time-to-first-byte; done event ends the stream.
  const res = http.post(
    `${BASE_URL}/v1/answer`,
    JSON.stringify({ query: "postgresql replication overview", stream: true }),
    { headers: { "Content-Type": "application/json" } }
  );
  check(res, {
    "answer 200": (r) => r.status === 200,
    "stream contains done": (r) => r.body.includes("event: done"),
  });
  sleep(2);
}
