// MetaSearch k6 load test (docs/12-roadmap-plan.md Phase 4 & WP-00 perf harness).
//
// Requires the compose stack (real SearXNG) or local bench upstream:
//   cd deploy && docker compose --profile observability up -d
//   k6 run -e BASE_URL=http://localhost:8000 -e API_KEY=msk_xxx k6/load-test.js
//
// Scenarios: steady search traffic + context retrieval + answer streaming + spike.
// Thresholds mirror the KPIs in docs/12 §3.

import http from "k6/http";
import { check, sleep } from "k6";
import { Trend } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
// Audit round 11 P1: compose enables AUTH_ENABLED=true — every k6 request
// must carry the key (k6 run -e API_KEY=msk_... k6/load-test.js).
function authHeaders() {
  const h = { "Content-Type": "application/json" };
  if (__ENV.API_KEY) h.Authorization = `Bearer ${__ENV.API_KEY}`;
  return h;
}

const ttftTrend = new Trend("stream_ttft_duration");

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
    context_steady: {
      executor: "ramping-vus",
      startVUs: 1,
      stages: [
        { duration: "30s", target: 5 },
        { duration: "1m", target: 5 },
        { duration: "15s", target: 0 },
      ],
      exec: "contextFlow",
      startTime: "15s",
    },
    answer_stream: {
      executor: "ramping-vus",
      startVUs: 1,
      stages: [
        { duration: "30s", target: 3 },
        { duration: "1m", target: 3 },
        { duration: "15s", target: 0 },
      ],
      exec: "answerFlow",
      startTime: "30s",
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
    "stream_ttft_duration": ["p(95)<3000"],
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
    headers: authHeaders(),
  });
  check(res, {
    "search 200": (r) => r.status === 200,
    "has results": (r) => r.status !== 200 || r.json("results") !== undefined,
  });
  sleep(1);
}

export function contextFlow() {
  const query = QUERIES[Math.floor(Math.random() * QUERIES.length)];
  // Audit round 11 P1: compose enables AUTH_ENABLED — send the key from the
  // environment (k6 run -e API_KEY=msk_...) and check the v2.0 field name.
  const headers = { "Content-Type": "application/json" };
  if (__ENV.API_KEY) headers.Authorization = `Bearer ${__ENV.API_KEY}`;
  const res = http.post(
    `${BASE_URL}/v1/context`,
    JSON.stringify({ query, k: 10 }),
    { headers }
  );
  check(res, {
    "context 200": (r) => r.status === 200,
    "has chunks": (r) => r.status !== 200 || r.json("chunks") !== undefined,
  });
  sleep(1);
}

export function answerFlow() {
  // SSE streaming: measures response duration and verifies done event
  const start = Date.now();
  const res = http.post(
    `${BASE_URL}/v1/answer`,
    JSON.stringify({ query: "postgresql replication overview", stream: true }),
    { headers: authHeaders() }
  );
  const duration = Date.now() - start;
  ttftTrend.add(duration);

  check(res, {
    "answer 200": (r) => r.status === 200,
    "stream contains done": (r) => r.body.includes("event: done"),
  });
  sleep(2);
}
