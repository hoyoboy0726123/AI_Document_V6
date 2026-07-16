import { createRequire } from 'module';
import { mkdirSync } from 'fs';
const require = createRequire('C:/Users/G635LXG/AppData/Roaming/npm/node_modules/');
const { chromium } = require('playwright');
const OUT = 'C:/Users/G635LXG/Downloads/RAG/AI_Document_V3/.shots';
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1500, height: 1300 } });
page.on('console', m => { if (m.type() === 'error') {} });
try {
  await page.goto('http://localhost:5175/', { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(500);
  await page.fill('#login_username', 'admin'); await page.fill('#login_password', 'Admin@123');
  await page.click('button[type="submit"]'); await page.waitForTimeout(2500);
  await page.goto('http://localhost:5175/qa', { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(1200);
  const clr = page.getByRole('button', { name: /清除|清空/ });
  if (await clr.count()) { await clr.first().click().catch(()=>{}); await page.waitForTimeout(300);
    const ok = page.getByRole('button', { name: /^(確定|確認|OK)$/ }); if (await ok.count()) await ok.first().click().catch(()=>{}); await page.waitForTimeout(500); }

  await page.fill('textarea', '鹽霧測試的鹽水濃度與溫度條件是多少?');
  await page.getByRole('button', { name: /送出查詢/ }).click();
  const previewBtn = page.getByRole('button', { name: /預\s*覽/ });
  await previewBtn.first().waitFor({ state: 'visible', timeout: 180000 });
  await previewBtn.first().click();
  await page.waitForSelector('.react-pdf__Page__canvas, canvas', { timeout: 60000 }).catch(()=>{});
  await page.waitForTimeout(4000);
  console.log('preview open');

  // add 10 pages: 加入多頁清單 + 下一頁  x10
  const addBtn = page.locator('button:has-text("加入多頁清單")').first();
  for (let i = 0; i < 10; i++) {
    await addBtn.click().catch(()=>{});
    await page.waitForTimeout(500);
    await page.keyboard.press('ArrowRight');
    await page.waitForTimeout(1500);
  }
  const listTxt = await page.evaluate(() => { const m = document.body.innerText.match(/多頁分析列表（\d+\/\d+）/); return m ? m[0] : '?'; });
  console.log('list after 10 adds:', listTxt);
  await page.screenshot({ path: `${OUT}/analyze10_list.png`, fullPage: true });

  await page.locator('button:has-text("多頁分析")').first().click({ force: true });
  console.log('clicked 多頁分析 (10 pages), waiting…');
  await page.getByText(/分析完成/).first().waitFor({ timeout: 240000 }).catch(()=>console.log('分析完成 toast not caught'));
  await page.waitForTimeout(3000);
  const res = await page.evaluate(() => { const t=document.body.innerText; const i=t.lastIndexOf('AI 分析結果'); return (i>=0?t.slice(i,i+500):t.slice(-500)).replace(/\s+/g,' '); });
  console.log('10-PAGE RESULT >>>', res);
  await page.screenshot({ path: `${OUT}/analyze10_result.png`, fullPage: true });
  console.log('done');
} catch (e) {
  console.error('ERROR:', e.message);
  await page.screenshot({ path: `${OUT}/analyze10_err.png`, fullPage: true }).catch(()=>{});
} finally { await browser.close(); }
