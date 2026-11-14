<template>
  <section class="page" data-module="impact">
    <header class="page-head">
      <div>
        <h2>口径影响分析</h2>
        <p class="page-desc">
          指标换版后按 metric_id、grain_hash、baseline_uri、tolerance 计算影响键，
          只重验命中影响键的任务，未命中的沿用历史完整结论。
        </p>
      </div>
      <div class="page-actions">
        <select v-model="metricId" class="select" @change="onMetricChange">
          <option v-for="item in metricOptions" :key="item.id" :value="item.code">
            {{ item.code }} · {{ item.name }}
          </option>
        </select>
        <select v-model="revision" class="select">
          <option v-for="item in revisionOptions" :key="item" :value="item">{{ item }}</option>
        </select>
        <button class="btn primary" type="button" :disabled="busy" @click="analyze">生成影响分析</button>
        <button class="btn" type="button" :disabled="busy || !runId" @click="execute">执行受影响任务</button>
      </div>
    </header>

    <p v-if="message" class="notice">{{ message }}</p>
    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <div v-if="detail" class="stat-row">
      <article v-for="card in countCards" :key="card.key" class="stat-card" :data-set="card.key">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>

    <template v-if="detail">
      <section class="panel">
        <h3>影响键与版本</h3>
        <dl class="kv-grid">
          <div><dt>impact_run</dt><dd>{{ detail.run_id }}</dd></div>
          <div><dt>metric_id</dt><dd>{{ detail.impact_key_fields?.metric_id }}</dd></div>
          <div><dt>metric_revision</dt><dd>{{ detail.metric_revision }} ← {{ detail.previous_revision || '—' }}</dd></div>
          <div><dt>grain_hash</dt><dd>{{ detail.impact_key_fields?.grain_hash }}</dd></div>
          <div><dt>baseline_uri</dt><dd>{{ detail.impact_key_fields?.baseline_uri }}</dd></div>
          <div><dt>tolerance</dt><dd>{{ detail.impact_key_fields?.tolerance }}</dd></div>
          <div><dt>baseline_digest</dt><dd>{{ detail.baseline_digest }}</dd></div>
          <div><dt>变化字段</dt><dd>{{ (detail.changed_fields || []).join('、') || '无' }}</dd></div>
          <div><dt>run 状态</dt><dd>{{ detail.status }} · 执行 {{ detail.execution_count }} 次</dd></div>
        </dl>
      </section>

      <section class="panel-grid">
        <article v-for="group in groups" :key="group.key" class="panel" :data-set="group.key">
          <h3>{{ group.label }} <span class="tag">{{ group.rows.length }}</span></h3>
          <p class="panel-hint">{{ group.hint }}</p>
          <table class="data-table compact">
            <thead>
              <tr><th>任务</th><th>结论</th><th>结论版本</th><th>依据</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in group.rows" :key="`${group.key}-${row.task_code}`">
                <td>{{ row.task_code }}</td>
                <td>{{ row.verdict }}</td>
                <td>{{ row.conclusion_revision || '—' }}</td>
                <td class="reason-cell">
                  {{ row.reason }}
                  <span v-if="row.failure" class="error-text">（{{ row.failure }}）</span>
                </td>
              </tr>
              <tr v-if="!group.rows.length">
                <td colspan="4" class="empty-state">该集合为空</td>
              </tr>
            </tbody>
          </table>
        </article>
      </section>

      <section class="panel">
        <h3>样本结论（02-29 / 跨月首日 / 空地区）</h3>
        <table class="data-table compact">
          <thead>
            <tr><th>任务</th><th>样本</th><th>类型</th><th>结论</th><th>期望</th><th>观测</th><th>补数</th></tr>
          </thead>
          <tbody>
            <template v-for="row in sampleRows" :key="`${row.task_code}-${row.sample.sample_id}`">
              <tr>
                <td>{{ row.task_code }}</td>
                <td>{{ row.sample.label || row.sample.sample_id }}</td>
                <td>{{ row.sample.kind }}</td>
                <td>{{ row.sample.verdict }}</td>
                <td>{{ row.sample.expected ?? '—' }}</td>
                <td>{{ row.sample.observed ?? '—' }}</td>
                <td>
                  <template v-if="row.sample.state === 'gap'">
                    <input v-model="backfillValue[row.sample.sample_id]" class="inline-input" placeholder="观测值" />
                    <button class="link" type="button" @click="backfill(row.task_code, row.sample.sample_id)">
                      登记补数
                    </button>
                  </template>
                  <span v-else>—</span>
                </td>
              </tr>
            </template>
            <tr v-if="!sampleRows.length">
              <td colspan="7" class="empty-state">执行后在这里查看样本级结论</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="panel">
        <h3>快照边界</h3>
        <dl class="kv-grid">
          <div>
            <dt>对外生效（active）</dt>
            <dd>{{ detail.snapshots?.authoritative_active?.snapshot_id || '—' }} ·
              {{ detail.snapshots?.authoritative_active?.metric_revision || '—' }}</dd>
          </div>
          <div>
            <dt>本次构建（building）</dt>
            <dd>{{ detail.snapshots?.building?.snapshot_id || '无' }} ·
              {{ detail.snapshots?.building?.state || '—' }}</dd>
          </div>
          <div>
            <dt>已被替代（superseded）</dt>
            <dd>
              {{ (detail.snapshots?.superseded || []).map((item) => `${item.snapshot_id}@${item.metric_revision}`).join('、') || '无' }}
            </dd>
          </div>
        </dl>
      </section>
    </template>

    <footer class="page-foot">
      <span>结论均绑定 metric_revision；未命中影响键的任务不生成新记录。</span>
      <span>impact_run：{{ runId || '未生成' }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type ImpactItem = {
  task_code: string
  task_name?: string
  verdict: string
  metric_revision: string | null
  conclusion_revision: string | null
  reason: string
  failure?: string
  sample_results?: SampleResult[]
}

type SampleResult = {
  sample_id: string
  label?: string
  kind?: string
  state: string
  verdict: string
  expected?: number | null
  observed?: number | null
}

type SnapshotItem = { snapshot_id: string; state: string; metric_revision: string; items?: unknown[] }

type ImpactDetail = {
  run_id: string
  metric_id: string
  metric_revision: string
  previous_revision: string | null
  impact_key: string
  impact_key_fields: Record<string, string>
  baseline_digest: string
  changed_fields: string[]
  status: string
  execution_count: number
  counts: Record<string, number>
  sets: { affected: ImpactItem[]; reused: ImpactItem[]; pending: ImpactItem[]; abnormal: ImpactItem[] }
  snapshots?: {
    building: SnapshotItem | null
    authoritative_active: SnapshotItem | null
    superseded: SnapshotItem[]
  }
}

const ENDPOINT = '/api/impact'
const metricOptions = ref<{ id: number; code: string; name: string }[]>([])
const revisionOptions = ref<string[]>([])
const metricId = ref('')
const revision = ref('')
const detail = ref<ImpactDetail | null>(null)
const runId = ref('')
const message = ref('')
const errorMessage = ref('')
const busy = ref(false)
const backfillValue = ref<Record<string, string>>({})

const countLabels: Record<string, string> = {
  affected: '受影响（重验）',
  reused: '已复用（沿用旧结论）',
  pending: '待完成',
  abnormal: '异常',
}

const countCards = computed(() =>
  Object.entries(countLabels).map(([key, label]) => ({
    key,
    label,
    value: detail.value?.counts?.[key] ?? 0,
  })),
)

const groups = computed(() => {
  const sets = detail.value?.sets
  return [
    { key: 'affected', label: 'affected', hint: '影响键命中，需要重新执行验证', rows: sets?.affected ?? [] },
    { key: 'reused', label: 'reused', hint: '影响键未命中，沿用历史完整结论', rows: sets?.reused ?? [] },
    { key: 'pending', label: 'pending', hint: '尚无当前版本完整结论，不能标成已验证', rows: sets?.pending ?? [] },
    { key: 'abnormal', label: 'abnormal', hint: '样本差异超过阈值或已发现异常样本', rows: sets?.abnormal ?? [] },
  ]
})

const sampleRows = computed(() => {
  const seen = new Set<string>()
  const rows: { task_code: string; sample: SampleResult }[] = []
  for (const item of detail.value?.sets?.affected ?? []) {
    for (const sample of item.sample_results ?? []) {
      const key = `${item.task_code}-${sample.sample_id}`
      if (seen.has(key)) continue
      seen.add(key)
      rows.push({ task_code: item.task_code, sample })
    }
  }
  return rows
})

function onMetricChange() {
  const picked = metricOptions.value.find((item) => item.code === metricId.value)
  if (picked) void loadRevisions(picked.code)
}

async function loadMetrics() {
  try {
    const payload = await fetchJson<{ items: Record<string, unknown>[] }>('/api/metric?size=200')
    metricOptions.value = payload.items
      .map((item) => ({
        id: Number(item.id),
        code: String(item['指标编号'] ?? ''),
        name: String(item['指标名称'] ?? ''),
      }))
      .filter((item) => item.code)
    const preferred = metricOptions.value.find((item) => item.code === 'METR-ORDER_COUNT')
    metricId.value = preferred?.code ?? metricOptions.value[0]?.code ?? ''
    if (metricId.value) await loadRevisions(metricId.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '指标列表读取失败'
  }
}

async function loadRevisions(code: string) {
  revisionOptions.value = []
  revision.value = ''
  if (!code) return
  try {
    const payload = await fetchJson<{ items: { metric_revision: string }[] }>(
      `${ENDPOINT}/revisions/${encodeURIComponent(code)}`,
    )
    revisionOptions.value = payload.items.map((item) => item.metric_revision)
    revision.value = revisionOptions.value[revisionOptions.value.length - 1] ?? ''
  } catch {
    revisionOptions.value = []
  }
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    throw new Error(String((payload as { detail?: string }).detail ?? `接口返回 ${response.status}`))
  }
  return (await response.json()) as T
}

async function analyze() {
  busy.value = true
  errorMessage.value = ''
  message.value = ''
  try {
    const payload = await postJson<{ ok: boolean; message: string; impact: ImpactDetail }>(
      `${ENDPOINT}/analyze`,
      { values: { metric_id: metricId.value, metric_revision: revision.value || undefined } },
    )
    if (!payload.ok) throw new Error(payload.message)
    detail.value = payload.impact
    runId.value = payload.impact.run_id
    message.value = payload.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '影响分析失败'
  } finally {
    busy.value = false
  }
}

async function execute() {
  busy.value = true
  errorMessage.value = ''
  try {
    const payload = await postJson<{ ok: boolean; message: string; impact: ImpactDetail }>(
      `${ENDPOINT}/runs/${encodeURIComponent(runId.value)}/execute`,
      { values: {} },
    )
    if (payload.impact) detail.value = payload.impact
    message.value = payload.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '验证执行失败'
  } finally {
    busy.value = false
  }
}

async function backfill(taskCode: string, sampleId: string) {
  const raw = backfillValue.value[sampleId]
  const observed = Number(raw)
  if (raw === undefined || raw === '' || Number.isNaN(observed)) {
    errorMessage.value = '补数需要填写数值观测值'
    return
  }
  errorMessage.value = ''
  try {
    const payload = await postJson<{ ok: boolean; message: string }>(
      `${ENDPOINT}/runs/${encodeURIComponent(runId.value)}/backfill`,
      { values: { task_code: taskCode, sample_id: sampleId, observed } },
    )
    message.value = payload.message
    if (payload.ok) backfillValue.value[sampleId] = ''
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '补数登记失败'
  }
}

onMounted(loadMetrics)
</script>

<style scoped>
.select { border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; background: #fff; }
.notice { background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 6px; padding: 8px 10px; font-size: 13px; }
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin-bottom: 12px; }
.panel h3 { margin: 0 0 8px; font-size: 14px; }
.panel-hint { color: var(--muted); font-size: 12px; margin: 0 0 8px; }
.panel-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.panel-grid .panel { margin-bottom: 12px; }
.panel[data-set='affected'] { border-left: 3px solid #d97706; }
.panel[data-set='reused'] { border-left: 3px solid #16a34a; }
.panel[data-set='pending'] { border-left: 3px solid #2563eb; }
.panel[data-set='abnormal'] { border-left: 3px solid #b42318; }
.kv-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px 16px; margin: 0; }
.kv-grid dt { color: var(--muted); font-size: 12px; }
.kv-grid dd { margin: 2px 0 0; font-size: 13px; word-break: break-all; }
.data-table.compact th, .data-table.compact td { padding: 6px 8px; font-size: 12px; }
.tag { background: #eef2ff; color: #3730a3; border-radius: 10px; padding: 1px 8px; font-size: 12px; }
.reason-cell { color: var(--muted); }
.inline-input { width: 80px; border: 1px solid var(--border); border-radius: 4px; padding: 2px 6px; margin-right: 6px; }
</style>
