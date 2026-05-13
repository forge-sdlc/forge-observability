# Grafana Langfuse Data Source Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Grafana frontend data source plugin that connects Grafana to a self-hosted Langfuse instance via its HTTP API, with two auto-provisioned dashboards and a Podman Compose file for local development.

**Architecture:** A TypeScript/React Grafana datasource plugin (no Go backend) that proxies all Langfuse API calls through Grafana's built-in backend proxy, eliminating CORS issues and keeping credentials server-side. Five fixed query types (trace_cost, trace_latency, trace_count, observation_tokens, observation_cost) fetch paginated data from Langfuse and bucket it client-side into time series. Grafana provisioning auto-loads the datasource config and dashboard JSON on startup.

**Tech Stack:** TypeScript, React, `@grafana/data`, `@grafana/runtime`, `@grafana/ui`, `rxjs`, Jest, `@grafana/create-plugin` scaffolding, Podman Compose

---

## File Map

| File | Responsibility |
|---|---|
| `src/plugin.json` | Plugin metadata, plugin ID, proxy route config |
| `src/types.ts` | `LangfuseQuery`, `LangfuseOptions`, `QueryType`, Langfuse API shapes |
| `src/utils.ts` | `fetchAllPages`, `bucketByTime`, `getBucketSize` — pure/injectable, fully testable |
| `src/datasource.ts` | `LangfuseDatasource` class — delegates to utils, builds DataFrames |
| `src/ConfigEditor.tsx` | Datasource config UI (URL + basicAuth handled by Grafana natively) |
| `src/QueryEditor.tsx` | Query editor UI — queryType Select dropdown |
| `src/module.ts` | Plugin entry point — registers DataSourcePlugin |
| `src/__tests__/utils.test.ts` | Unit tests for fetchAllPages, bucketByTime, getBucketSize |
| `src/__tests__/datasource.test.ts` | Integration tests for query() — mocks utils |
| `provisioning/datasources/langfuse.yml` | Auto-provisions Langfuse datasource on Grafana startup |
| `provisioning/dashboards/dashboards.yml` | Tells Grafana where to find dashboard JSON |
| `provisioning/dashboards/llm-cost.json` | LLM Cost Overview dashboard |
| `provisioning/dashboards/llm-performance.json` | LLM Performance dashboard |
| `devtools/compose.grafana.yml` | Starts Grafana with plugin + provisioning mounted |
| `.env.example` | Documents required environment variables |

---

## Task 1: Scaffold the project

**Files:**
- Create: `~/git/grafana-langfuse-datasource/` (entire scaffolded project)

- [ ] **Step 1: Run create-plugin scaffolding**

```bash
cd ~/git
npx @grafana/create-plugin@latest
```

When prompted:
- Plugin name: `Langfuse`
- Organization name: `forge`
- Plugin type: `datasource`
- Has backend (Go): `No`

This creates `~/git/forge-langfuse-datasource/`. If the generated directory is named differently, use whatever name was created.

- [ ] **Step 2: Rename directory to match spec**

```bash
mv ~/git/forge-langfuse-datasource ~/git/grafana-langfuse-datasource
cd ~/git/grafana-langfuse-datasource
```

- [ ] **Step 3: Install dependencies**

```bash
npm install
```

- [ ] **Step 4: Verify the scaffolded project builds**

```bash
npm run build
```

Expected: `dist/` directory created with `module.js`, `plugin.json`, etc. No errors.

- [ ] **Step 5: Verify the scaffolded tests pass**

```bash
npm run test -- --watchAll=false
```

Expected: All generated tests pass (there may be 0 or a few placeholder tests).

- [ ] **Step 6: Initialize git**

```bash
git init
git add .
git commit -m "chore: scaffold grafana-langfuse-datasource plugin"
```

---

## Task 2: Configure plugin.json and types

**Files:**
- Modify: `src/plugin.json`
- Create: `src/types.ts` (replaces generated version)

- [ ] **Step 1: Replace `src/plugin.json`**

```json
{
  "$schema": "https://raw.githubusercontent.com/grafana/grafana/main/docs/sources/developers/plugins/plugin.schema.json",
  "type": "datasource",
  "name": "Langfuse",
  "id": "forge-langfuse-datasource",
  "info": {
    "description": "Connect Grafana to Langfuse for LLM observability",
    "author": { "name": "Forge" },
    "keywords": ["langfuse", "llm", "observability"],
    "logos": {
      "small": "img/logo.svg",
      "large": "img/logo.svg"
    },
    "version": "1.0.0",
    "updated": "2026-05-13"
  },
  "routes": [
    {
      "path": "langfuse",
      "url": "{{ .URL }}",
      "authType": "basicAuth"
    }
  ],
  "backend": false
}
```

`{{ .URL }}` is Grafana's template variable for the datasource's configured URL field. The `authType: "basicAuth"` instructs Grafana to attach `Authorization: Basic <base64(user:password)>` to every proxied request using the datasource's stored basicAuth credentials. The plugin makes requests to `/api/datasources/proxy/:id/langfuse/...` and Grafana forwards them to `<configured_url>/...`.

- [ ] **Step 2: Write `src/types.ts`**

```typescript
import { DataQuery, DataSourceJsonData } from '@grafana/data';

export type QueryType =
  | 'trace_cost'
  | 'trace_latency'
  | 'trace_count'
  | 'observation_tokens'
  | 'observation_cost';

export interface LangfuseQuery extends DataQuery {
  queryType: QueryType;
}

export interface LangfuseOptions extends DataSourceJsonData {
  // URL is stored in instanceSettings.url (Grafana standard field).
  // Public key and secret key use Grafana's built-in basicAuth fields.
  // No custom jsonData fields needed for v1.
}

// Langfuse API response shapes
export interface LangfuseTrace {
  id: string;
  timestamp: string;       // ISO 8601 — when the trace was created
  updatedAt: string;       // ISO 8601 — last update
  totalCost: number | null;
  latency: number | null;  // seconds (float)
}

export interface LangfuseObservation {
  id: string;
  startTime: string;       // ISO 8601
  totalCost: number | null;
  usageDetails: {
    input: number;
    output: number;
    total: number;
  } | null;
}

export interface LangfusePage<T> {
  data: T[];
  meta: {
    page: number;
    limit: number;
    totalItems: number;
    totalPages: number;
  };
}
```

- [ ] **Step 3: Commit**

```bash
git add src/plugin.json src/types.ts
git commit -m "feat: configure plugin.json proxy route and define types"
```

---

## Task 3: Utility functions (TDD)

**Files:**
- Create: `src/__tests__/utils.test.ts`
- Create: `src/utils.ts`

- [ ] **Step 1: Write failing tests for `getBucketSize`**

Create `src/__tests__/utils.test.ts`:

```typescript
import { getBucketSize, bucketByTime } from '../utils';

const MIN = 60_000;
const HOUR = 3_600_000;
const DAY = 86_400_000;

describe('getBucketSize', () => {
  it('returns 10-minute buckets for ranges up to 6 hours', () => {
    expect(getBucketSize(6 * HOUR)).toBe(10 * MIN);
    expect(getBucketSize(1 * HOUR)).toBe(10 * MIN);
  });

  it('returns 1-hour buckets for ranges between 6 hours and 7 days', () => {
    expect(getBucketSize(7 * HOUR)).toBe(HOUR);
    expect(getBucketSize(24 * HOUR)).toBe(HOUR);
    expect(getBucketSize(7 * DAY)).toBe(HOUR);
  });

  it('returns 1-day buckets for ranges over 7 days', () => {
    expect(getBucketSize(8 * DAY)).toBe(DAY);
    expect(getBucketSize(30 * DAY)).toBe(DAY);
  });
});

describe('bucketByTime', () => {
  const from = new Date('2024-01-01T00:00:00Z').getTime();
  const to = new Date('2024-01-01T06:00:00Z').getTime(); // 6h range → 10min buckets

  it('sums values within the same bucket', () => {
    const timestamps = [
      '2024-01-01T00:05:00Z', // bucket 0
      '2024-01-01T00:08:00Z', // bucket 0 (same 10min window)
    ];
    const values = [1.0, 2.0];
    const result = bucketByTime(timestamps, values, from, to, 'sum');
    expect(result.values[0]).toBeCloseTo(3.0);
  });

  it('averages values within the same bucket', () => {
    const timestamps = [
      '2024-01-01T00:05:00Z',
      '2024-01-01T00:08:00Z',
    ];
    const values = [1.0, 3.0];
    const result = bucketByTime(timestamps, values, from, to, 'avg');
    expect(result.values[0]).toBeCloseTo(2.0);
  });

  it('counts records per bucket regardless of value', () => {
    const timestamps = [
      '2024-01-01T00:05:00Z',
      '2024-01-01T00:06:00Z',
      '2024-01-01T01:05:00Z', // different bucket
    ];
    const values = [null, null, null];
    const result = bucketByTime(timestamps, values, from, to, 'count');
    expect(result.values[0]).toBe(2);
    expect(result.values[6]).toBe(1); // 60min / 10min = bucket 6
  });

  it('returns zero for empty buckets', () => {
    const result = bucketByTime([], [], from, to, 'sum');
    expect(result.values.every((v) => v === 0)).toBe(true);
  });

  it('drops records outside the time range', () => {
    const timestamps = ['2023-12-31T23:59:00Z']; // before from
    const values = [999];
    const result = bucketByTime(timestamps, values, from, to, 'sum');
    expect(result.values.every((v) => v === 0)).toBe(true);
  });

  it('returns one time value per bucket', () => {
    const result = bucketByTime([], [], from, to, 'sum');
    const expectedBuckets = Math.ceil((to - from) / (10 * MIN));
    expect(result.times).toHaveLength(expectedBuckets);
  });

  it('treats null values as zero for sum and avg', () => {
    const timestamps = ['2024-01-01T00:05:00Z'];
    const values: Array<number | null> = [null];
    const result = bucketByTime(timestamps, values, from, to, 'sum');
    expect(result.values[0]).toBe(0);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
npm run test -- --watchAll=false --testPathPattern="utils"
```

Expected: FAIL — `getBucketSize`, `bucketByTime` not found.

- [ ] **Step 3: Write `fetchAllPages` tests**

Add to `src/__tests__/utils.test.ts` (after the existing describe blocks):

```typescript
import { fetchAllPages } from '../utils';
import { getBackendSrv } from '@grafana/runtime';
import { of } from 'rxjs';

jest.mock('@grafana/runtime', () => ({
  getBackendSrv: jest.fn(),
}));

const mockGetBackendSrv = getBackendSrv as jest.MockedFunction<typeof getBackendSrv>;

function mockFetch(pages: Array<{ data: unknown[]; totalPages: number }>) {
  let call = 0;
  mockGetBackendSrv.mockReturnValue({
    fetch: jest.fn().mockImplementation(() => {
      const page = pages[call++];
      return of({
        data: {
          data: page.data,
          meta: { page: call, limit: 50, totalItems: page.data.length, totalPages: page.totalPages },
        },
      });
    }),
  } as any);
}

describe('fetchAllPages', () => {
  beforeEach(() => jest.clearAllMocks());

  it('returns all items from a single-page response', async () => {
    mockFetch([{ data: [{ id: '1' }, { id: '2' }], totalPages: 1 }]);
    const result = await fetchAllPages('/proxy/langfuse', '/api/public/traces', {});
    expect(result).toHaveLength(2);
  });

  it('fetches all pages when totalPages > 1', async () => {
    mockFetch([
      { data: [{ id: '1' }], totalPages: 2 },
      { data: [{ id: '2' }], totalPages: 2 },
    ]);
    const result = await fetchAllPages('/proxy/langfuse', '/api/public/traces', {});
    expect(result).toHaveLength(2);
    expect((result[0] as any).id).toBe('1');
    expect((result[1] as any).id).toBe('2');
  });

  it('passes params and injects page number on each request', async () => {
    const fetchMock = jest.fn().mockReturnValue(
      of({ data: { data: [], meta: { totalPages: 1 } } })
    );
    mockGetBackendSrv.mockReturnValue({ fetch: fetchMock } as any);

    await fetchAllPages('/proxy/langfuse', '/api/public/traces', { fromUpdatedAt: '2024-01-01T00:00:00Z' });

    expect(fetchMock).toHaveBeenCalledWith(
      expect.objectContaining({
        url: '/proxy/langfuse/api/public/traces',
        params: expect.objectContaining({ fromUpdatedAt: '2024-01-01T00:00:00Z', page: 1, limit: 50 }),
      })
    );
  });

  it('returns empty array when response data is empty', async () => {
    mockFetch([{ data: [], totalPages: 1 }]);
    const result = await fetchAllPages('/proxy/langfuse', '/api/public/traces', {});
    expect(result).toHaveLength(0);
  });
});
```

- [ ] **Step 4: Run to verify fetchAllPages tests also fail**

```bash
npm run test -- --watchAll=false --testPathPattern="utils"
```

Expected: Additional failures for `fetchAllPages` not found.

- [ ] **Step 5: Implement `src/utils.ts`**

```typescript
import { getBackendSrv } from '@grafana/runtime';
import { lastValueFrom } from 'rxjs';
import { LangfusePage } from './types';

export async function fetchAllPages<T>(
  proxyUrl: string,
  path: string,
  params: Record<string, string | number>,
): Promise<T[]> {
  const results: T[] = [];
  let page = 1;

  while (true) {
    const response = await lastValueFrom(
      getBackendSrv().fetch<LangfusePage<T>>({
        url: `${proxyUrl}${path}`,
        params: { ...params, page, limit: 50 },
      })
    );

    results.push(...response.data.data);

    if (page >= (response.data.meta?.totalPages ?? 1)) {
      break;
    }
    page++;
  }

  return results;
}

export function bucketByTime(
  timestamps: string[],
  values: Array<number | null>,
  from: number,
  to: number,
  aggregation: 'sum' | 'avg' | 'count',
): { times: number[]; values: number[] } {
  const bucketMs = getBucketSize(to - from);
  const bucketCount = Math.ceil((to - from) / bucketMs);
  const buckets: Array<number[]> = Array.from({ length: bucketCount }, () => []);

  for (let i = 0; i < timestamps.length; i++) {
    const ts = new Date(timestamps[i]).getTime();
    if (ts < from || ts > to) {
      continue;
    }
    const idx = Math.floor((ts - from) / bucketMs);
    if (idx >= 0 && idx < bucketCount) {
      buckets[idx].push(values[i] ?? 0);
    }
  }

  const times = Array.from({ length: bucketCount }, (_, i) => from + i * bucketMs);
  const resultValues = buckets.map((bucket) => {
    if (aggregation === 'count') {
      return bucket.length;
    }
    if (bucket.length === 0) {
      return 0;
    }
    const sum = bucket.reduce((a, b) => a + b, 0);
    return aggregation === 'sum' ? sum : sum / bucket.length;
  });

  return { times, values: resultValues };
}

export function getBucketSize(rangeMs: number): number {
  const HOUR = 3_600_000;
  const DAY = 86_400_000;

  if (rangeMs <= 6 * HOUR) {
    return 10 * 60_000; // 10 minutes
  }
  if (rangeMs <= 7 * DAY) {
    return HOUR; // 1 hour
  }
  return DAY; // 1 day
}
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
npm run test -- --watchAll=false --testPathPattern="utils"
```

Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/utils.ts src/__tests__/utils.test.ts
git commit -m "feat: add fetchAllPages and bucketByTime utilities with tests"
```

---

## Task 4: DataSource class (TDD)

**Files:**
- Create: `src/__tests__/datasource.test.ts`
- Modify: `src/datasource.ts` (replace generated version)

- [ ] **Step 1: Write failing tests for `datasource.ts`**

Create `src/__tests__/datasource.test.ts`:

```typescript
import { dateTime } from '@grafana/data';
import { LangfuseDatasource } from '../datasource';
import * as utils from '../utils';

// Only mock fetchAllPages — let bucketByTime run real so DataFrames are populated correctly.
jest.mock('../utils', () => ({
  ...jest.requireActual('../utils'),
  fetchAllPages: jest.fn(),
}));

const mockFetchAllPages = utils.fetchAllPages as jest.MockedFunction<typeof utils.fetchAllPages>;

function makeInstanceSettings(overrides = {}) {
  return {
    id: 1,
    uid: 'test-uid',
    type: 'forge-langfuse-datasource',
    name: 'Langfuse Test',
    url: '/api/datasources/proxy/1',
    jsonData: {},
    ...overrides,
  } as any;
}

function makeQueryOptions(queryType: string, fromIso: string, toIso: string) {
  return {
    targets: [{ queryType, refId: 'A', hide: false }],
    range: {
      from: dateTime(fromIso),
      to: dateTime(toIso),
      raw: { from: fromIso, to: toIso },
    },
    requestId: 'test',
    timezone: 'UTC',
    scopedVars: {},
    startTime: 0,
  } as any;
}

describe('LangfuseDatasource.query', () => {
  let ds: LangfuseDatasource;

  beforeEach(() => {
    jest.clearAllMocks();
    ds = new LangfuseDatasource(makeInstanceSettings());
  });

  it('returns a DataFrame with time and value fields for trace_cost', async () => {
    mockFetchAllPages.mockResolvedValue([
      { id: 't1', timestamp: '2024-01-01T01:00:00Z', totalCost: 0.05, latency: 1.5 },
      { id: 't2', timestamp: '2024-01-01T02:00:00Z', totalCost: 0.03, latency: 2.0 },
    ]);

    const result = await ds.query(
      makeQueryOptions('trace_cost', '2024-01-01T00:00:00Z', '2024-01-02T00:00:00Z')
    );

    expect(result.data).toHaveLength(1);
    const frame = result.data[0];
    expect(frame.fields[0].name).toBe('time');
    expect(frame.fields[1].name).toBe('Total Cost (USD)');
    expect(frame.fields[0].values.length).toBeGreaterThan(0);
  });

  it('returns latency values for trace_latency', async () => {
    mockFetchAllPages.mockResolvedValue([
      { id: 't1', timestamp: '2024-01-01T01:00:00Z', totalCost: 0, latency: 2.5 },
    ]);

    const result = await ds.query(
      makeQueryOptions('trace_latency', '2024-01-01T00:00:00Z', '2024-01-02T00:00:00Z')
    );

    const frame = result.data[0];
    expect(frame.fields[1].name).toBe('Avg Latency (s)');
  });

  it('returns count values for trace_count', async () => {
    mockFetchAllPages.mockResolvedValue([
      { id: 't1', timestamp: '2024-01-01T01:00:00Z' },
      { id: 't2', timestamp: '2024-01-01T01:05:00Z' },
    ]);

    const result = await ds.query(
      makeQueryOptions('trace_count', '2024-01-01T00:00:00Z', '2024-01-02T00:00:00Z')
    );

    const frame = result.data[0];
    expect(frame.fields[1].name).toBe('Trace Volume');
  });

  it('returns token totals for observation_tokens', async () => {
    mockFetchAllPages.mockResolvedValue([
      { id: 'o1', startTime: '2024-01-01T01:00:00Z', totalCost: 0, usageDetails: { input: 100, output: 50, total: 150 } },
    ]);

    const result = await ds.query(
      makeQueryOptions('observation_tokens', '2024-01-01T00:00:00Z', '2024-01-02T00:00:00Z')
    );

    const frame = result.data[0];
    expect(frame.fields[1].name).toBe('Total Tokens');
  });

  it('returns cost for observation_cost', async () => {
    mockFetchAllPages.mockResolvedValue([
      { id: 'o1', startTime: '2024-01-01T01:00:00Z', totalCost: 0.02, usageDetails: null },
    ]);

    const result = await ds.query(
      makeQueryOptions('observation_cost', '2024-01-01T00:00:00Z', '2024-01-02T00:00:00Z')
    );

    const frame = result.data[0];
    expect(frame.fields[1].name).toBe('Observation Cost (USD)');
  });

  it('skips hidden targets', async () => {
    const options = makeQueryOptions('trace_cost', '2024-01-01T00:00:00Z', '2024-01-02T00:00:00Z');
    options.targets[0].hide = true;

    const result = await ds.query(options);
    expect(result.data).toHaveLength(0);
    expect(mockFetchAllPages).not.toHaveBeenCalled();
  });

  it('uses the correct proxy URL prefix', async () => {
    mockFetchAllPages.mockResolvedValue([]);
    await ds.query(makeQueryOptions('trace_cost', '2024-01-01T00:00:00Z', '2024-01-02T00:00:00Z'));

    expect(mockFetchAllPages).toHaveBeenCalledWith(
      '/api/datasources/proxy/1/langfuse',
      '/api/public/traces',
      expect.any(Object)
    );
  });
});

describe('LangfuseDatasource.testDatasource', () => {
  it('returns success when Langfuse is reachable', async () => {
    mockFetchAllPages.mockResolvedValue([]);
    const ds = new LangfuseDatasource(makeInstanceSettings());
    const result = await ds.testDatasource();
    expect(result.status).toBe('success');
  });

  it('returns error when Langfuse is unreachable', async () => {
    mockFetchAllPages.mockRejectedValue(new Error('connection refused'));
    const ds = new LangfuseDatasource(makeInstanceSettings());
    const result = await ds.testDatasource();
    expect(result.status).toBe('error');
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
npm run test -- --watchAll=false --testPathPattern="datasource"
```

Expected: FAIL — `LangfuseDatasource` not exported from `datasource.ts` or missing query methods.

- [ ] **Step 3: Implement `src/datasource.ts`**

```typescript
import {
  DataSourceApi,
  DataQueryRequest,
  DataQueryResponse,
  DataSourceInstanceSettings,
  FieldType,
  MutableDataFrame,
} from '@grafana/data';
import { LangfuseQuery, LangfuseOptions, LangfuseTrace, LangfuseObservation } from './types';
import { fetchAllPages, bucketByTime } from './utils';

export class LangfuseDatasource extends DataSourceApi<LangfuseQuery, LangfuseOptions> {
  private readonly proxyUrl: string;

  constructor(instanceSettings: DataSourceInstanceSettings<LangfuseOptions>) {
    super(instanceSettings);
    this.proxyUrl = `${instanceSettings.url}/langfuse`;
  }

  async query(options: DataQueryRequest<LangfuseQuery>): Promise<DataQueryResponse> {
    const { range, targets } = options;
    const from = range.from.valueOf();
    const to = range.to.valueOf();
    const fromIso = range.from.toISOString();
    const toIso = range.to.toISOString();

    const data = await Promise.all(
      targets
        .filter((t) => !t.hide && t.queryType)
        .map((t) => this.runQuery(t, from, to, fromIso, toIso))
    );

    return { data };
  }

  private async runQuery(
    target: LangfuseQuery,
    from: number,
    to: number,
    fromIso: string,
    toIso: string
  ): Promise<MutableDataFrame> {
    switch (target.queryType) {
      case 'trace_cost':
        return this.queryTraceCost(target.refId, from, to, fromIso, toIso);
      case 'trace_latency':
        return this.queryTraceLatency(target.refId, from, to, fromIso, toIso);
      case 'trace_count':
        return this.queryTraceCount(target.refId, from, to, fromIso, toIso);
      case 'observation_tokens':
        return this.queryObservationTokens(target.refId, from, to, fromIso, toIso);
      case 'observation_cost':
        return this.queryObservationCost(target.refId, from, to, fromIso, toIso);
      default:
        return new MutableDataFrame({ refId: target.refId, fields: [] });
    }
  }

  private async queryTraceCost(refId: string, from: number, to: number, fromIso: string, toIso: string) {
    const traces = await fetchAllPages<LangfuseTrace>(this.proxyUrl, '/api/public/traces', {
      fromUpdatedAt: fromIso,
      toUpdatedAt: toIso,
    });
    const { times, values } = bucketByTime(
      traces.map((t) => t.timestamp),
      traces.map((t) => t.totalCost),
      from, to, 'sum'
    );
    return toFrame(refId, 'Total Cost (USD)', FieldType.number, times, values);
  }

  private async queryTraceLatency(refId: string, from: number, to: number, fromIso: string, toIso: string) {
    const traces = await fetchAllPages<LangfuseTrace>(this.proxyUrl, '/api/public/traces', {
      fromUpdatedAt: fromIso,
      toUpdatedAt: toIso,
    });
    const { times, values } = bucketByTime(
      traces.map((t) => t.timestamp),
      traces.map((t) => t.latency),
      from, to, 'avg'
    );
    return toFrame(refId, 'Avg Latency (s)', FieldType.number, times, values);
  }

  private async queryTraceCount(refId: string, from: number, to: number, fromIso: string, toIso: string) {
    const traces = await fetchAllPages<LangfuseTrace>(this.proxyUrl, '/api/public/traces', {
      fromUpdatedAt: fromIso,
      toUpdatedAt: toIso,
    });
    const { times, values } = bucketByTime(
      traces.map((t) => t.timestamp),
      traces.map(() => null),
      from, to, 'count'
    );
    return toFrame(refId, 'Trace Volume', FieldType.number, times, values);
  }

  private async queryObservationTokens(refId: string, from: number, to: number, fromIso: string, toIso: string) {
    const observations = await fetchAllPages<LangfuseObservation>(
      this.proxyUrl, '/api/public/observations',
      { fromStartTime: fromIso, toStartTime: toIso }
    );
    const { times, values } = bucketByTime(
      observations.map((o) => o.startTime),
      observations.map((o) => o.usageDetails?.total ?? 0),
      from, to, 'sum'
    );
    return toFrame(refId, 'Total Tokens', FieldType.number, times, values);
  }

  private async queryObservationCost(refId: string, from: number, to: number, fromIso: string, toIso: string) {
    const observations = await fetchAllPages<LangfuseObservation>(
      this.proxyUrl, '/api/public/observations',
      { fromStartTime: fromIso, toStartTime: toIso }
    );
    const { times, values } = bucketByTime(
      observations.map((o) => o.startTime),
      observations.map((o) => o.totalCost),
      from, to, 'sum'
    );
    return toFrame(refId, 'Observation Cost (USD)', FieldType.number, times, values);
  }

  async testDatasource(): Promise<{ status: string; message: string }> {
    try {
      await fetchAllPages(this.proxyUrl, '/api/public/traces', { limit: 1, page: 1 });
      return { status: 'success', message: 'Connected to Langfuse successfully' };
    } catch (err) {
      return { status: 'error', message: `Failed to connect: ${String(err)}` };
    }
  }
}

function toFrame(
  refId: string,
  name: string,
  type: FieldType,
  times: number[],
  values: number[]
): MutableDataFrame {
  return new MutableDataFrame({
    refId,
    fields: [
      { name: 'time', type: FieldType.time, values: times },
      { name, type, values },
    ],
  });
}
```

- [ ] **Step 4: Run all tests to verify they pass**

```bash
npm run test -- --watchAll=false
```

Expected: All tests pass. Note: if `toUpdatedAt` is not supported by the Langfuse API in your deployment, remove it from the params — the `fromUpdatedAt` filter alone is sufficient.

- [ ] **Step 5: Commit**

```bash
git add src/datasource.ts src/__tests__/datasource.test.ts
git commit -m "feat: implement LangfuseDatasource with 5 query types and tests"
```

---

## Task 5: ConfigEditor

**Files:**
- Modify: `src/ConfigEditor.tsx` (replace generated version)

Grafana renders URL and basicAuth fields automatically for any datasource with `basicAuth` enabled. The ConfigEditor only needs to handle custom `jsonData` fields — of which we have none in v1. The component can be minimal.

- [ ] **Step 1: Replace `src/ConfigEditor.tsx`**

```tsx
import React from 'react';
import { DataSourcePluginOptionsEditorProps } from '@grafana/data';
import { LangfuseOptions } from './types';

type Props = DataSourcePluginOptionsEditorProps<LangfuseOptions>;

export function ConfigEditor(_props: Props) {
  // Grafana renders the URL field and Basic Auth (username/password) fields
  // automatically. No custom configuration needed for v1.
  return null;
}
```

- [ ] **Step 2: Commit**

```bash
git add src/ConfigEditor.tsx
git commit -m "feat: add minimal ConfigEditor (Grafana handles URL and basicAuth)"
```

---

## Task 6: QueryEditor

**Files:**
- Modify: `src/QueryEditor.tsx` (replace generated version)

- [ ] **Step 1: Replace `src/QueryEditor.tsx`**

```tsx
import React from 'react';
import { QueryEditorProps, SelectableValue } from '@grafana/data';
import { Select, InlineField } from '@grafana/ui';
import { LangfuseDatasource } from './datasource';
import { LangfuseOptions, LangfuseQuery, QueryType } from './types';

const QUERY_TYPE_OPTIONS: Array<SelectableValue<QueryType>> = [
  { label: 'Cost (traces)', value: 'trace_cost', description: 'Sum of LLM cost per time bucket' },
  { label: 'Latency (traces)', value: 'trace_latency', description: 'Average trace latency per time bucket' },
  { label: 'Volume (traces)', value: 'trace_count', description: 'Number of traces per time bucket' },
  { label: 'Token usage (observations)', value: 'observation_tokens', description: 'Total tokens (input + output) per time bucket' },
  { label: 'Cost (observations)', value: 'observation_cost', description: 'Sum of observation cost per time bucket' },
];

type Props = QueryEditorProps<LangfuseDatasource, LangfuseQuery, LangfuseOptions>;

export function QueryEditor({ query, onChange, onRunQuery }: Props) {
  const onQueryTypeChange = (selected: SelectableValue<QueryType>) => {
    onChange({ ...query, queryType: selected.value! });
    onRunQuery();
  };

  return (
    <InlineField label="Metric" labelWidth={12}>
      <Select
        options={QUERY_TYPE_OPTIONS}
        value={query.queryType}
        onChange={onQueryTypeChange}
        placeholder="Select metric..."
        width={32}
      />
    </InlineField>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add src/QueryEditor.tsx
git commit -m "feat: add QueryEditor with queryType Select dropdown"
```

---

## Task 7: Wire module.ts and build

**Files:**
- Modify: `src/module.ts` (replace generated version)

- [ ] **Step 1: Replace `src/module.ts`**

```typescript
import { DataSourcePlugin } from '@grafana/data';
import { LangfuseDatasource } from './datasource';
import { ConfigEditor } from './ConfigEditor';
import { QueryEditor } from './QueryEditor';
import { LangfuseOptions, LangfuseQuery } from './types';

export const plugin = new DataSourcePlugin<LangfuseDatasource, LangfuseQuery, LangfuseOptions>(
  LangfuseDatasource
)
  .setConfigEditor(ConfigEditor)
  .setQueryEditor(QueryEditor);
```

- [ ] **Step 2: Run all tests one final time**

```bash
npm run test -- --watchAll=false
```

Expected: All tests pass.

- [ ] **Step 3: Build the plugin**

```bash
npm run build
```

Expected: `dist/` directory populated with `module.js`, `plugin.json`, and supporting files. No TypeScript errors.

- [ ] **Step 4: Commit**

```bash
git add src/module.ts
git commit -m "feat: wire DataSourcePlugin and verify build"
```

---

## Task 8: Provisioning files

**Files:**
- Create: `provisioning/datasources/langfuse.yml`
- Create: `provisioning/dashboards/dashboards.yml`

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p provisioning/datasources provisioning/dashboards
```

- [ ] **Step 2: Create `provisioning/datasources/langfuse.yml`**

```yaml
apiVersion: 1

datasources:
  - name: Langfuse
    type: forge-langfuse-datasource
    uid: langfuse
    url: "http://${LANGFUSE_HOST}:${LANGFUSE_PORT}"
    editable: false
    basicAuth: true
    basicAuthUser: "${LANGFUSE_PUBLIC_KEY}"
    secureJsonData:
      basicAuthPassword: "${LANGFUSE_SECRET_KEY}"
```

The `uid: langfuse` is referenced by the dashboard JSON files so they auto-wire to this datasource. Grafana expands `${VAR}` syntax in provisioning files from environment variables.

- [ ] **Step 3: Create `provisioning/dashboards/dashboards.yml`**

```yaml
apiVersion: 1

providers:
  - name: Langfuse Dashboards
    orgId: 1
    type: file
    disableDeletion: false
    updateIntervalSeconds: 30
    options:
      path: /etc/grafana/provisioning/dashboards
      foldersFromFilesStructure: false
```

- [ ] **Step 4: Commit**

```bash
git add provisioning/
git commit -m "feat: add Grafana provisioning config for datasource and dashboards"
```

---

## Task 9: LLM Cost Overview dashboard

**Files:**
- Create: `provisioning/dashboards/llm-cost.json`

- [ ] **Step 1: Create `provisioning/dashboards/llm-cost.json`**

```json
{
  "__inputs": [],
  "__requires": [],
  "annotations": { "list": [] },
  "editable": true,
  "fiscalYearStartMonth": 0,
  "graphTooltip": 0,
  "links": [],
  "panels": [
    {
      "datasource": { "type": "forge-langfuse-datasource", "uid": "langfuse" },
      "fieldConfig": {
        "defaults": { "unit": "currencyUSD", "color": { "mode": "palette-classic" } },
        "overrides": []
      },
      "gridPos": { "h": 8, "w": 18, "x": 0, "y": 0 },
      "id": 1,
      "options": {
        "legend": { "calcs": ["sum"], "displayMode": "list", "placement": "bottom" },
        "tooltip": { "mode": "single" }
      },
      "targets": [
        {
          "datasource": { "type": "forge-langfuse-datasource", "uid": "langfuse" },
          "queryType": "trace_cost",
          "refId": "A"
        }
      ],
      "title": "Trace Cost Over Time",
      "type": "timeseries"
    },
    {
      "datasource": { "type": "forge-langfuse-datasource", "uid": "langfuse" },
      "fieldConfig": {
        "defaults": { "unit": "currencyUSD", "color": { "mode": "thresholds" } },
        "overrides": []
      },
      "gridPos": { "h": 8, "w": 6, "x": 18, "y": 0 },
      "id": 2,
      "options": {
        "colorMode": "value",
        "graphMode": "none",
        "justifyMode": "center",
        "orientation": "auto",
        "reduceOptions": { "calcs": ["sum"], "fields": "", "values": false },
        "textMode": "auto"
      },
      "targets": [
        {
          "datasource": { "type": "forge-langfuse-datasource", "uid": "langfuse" },
          "queryType": "trace_cost",
          "refId": "A"
        }
      ],
      "title": "Total Cost",
      "type": "stat"
    },
    {
      "datasource": { "type": "forge-langfuse-datasource", "uid": "langfuse" },
      "fieldConfig": {
        "defaults": { "unit": "currencyUSD", "color": { "mode": "palette-classic" } },
        "overrides": []
      },
      "gridPos": { "h": 8, "w": 24, "x": 0, "y": 8 },
      "id": 3,
      "options": {
        "legend": { "calcs": ["sum"], "displayMode": "list", "placement": "bottom" },
        "tooltip": { "mode": "single" }
      },
      "targets": [
        {
          "datasource": { "type": "forge-langfuse-datasource", "uid": "langfuse" },
          "queryType": "observation_cost",
          "refId": "A"
        }
      ],
      "title": "Observation Cost Over Time",
      "type": "timeseries"
    }
  ],
  "refresh": "5m",
  "schemaVersion": 36,
  "style": "dark",
  "tags": ["langfuse", "llm", "cost"],
  "time": { "from": "now-24h", "to": "now" },
  "timepicker": {},
  "timezone": "",
  "title": "LLM Cost Overview",
  "uid": "llm-cost-overview",
  "version": 1
}
```

- [ ] **Step 2: Commit**

```bash
git add provisioning/dashboards/llm-cost.json
git commit -m "feat: add LLM Cost Overview dashboard"
```

---

## Task 10: LLM Performance dashboard

**Files:**
- Create: `provisioning/dashboards/llm-performance.json`

- [ ] **Step 1: Create `provisioning/dashboards/llm-performance.json`**

```json
{
  "__inputs": [],
  "__requires": [],
  "annotations": { "list": [] },
  "editable": true,
  "fiscalYearStartMonth": 0,
  "graphTooltip": 0,
  "links": [],
  "panels": [
    {
      "datasource": { "type": "forge-langfuse-datasource", "uid": "langfuse" },
      "fieldConfig": {
        "defaults": { "unit": "s", "color": { "mode": "palette-classic" } },
        "overrides": []
      },
      "gridPos": { "h": 8, "w": 24, "x": 0, "y": 0 },
      "id": 1,
      "options": {
        "legend": { "calcs": ["mean", "max"], "displayMode": "list", "placement": "bottom" },
        "tooltip": { "mode": "single" }
      },
      "targets": [
        {
          "datasource": { "type": "forge-langfuse-datasource", "uid": "langfuse" },
          "queryType": "trace_latency",
          "refId": "A"
        }
      ],
      "title": "Average Trace Latency",
      "type": "timeseries"
    },
    {
      "datasource": { "type": "forge-langfuse-datasource", "uid": "langfuse" },
      "fieldConfig": {
        "defaults": { "unit": "short", "color": { "mode": "palette-classic" } },
        "overrides": []
      },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 8 },
      "id": 2,
      "options": {
        "legend": { "calcs": ["sum"], "displayMode": "list", "placement": "bottom" },
        "tooltip": { "mode": "single" }
      },
      "targets": [
        {
          "datasource": { "type": "forge-langfuse-datasource", "uid": "langfuse" },
          "queryType": "trace_count",
          "refId": "A"
        }
      ],
      "title": "Trace Volume",
      "type": "timeseries"
    },
    {
      "datasource": { "type": "forge-langfuse-datasource", "uid": "langfuse" },
      "fieldConfig": {
        "defaults": { "unit": "short", "color": { "mode": "palette-classic" } },
        "overrides": []
      },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 8 },
      "id": 3,
      "options": {
        "legend": { "calcs": ["sum"], "displayMode": "list", "placement": "bottom" },
        "tooltip": { "mode": "single" }
      },
      "targets": [
        {
          "datasource": { "type": "forge-langfuse-datasource", "uid": "langfuse" },
          "queryType": "observation_tokens",
          "refId": "A"
        }
      ],
      "title": "Token Usage",
      "type": "timeseries"
    }
  ],
  "refresh": "5m",
  "schemaVersion": 36,
  "style": "dark",
  "tags": ["langfuse", "llm", "performance"],
  "time": { "from": "now-24h", "to": "now" },
  "timepicker": {},
  "timezone": "",
  "title": "LLM Performance",
  "uid": "llm-performance",
  "version": 1
}
```

- [ ] **Step 2: Commit**

```bash
git add provisioning/dashboards/llm-performance.json
git commit -m "feat: add LLM Performance dashboard"
```

---

## Task 11: Compose file, .env.example, and smoke test

**Files:**
- Create: `devtools/compose.grafana.yml`
- Create: `.env.example`

- [ ] **Step 1: Create `devtools/` directory**

```bash
mkdir -p devtools
```

- [ ] **Step 2: Create `devtools/compose.grafana.yml`**

```yaml
# Grafana with Langfuse data source plugin
#
# Build the plugin first:
#   npm run build
#
# Then start Grafana:
#   podman compose --env-file ../.env -f devtools/compose.grafana.yml up -d
#
# Grafana UI: http://localhost:3001
# Default credentials: admin / admin
#
# Tear down:
#   podman compose -f devtools/compose.grafana.yml down

name: grafana-langfuse

services:
  grafana:
    image: grafana/grafana:latest
    ports:
      - "3001:3000"
    environment:
      GF_PATHS_PROVISIONING: /etc/grafana/provisioning
      GF_PLUGINS_ALLOW_LOADING_UNSIGNED_PLUGINS: forge-langfuse-datasource
      GF_AUTH_ANONYMOUS_ENABLED: "true"
      GF_AUTH_ANONYMOUS_ORG_ROLE: Viewer
      LANGFUSE_HOST: ${LANGFUSE_HOST:-host.containers.internal}
      LANGFUSE_PORT: ${LANGFUSE_PORT:-3000}
      LANGFUSE_PUBLIC_KEY: ${LANGFUSE_PUBLIC_KEY}
      LANGFUSE_SECRET_KEY: ${LANGFUSE_SECRET_KEY}
    volumes:
      - ../dist:/var/lib/grafana/plugins/forge-langfuse-datasource:ro
      - ../provisioning:/etc/grafana/provisioning:ro
    healthcheck:
      test: ["CMD-SHELL", "wget -q --spider http://localhost:3000/api/health || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 5
```

- [ ] **Step 3: Create `.env.example`**

```bash
# Langfuse connection — copy to .env and fill in values
LANGFUSE_HOST=host.containers.internal
LANGFUSE_PORT=3000
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

- [ ] **Step 4: Build the plugin**

```bash
npm run build
```

Expected: `dist/` updated with latest code. No errors.

- [ ] **Step 5: Copy .env.example to .env and fill in credentials**

```bash
cp .env.example .env
# Edit .env with actual Langfuse credentials
```

- [ ] **Step 6: Start Grafana**

```bash
podman compose --env-file .env -f devtools/compose.grafana.yml up -d
```

Expected: Container starts. Wait ~10 seconds for Grafana to initialize.

- [ ] **Step 7: Verify datasource is provisioned**

```bash
curl -s http://admin:admin@localhost:3001/api/datasources | python3 -m json.tool | grep '"name"'
```

Expected: `"name": "Langfuse"` appears in output.

- [ ] **Step 8: Verify dashboards are provisioned**

```bash
curl -s http://admin:admin@localhost:3001/api/search | python3 -m json.tool | grep '"title"'
```

Expected: `"title": "LLM Cost Overview"` and `"title": "LLM Performance"` appear in output.

- [ ] **Step 9: Test the datasource connection in Grafana UI**

Open http://localhost:3001 in a browser. Navigate to Connections → Data Sources → Langfuse → click "Test". Expected: "Connected to Langfuse successfully".

- [ ] **Step 10: Verify dashboards render data**

Open http://localhost:3001/dashboards. Open "LLM Cost Overview". Set time range to a period you know has Langfuse data. Panels should render time series (may be empty if no data in range — that's correct behavior, not an error).

- [ ] **Step 11: Commit**

```bash
git add devtools/compose.grafana.yml .env.example
git commit -m "feat: add Podman Compose for Grafana and smoke test complete"
```
