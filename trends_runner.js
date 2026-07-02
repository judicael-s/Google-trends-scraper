#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');

const CONNECTOR = 'google_trends_playwright_windows';
const SUPPORTED_TIMEFRAMES = new Set(['now 1-d', 'now 7-d', 'today 1-m', 'today 3-m', 'today 12-m', 'today 5-y', 'all']);

function parseArgs(argv) {
  const args = {
    queries: [],
    geo: '',
    hl: 'fr-FR',
    timeframe: 'today 12-m',
    regionResolution: 'REGION',
    fixture: '',
    timeoutMs: 60000,
    userDataDir: path.join(process.env.TEMP || process.env.TMP || '.', 'seo-trends-playwright-profile'),
    browserChannel: 'chrome',
    headless: false,
    keepOpenMs: 2500,
    keepOpenOnErrorMs: 0,
  };
  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => {
      i += 1;
      if (i >= argv.length) throw new Error(`Missing value for ${arg}`);
      return argv[i];
    };
    if (arg === '--query' || arg === '-q') args.queries.push(next());
    else if (arg === '--queries') args.queries.push(...next().split(',').map((q) => q.trim()).filter(Boolean));
    else if (arg === '--geo') args.geo = next().toUpperCase();
    else if (arg === '--hl') args.hl = next();
    else if (arg === '--timeframe' || arg === '--date') args.timeframe = next();
    else if (arg === '--region-resolution') args.regionResolution = next().toUpperCase();
    else if (arg === '--fixture') args.fixture = next();
    else if (arg === '--timeout-ms') args.timeoutMs = Number(next());
    else if (arg === '--user-data-dir') args.userDataDir = next();
    else if (arg === '--browser-channel') args.browserChannel = next();
    else if (arg === '--headless') args.headless = true;
    else if (arg === '--keep-open-ms') args.keepOpenMs = Number(next());
    else if (arg === '--keep-open-on-error-ms') args.keepOpenOnErrorMs = Number(next());
    else if (arg === '--help' || arg === '-h') {
      printHelp();
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  if (!SUPPORTED_TIMEFRAMES.has(args.timeframe)) {
    throw new Error(`Unsupported timeframe: ${args.timeframe}. Supported: ${Array.from(SUPPORTED_TIMEFRAMES).join(', ')}`);
  }
  if (!['COUNTRY', 'REGION', 'CITY'].includes(args.regionResolution)) {
    throw new Error('--region-resolution must be COUNTRY, REGION, or CITY');
  }
  if (!Number.isFinite(args.timeoutMs) || args.timeoutMs < 5000) throw new Error('--timeout-ms must be >= 5000');
  if (!Number.isFinite(args.keepOpenMs) || args.keepOpenMs < 0) throw new Error('--keep-open-ms must be >= 0');
  if (!Number.isFinite(args.keepOpenOnErrorMs) || args.keepOpenOnErrorMs < 0) throw new Error('--keep-open-on-error-ms must be >= 0');
  return args;
}

function printHelp() {
  console.log(`Usage: node trends_runner.js --query "keyword" [options]\n\nWindows-side Google Trends runner. Uses a persistent Chrome/Edge profile to avoid suspicious clean WSL/headless sessions.\n\nOptions:\n  --query, -q              Query; repeat for comparisons\n  --queries                Comma-separated queries\n  --geo                    Trends geo code, e.g. FR, US, GB; empty = worldwide\n  --hl                     Interface language, e.g. fr-FR, en-US\n  --timeframe, --date      now 1-d | now 7-d | today 1-m | today 3-m | today 12-m | today 5-y | all\n  --region-resolution      COUNTRY | REGION | CITY\n  --browser-channel        chrome | msedge (default chrome)\n  --user-data-dir          Persistent browser profile directory\n  --headless               Not recommended; visible persistent browser is safer\n  --fixture                Offline fixture JSON instead of live browser\n  --timeout-ms             Navigation timeout (default 60000)\n  --keep-open-ms           Extra wait after load for widgets (default 2500)\n  --keep-open-on-error-ms  Keep browser open after rate-limit/no-data for manual warmup/debug\n`);
}

function safeJsonFromGoogle(text) {
  const trimmed = String(text || '').trim();
  const firstBrace = trimmed.search(/[\[{]/);
  if (firstBrace === -1) throw new Error('Google Trends response did not contain JSON');
  return JSON.parse(trimmed.slice(firstBrace));
}

function valuesForIndex(value, index) {
  if (Array.isArray(value)) return value[index] ?? null;
  if (index === 0 && typeof value === 'number') return value;
  return null;
}

function summarize(points) {
  const values = points.map((p) => p.value).filter((v) => typeof v === 'number' && Number.isFinite(v));
  if (!values.length) return { points: points.length, mean_value: null, latest_value: null, max_value: null, trend_delta: null };
  const latest = values[values.length - 1];
  const first = values[0];
  const mean = values.reduce((a, b) => a + b, 0) / values.length;
  return { points: points.length, mean_value: Number(mean.toFixed(2)), latest_value: latest, max_value: Math.max(...values), trend_delta: latest - first };
}

function extractRelated(widgetData) {
  const out = [];
  const rankedLists = (((widgetData || {}).default || {}).rankedList) || [];
  for (const list of rankedLists) {
    const type = list.rankedKeyword && list.rankedKeyword.some((kw) => kw.formattedValue === 'Breakout') ? 'rising' : 'top';
    for (const kw of (list.rankedKeyword || [])) out.push({ query: kw.query, value: kw.value ?? kw.formattedValue ?? null, type });
  }
  return out;
}

function normalizePayload(raw, args, mode) {
  const params = {
    queries: args.queries.length ? args.queries : (raw.params && raw.params.queries) || [],
    geo: args.geo || (raw.params && raw.params.geo) || '',
    hl: args.hl || (raw.params && raw.params.hl) || 'fr-FR',
    timeframe: args.timeframe || (raw.params && raw.params.timeframe) || 'today 12-m',
    region_resolution: args.regionResolution || (raw.params && raw.params.region_resolution) || 'REGION',
  };
  const warnings = Array.isArray(raw.warnings) ? [...raw.warnings] : [];
  const timeline = raw.timelineData || raw.interest_over_time || [];
  const geoMap = raw.geoMapData || raw.interest_by_region || [];
  const related = raw.relatedQueries || raw.related_queries || [];

  if (!params.queries.length) warnings.push({ code: 'NO_QUERIES', message: 'No query was supplied or discovered.' });
  if (!Array.isArray(timeline) || timeline.length === 0) warnings.push({ code: 'NO_TIMELINE_DATA', message: 'No interest_over_time timeline was captured. This can be true low volume, a Google UI change, or rate limiting if no explicit 429 was detected.' });
  if (!Array.isArray(geoMap) || geoMap.length === 0) warnings.push({ code: 'NO_REGION_DATA', message: 'No interest_by_region data was captured.' });

  const rows = params.queries.map((query, queryIndex) => {
    const points = (Array.isArray(timeline) ? timeline : []).map((point) => ({
      time: point.time || point.timestamp || null,
      formatted_time: point.formattedTime || point.formatted_time || point.date || null,
      value: valuesForIndex(point.value, queryIndex),
      is_partial: Boolean(point.isPartial || point.is_partial),
    })).filter((point) => point.value !== null || point.formatted_time || point.time);
    const regions = (Array.isArray(geoMap) ? geoMap : []).map((region) => ({
      region: region.geoName || region.region || region.name || null,
      geo_code: region.geoCode || region.geo_code || null,
      value: valuesForIndex(region.value, queryIndex),
    })).filter((region) => region.value !== null || region.region || region.geo_code);
    const relatedQueries = (Array.isArray(related) ? related : [])
      .filter((item) => !item.query_index || item.query_index === queryIndex)
      .map((item) => ({ query: item.query || item.title || null, value: item.value ?? null, type: item.type || item.rising || 'related' }))
      .filter((item) => item.query);
    const summary = summarize(points);
    summary.region_count = regions.length;
    summary.related_query_count = relatedQueries.length;
    return {
      query,
      geo: params.geo,
      hl: params.hl,
      timeframe: params.timeframe,
      region_resolution: params.region_resolution,
      interest_over_time: points,
      interest_by_region: regions,
      related_queries: relatedQueries,
      summary,
      validation_status: 'trends_ideation_only',
      next_safe_check_window: 'respect cache and slow watcher budget before repeating this query/geo/timeframe',
    };
  });
  return { connector: CONNECTOR, mode, fetched_at: new Date().toISOString(), params, rows, warnings, errors: [] };
}

async function runLive(args) {
  let chromium;
  try {
    ({ chromium } = require('playwright'));
  } catch (error) {
    return {
      connector: CONNECTOR,
      mode: 'live',
      fetched_at: new Date().toISOString(),
      params: { queries: args.queries, geo: args.geo, hl: args.hl, timeframe: args.timeframe, region_resolution: args.regionResolution },
      rows: [],
      warnings: [],
      errors: [{ code: 'PLAYWRIGHT_NOT_INSTALLED_WINDOWS', message: 'Install Playwright in tools/windows-trends-runner with npm install.', context: { original_error: error.message } }],
    };
  }
  if (!args.queries.length) throw new Error('At least one --query is required for live mode.');

  const captured = { timelineData: [], geoMapData: [], relatedQueries: [], warnings: [] };
  let context;
  try {
    context = await chromium.launchPersistentContext(args.userDataDir, {
      channel: args.browserChannel,
      headless: args.headless,
      locale: args.hl,
      viewport: { width: 1365, height: 900 },
      args: ['--disable-blink-features=AutomationControlled'],
    });
  } catch (error) {
    return {
      connector: CONNECTOR,
      mode: 'live',
      fetched_at: new Date().toISOString(),
      params: { queries: args.queries, geo: args.geo, hl: args.hl, timeframe: args.timeframe, region_resolution: args.regionResolution },
      rows: [],
      warnings: [],
      errors: [{ code: 'WINDOWS_BROWSER_LAUNCH_FAILED', message: 'Could not launch persistent Windows browser. Check --browser-channel and close existing Chrome windows using the same profile.', context: { original_error: error.message, browser_channel: args.browserChannel, user_data_dir: args.userDataDir } }],
    };
  }

  try {
    const page = await context.newPage();
    page.on('response', async (response) => {
      const url = response.url();
      if (!url.includes('/trends/api/widgetdata/')) return;
      try {
        const text = await response.text();
        const json = safeJsonFromGoogle(text);
        if (url.includes('/multiline')) captured.timelineData = (((json || {}).default || {}).timelineData) || [];
        else if (url.includes('/comparedgeo')) captured.geoMapData = (((json || {}).default || {}).geoMapData) || [];
        else if (url.includes('/relatedsearches')) captured.relatedQueries = extractRelated(json);
      } catch (error) {
        captured.warnings.push({ code: 'WIDGET_PARSE_FAILED', message: error.message, context: { url } });
      }
    });

    const url = new URL('https://trends.google.com/trends/explore');
    url.searchParams.set('date', args.timeframe);
    if (args.geo) url.searchParams.set('geo', args.geo);
    url.searchParams.set('q', args.queries.join(','));
    url.searchParams.set('hl', args.hl);
    const mainResponse = await page.goto(url.toString(), { waitUntil: 'domcontentloaded', timeout: args.timeoutMs });
    await page.waitForTimeout(args.keepOpenMs);
    const status = mainResponse ? mainResponse.status() : null;
    const title = await page.title().catch(() => '');
    const bodyText = await page.locator('body').innerText({ timeout: 3000 }).catch(() => '');
    if (status === 429 || title.includes('429') || bodyText.includes('Too Many Requests')) {
      if (args.keepOpenOnErrorMs > 0) await page.waitForTimeout(args.keepOpenOnErrorMs);
      return {
        connector: CONNECTOR,
        mode: 'live',
        fetched_at: new Date().toISOString(),
        params: { queries: args.queries, geo: args.geo, hl: args.hl, timeframe: args.timeframe, region_resolution: args.regionResolution },
        rows: [],
        warnings: [],
        errors: [{ code: 'GOOGLE_TRENDS_RATE_LIMITED', message: 'Google Trends returned 429 / Too Many Requests. Do not treat this as no demand.', context: { status, title } }],
      };
    }
    const payload = normalizePayload(captured, args, 'windows-live');
    payload.page = { title, url: page.url(), status };
    return payload;
  } finally {
    await context.close();
  }
}

async function main() {
  const args = parseArgs(process.argv);
  if (args.fixture) {
    const raw = JSON.parse(fs.readFileSync(args.fixture, 'utf8'));
    console.log(JSON.stringify(normalizePayload(raw, args, 'windows-fixture'), null, 2));
    return;
  }
  console.log(JSON.stringify(await runLive(args), null, 2));
}

main().catch((error) => {
  console.log(JSON.stringify({ connector: CONNECTOR, mode: 'error', fetched_at: new Date().toISOString(), rows: [], warnings: [], errors: [{ code: 'WINDOWS_RUNNER_FAILED', message: error.message }] }, null, 2));
  process.exitCode = 1;
});
