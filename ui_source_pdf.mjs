import { createRequire } from 'module';
import { mkdirSync } from 'fs';
const require = createRequire('C:/Users/G635LXG/AppData/Roaming/npm/node_modules/');
const { chromium } = require('playwright');
const OUT = 'C:/Users/G635LXG/Downloads/RAG/AI_Document_V3/.shots';
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1500, height: 1300 } });
page.on('console', m => { if (m.type() === 'error') console.log('PAGE ERR:', m.text().slice(0, 140)); });
try {
  await page.goto('http://localhost:5175/', { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(500);
  await page.fill('#login_username', 'admin');
  await page.fill('#login_password', 'Admin@123');
  await page.click('button[type="submit"]');
  await page.waitForTimeout(2500);
  await page.goto('http://localhost:5175/qa', { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(1200);

  // clear history for a clean view
  const clr = page.getByRole('button', { name: /清除|清空/ });
  if (await clr.count()) { await clr.first().click().catch(()=>{}); await page.waitForTimeout(300);
    const ok = page.getByRole('button', { name: /^(確定|確認|OK)$/ }); if (await ok.count()) await ok.first().click().catch(()=>{}); await page.waitForTimeout(500); }

  await page.fill('textarea', '鹽霧測試的鹽水濃度與溫度條件是多少?');
  await page.getByRole('button', { name: /送出查詢/ }).click();
  console.log('submitted, waiting for sources…');
  // wait for a 預覽 button (sources rendered). antd inserts a space between the 2 CJK chars → "預 覽"
  const previewBtn = page.getByRole('button', { name: /預\s*覽/ });
  await previewBtn.first().waitFor({ state: 'visible', timeout: 180000 });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${OUT}/src_answer.png`, fullPage: true });
  console.log('answer+sources rendered, screenshot src_answer.png');

  // click the first 預覽
  await previewBtn.first().click();
  console.log('clicked 預覽, waiting for PDF render…');
  // wait for react-pdf canvas (the page actually rendered)
  await page.waitForSelector('.react-pdf__Page__canvas, canvas', { timeout: 60000 }).catch(()=>console.log('canvas wait timeout'));
  await page.waitForTimeout(6000);  // give pdf.js time to paint the page
  const pageLabel = await page.evaluate(() => {
    const m = document.body.innerText.match(/第\s*\d+\s*\/\s*\d+\s*頁|Page\s*\d+|\d+\s*\/\s*\d+/);
    return m ? m[0] : '(page label not found)';
  });
  console.log('PDF modal page label:', pageLabel);
  await page.screenshot({ path: `${OUT}/src_pdf.png`, fullPage: true });
  console.log('done');
} catch (e) {
  console.error('ERROR:', e.message);
  await page.screenshot({ path: `${OUT}/src_err.png`, fullPage: true }).catch(()=>{});
} finally { await browser.close(); }
