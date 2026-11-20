import { expect, test } from '@playwright/test'

/**
 * 异常分区修复清单：湖上分区页生成清单并归属，Iceberg 管理页查看支付表分区。
 * 同一支付事件只允许一个归属状态；非归属团队只能查看。
 */
test('异常分区修复清单：生成、归属、同步与受控查看', async ({ page }) => {
  test.setTimeout(300_000)
  const browserErrors: string[] = []
  page.on('pageerror', e => browserErrors.push(e.message))
  page.on('response', r => { if (r.url().includes('/api/') && r.status() >= 400) browserErrors.push(`${r.status()} ${r.url()}`) })

  const eventCode = `PAYE-E2E-${Date.now()}`

  await page.goto('/lake_partition')
  await expect(page.getByTestId('repair-panel')).toBeVisible({ timeout: 10_000 })

  // 缺业务日期：进入修复清单，原始时间与目标分区成对保留。
  await page.getByTestId('event-code').fill(eventCode)
  await page.getByTestId('event-ingested-at').fill('2026-11-19T09:05:00')
  await page.getByTestId('submit-event').click()

  const row = page.locator(`tr[data-event="${eventCode}"]`)
  await expect(row).toHaveCount(1)
  await expect(row).toHaveAttribute('data-status', '待修复')
  await expect(row.locator('td').nth(2)).toContainText('2026-11-19T09:05:00')
  await expect(row.locator('td').nth(3)).toContainText('payments/dt=2026-11-19')
  await expect(row.locator('td').nth(5)).toContainText('LAKE-0003@v1')

  // 非归属团队：改动被拦下，状态保持待修复。
  await page.getByTestId('operator-team').selectOption('订单数据组')
  await row.getByRole('button', { name: '归属分区' }).click()
  await expect(page.getByTestId('repair-message')).toContainText('只能查看受控结果')
  await expect(row).toHaveAttribute('data-status', '待修复')

  // 归属团队：推进到已归属，按钮消失且不会重复显示。
  await page.getByTestId('operator-team').selectOption('数据平台组')
  await row.getByRole('button', { name: '归属分区' }).click()
  await expect(row).toHaveAttribute('data-status', '已归属')
  await expect(row.getByTestId(/^assigned-/)).toBeVisible()
  await expect(page.locator(`tr[data-event="${eventCode}"]`)).toHaveCount(1)

  // 刷新湖上分区页：清单保留此前完整结果，不会再次出现在待修复里。
  await page.reload()
  await expect(page.locator(`tr[data-event="${eventCode}"]`)).toHaveAttribute('data-status', '已归属')

  // Iceberg 管理页同步：同一事件只展示一个归属分区。
  await page.goto('/iceberg_table')
  await expect(page.getByTestId('iceberg-partition-panel')).toBeVisible({ timeout: 10_000 })
  await expect(page.getByTestId('partition-source-version')).toContainText('LAKE-0003@v1')
  const partitionRow = page.locator(`tr[data-testid="partition-${eventCode}"]`)
  await expect(partitionRow).toHaveCount(1)
  await expect(partitionRow).toHaveAttribute('data-partition', 'payments/dt=2026-11-19')
  await expect(partitionRow.locator('td').nth(3)).toContainText('2026-11-19T09:05:00')

  expect(browserErrors).toEqual([])
})
