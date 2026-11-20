<template>
  <section class="page" data-module="lake_partition">
    <header class="page-head">
      <div>
        <h2>湖上分区管理</h2>
        <p class="page-desc">维护数据湖表的物理分区、桶化与文件组织规则，为湖上分区提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记分区定义</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出湖上分区清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无湖上分区数据，可先登记分区定义</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记分区定义</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条湖上分区记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section class="repair-panel" data-testid="repair-panel">
      <header class="panel-head">
        <div>
          <h3>异常分区修复清单</h3>
          <p class="page-desc">
            支付事件缺少业务日期时不再用当天日期兜底；清单保留原始时间与目标分区，归属后同步 Iceberg 元数据。
          </p>
        </div>
        <div class="page-actions">
          <select v-model="operatorTeam" data-testid="operator-team" aria-label="操作团队">
            <option v-for="team in teams" :key="team" :value="team">{{ team }}</option>
          </select>
          <button class="btn" data-testid="recover" type="button" @click="recover">断点恢复</button>
        </div>
      </header>

      <div class="routing-bar">
        <label class="filter-item">
          <span>缺失业务日期归属口径</span>
          <select v-model="routingRule" data-testid="routing-rule" @change="saveRouting">
            <option v-for="rule in routingRules" :key="rule" :value="rule">{{ rule }}</option>
          </select>
        </label>
        <span class="panel-hint" data-testid="routing-version">来源版本 {{ routingVersion || '—' }}</span>
        <form class="event-form" @submit.prevent="submitEvent">
          <input v-model="eventValues['支付事件编号']" data-testid="event-code" placeholder="支付事件编号" required />
          <input v-model="eventValues['业务日期']" data-testid="event-business-date" placeholder="业务日期 YYYY-MM-DD（可空）" />
          <input v-model="eventValues['接入时间']" data-testid="event-ingested-at" placeholder="接入时间 ISO（可空）" />
          <button class="btn primary" data-testid="submit-event" type="submit">登记支付事件</button>
        </form>
      </div>

      <table class="data-table repair-table">
        <thead>
          <tr>
            <th v-for="column in repairColumns" :key="column">{{ column }}</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in repairs"
            :key="String(row.id)"
            :data-testid="`repair-row-${row.id}`"
            :data-status="String(row.状态)"
            :data-event="String(row.支付事件编号)"
          >
            <td v-for="column in repairColumns" :key="column">{{ row[column] ?? '—' }}</td>
            <td class="row-actions">
              <button
                v-if="row.状态 === '待修复'"
                class="link"
                type="button"
                :data-testid="`assign-${row.id}`"
                :title="canEdit(row) ? '归属分区并同步 Iceberg 元数据' : '其他团队只能查看受控结果'"
                @click="assignRepair(row)"
              >
                归属分区
              </button>
              <span v-else class="panel-hint" :data-testid="`assigned-${row.id}`">已归属，不可重复改写</span>
            </td>
          </tr>
          <tr v-if="!repairs.length">
            <td :colspan="repairColumns.length + 1" class="empty-state">暂无异常分区，支付事件均已按业务日期归属</td>
          </tr>
        </tbody>
      </table>
      <p v-if="repairMessage" class="panel-hint" data-testid="repair-message">{{ repairMessage }}</p>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/lake_partition'
const columns = ["分区编号", "目标表名", "分区列名", "分区粒度", "分区转换", "存储位置", "文件格式", "生命周期"]
const actions = ["提交校验", "标记生效", "停用分区"]
const statuses = ["草稿", "待校验", "已生效", "已停用"]
const metricNames = ["分区定义总数", "待校验定义", "已生效定义"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'lake_partition.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('湖上分区动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '湖上分区操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('分区定义列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '湖上分区列表读取失败'
  }
}

// ── 异常分区修复清单 ──
const REPAIR_ENDPOINT = '/api/partition_repair'
const teams = ['数据平台组', '订单数据组', '用户增长组']
const routingRules = ['按接入时间归属', '按空值隔离']
const repairColumns = ['清单编号', '支付事件编号', '原始时间', '目标分区', '归属口径', '来源版本', '状态']
const repairs = ref<Row[]>([])
const repairMessage = ref('')
const operatorTeam = ref(teams[0])
const routingRule = ref(routingRules[0])
const routingVersion = ref('')
const eventValues = ref<Record<string, string>>({
  支付事件编号: '', 业务日期: '', 接入时间: '', 目标表名: 'payments',
})

function canEdit(row: Row) {
  const owner = row.归属团队
  return !owner || owner === operatorTeam.value
}

async function loadRouting() {
  try {
    const response = await request(`${REPAIR_ENDPOINT}/routing`)
    if (!response.ok) return
    const payload = await response.json()
    routingRule.value = payload.routing_rule ?? routingRules[0]
    routingVersion.value = payload.source_version ?? ''
  } catch {
    routingVersion.value = ''
  }
}

async function saveRouting() {
  repairMessage.value = ''
  try {
    const response = await request(`${REPAIR_ENDPOINT}/routing`, {
      method: 'POST',
      body: JSON.stringify({ 归属口径: routingRule.value, 操作团队: operatorTeam.value }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      repairMessage.value = payload.message || '归属口径调整失败'
      await loadRouting()
      return
    }
    routingVersion.value = payload.entry?.来源版本 ?? routingVersion.value
    repairMessage.value = payload.message
    await reloadRepairs()
  } catch (error) {
    repairMessage.value = error instanceof Error ? error.message : '归属口径调整失败'
  }
}

async function reloadRepairs() {
  try {
    const response = await request(`${REPAIR_ENDPOINT}/repairs?table_name=payments&size=200`)
    if (!response.ok) throw new Error('异常分区修复清单读取失败')
    const payload = await response.json()
    repairs.value = payload.items ?? []
  } catch (error) {
    repairMessage.value = error instanceof Error ? error.message : '异常分区修复清单读取失败'
  }
}

async function submitEvent() {
  repairMessage.value = ''
  try {
    const response = await request(`${REPAIR_ENDPOINT}/events`, {
      method: 'POST', body: JSON.stringify(eventValues.value),
    })
    const payload = await response.json()
    repairMessage.value = payload.message || ''
    if (!response.ok || !payload.ok) return
    eventValues.value = { ...eventValues.value, 支付事件编号: '', 业务日期: '', 接入时间: '' }
    await reloadRepairs()
  } catch (error) {
    repairMessage.value = error instanceof Error ? error.message : '支付事件登记失败'
  }
}

async function assignRepair(row: Row) {
  repairMessage.value = ''
  try {
    const response = await request(`${REPAIR_ENDPOINT}/repairs/${row.id}/assign`, {
      method: 'POST',
      body: JSON.stringify({ 操作团队: operatorTeam.value }),
    })
    const payload = await response.json()
    repairMessage.value = payload.message || ''
    await reloadRepairs()
  } catch (error) {
    repairMessage.value = error instanceof Error ? error.message : '归属分区失败'
  }
}

async function recover() {
  repairMessage.value = ''
  try {
    const response = await request(`${REPAIR_ENDPOINT}/recover`, {
      method: 'POST', body: JSON.stringify({ 目标表名: 'payments' }),
    })
    const payload = await response.json()
    repairMessage.value = payload.message || ''
    await reloadRepairs()
  } catch (error) {
    repairMessage.value = error instanceof Error ? error.message : '断点恢复失败'
  }
}

onMounted(() => {
  void reload()
  void loadRouting()
  void reloadRepairs()
})
</script>
