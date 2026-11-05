import http from 'k6/http';
import { check } from 'k6';

export const options = {
  stages: [{ duration: '5m', target: 5 }],
};

const BASE = __ENV.API_URL;

export default function () {
  const key = `k6-${__VU}-${__ITER}-${Date.now()}`;
  const res = http.post(
    `${BASE}/orders`,
    JSON.stringify({ items: [{ sku: 'SKU-1', qty: 1 }] }),
    { headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key } }
  );
  check(res, { '202 accepted': (r) => r.status === 202 });
}
