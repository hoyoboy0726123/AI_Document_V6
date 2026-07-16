import { createRequire } from 'module';
import { mkdirSync } from 'fs';
const require = createRequire('C:/Users/G635LXG/AppData/Roaming/npm/node_modules/');
const { chromium } = require('playwright');
const OUT = 'C:/Users/G635LXG/Downloads/RAG/AI_Document_V3/.shots';
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1500, height: 1200 } });
try {
  await page.goto('http://localhost:5175/', { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(500);
  await page.fill('#login_username', 'admin');
  await page.fill('#login_password', 'Admin@123');
  await page.click('button[type="submit"]');
  await page.waitForTimeout(2500);
  await page.goto('http://localhost:5175/qa', { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(1500);

  // clear history for a clean view
  const clr = page.getByRole('button', { name: /清除|清空/ });
  if (await clr.count()) { await clr.first().click().catch(()=>{}); await page.waitForTimeout(300);
    const ok = page.getByRole('button', { name: /^(確定|確認|OK)$/ }); if (await ok.count()) await ok.first().click().catch(()=>{}); await page.waitForTimeout(600); }

  // report which segmented option is selected (should be 混合)
  const sel = await page.locator('.ant-segmented-item-selected').first().innerText().catch(()=>'?');
  console.log('selected mode =', sel.trim());
  await page.screenshot({ path: `${OUT}/hybrid_panel.png`, clip: { x: 470, y: 60, width: 540, height: 360 } });

  // ask a CONTENT question -> should route to RAG
  await page.fill('textarea', '濕度測試怎麼進行?');
  await page.getByRole('button', { name: /送出查詢/ }).click();
  console.log('submitted content question, waiting for route badge…');
  await page.waitForFunction(() => /混合→/.test(document.body.innerText), { timeout: 120000 }).catch(()=>console.log('badge wait timeout'));
  await page.waitForFunction(() => /內容查詢\(RAG\)|關係查詢\(Agent\)/.test(document.body.innerText), { timeout: 120000 }).catch(()=>{});
  // wait until answer text present (not just "思考中")
  await page.waitForFunction(() => /Method 507|濕度|HUMID|耐受/.test(document.body.innerText), { timeout: 120000 }).catch(()=>console.log('answer wait timeout'));
  await page.waitForTimeout(2500);
  const badge = await page.evaluate(() => { const m = document.body.innerText.match(/混合→[^\n]{0,20}/); return m ? m[0] : 'NO BADGE'; });
  console.log('route badge =', badge);
  await page.screenshot({ path: `${OUT}/hybrid_result.png`, fullPage: true });
  console.log('done');
} catch (e) {
  console.error('ERROR:', e.message);
  await page.screenshot({ path: `${OUT}/hybrid_error.png`, fullPage: true }).catch(()=>{});
} finally { await browser.close(); }
