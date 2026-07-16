import { createRequire } from 'module';
import { mkdirSync } from 'fs';
const require = createRequire('C:/Users/G635LXG/AppData/Roaming/npm/node_modules/');
const { chromium } = require('playwright');
const OUT = 'C:/Users/G635LXG/Downloads/RAG/AI_Document_V3/.shots/deck';
mkdirSync(OUT, { recursive: true });
const F = 'http://localhost:5175';
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1100 }, deviceScaleFactor: 1.5 });
const shot = (n) => page.screenshot({ path: `${OUT}/${n}.png`, fullPage: true }).then(()=>console.log('shot', n));
try {
  await page.goto(F + '/login', { waitUntil: 'networkidle' }); await page.waitForTimeout(1200);
  await page.fill('#login_username', 'admin'); await page.fill('#login_password', 'Admin@123');
  await page.click('button[type="submit"]'); await page.waitForTimeout(2500);

  // 系統設置 tab
  await page.goto(F + '/admin/metadata', { waitUntil: 'networkidle' }); await page.waitForTimeout(2000);
  const tab = page.getByText('系統設置', { exact: true });
  if (await tab.count()) { await tab.first().click().catch(()=>{}); await page.waitForTimeout(2000); }
  await shot('09b_settings');
  // RAG 提示詞 tab
  const tab2 = page.getByText('RAG 提示詞', { exact: false });
  if (await tab2.count()) { await tab2.first().click().catch(()=>{}); await page.waitForTimeout(1800); await shot('09c_prompts'); }

  // 關係查詢結果（TO-BE 用）：Agent 模式問 510.7 引用哪些規範
  await page.goto(F + '/qa', { waitUntil: 'networkidle' }); await page.waitForTimeout(1500);
  const clr = page.getByRole('button', { name: /清除|清空/ });
  if (await clr.count()) { await clr.first().click().catch(()=>{}); await page.waitForTimeout(300);
    const ok = page.getByRole('button', { name: /^(確定|確認|OK)$/ }); if (await ok.count()) await ok.first().click().catch(()=>{}); await page.waitForTimeout(500); }
  // switch to Agent mode for a clear KG block
  const seg = page.getByText('Agent', { exact: true });
  if (await seg.count()) { await seg.first().click().catch(()=>{}); await page.waitForTimeout(500); }
  await page.fill('textarea', '沙塵測試 Method 510.7 引用了哪些外部規範?');
  await page.getByRole('button', { name: /送出查詢/ }).click();
  await page.waitForFunction(() => /全章節引用的外部規範/.test(document.body.innerText), { timeout: 150000 }).catch(()=>{});
  await page.waitForTimeout(3000);
  await shot('11_rel_answer');
  console.log('done');
} catch (e) { console.error('ERROR:', e.message); }
finally { await browser.close(); }
