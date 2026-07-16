import { createRequire } from 'module';
import { mkdirSync } from 'fs';
const require = createRequire('C:/Users/G635LXG/AppData/Roaming/npm/node_modules/');
const { chromium } = require('playwright');
const OUT = 'C:/Users/G635LXG/Downloads/RAG/AI_Document_V3/.shots';
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1500, height: 1300 } });
page.on('console', m => { if (m.type() === 'error') console.log('PAGE ERR:', m.text().slice(0, 120)); });
const waitDone = async (label) => {
  await page.getByText(/分析完成/).first().waitFor({ timeout: 150000 }).catch(() => console.log(`${label}: 分析完成 toast not caught`));
};
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
  await page.waitForTimeout(5000);
  console.log('preview open');

  // === 單頁分析 ===
  await page.getByRole('button', { name: '分析本頁' }).click();
  console.log('clicked 分析本頁, waiting…');
  await waitDone('single');
  await page.waitForTimeout(2500);
  await page.screenshot({ path: `${OUT}/analyze_single.png`, fullPage: true });
  const singleLen = await page.evaluate(() => (document.body.innerText.match(/【圖片說明】|密度|重點/g) || []).length);
  console.log('single-page analysis done, markers=', singleLen);

  // === 多頁分析 ===
  await page.getByRole('button', { name: /加入多頁清單/ }).click(); await page.waitForTimeout(800);
  await page.keyboard.press('ArrowRight'); await page.waitForTimeout(2500);  // next page
  await page.getByRole('button', { name: /加入多頁清單/ }).click(); await page.waitForTimeout(800);
  const listTxt = await page.evaluate(() => { const m = document.body.innerText.match(/多頁分析列表（\d+\/\d+）/); return m ? m[0] : '?'; });
  console.log('multi list:', listTxt);
  const multiBtn = page.locator('button:has-text("多頁分析")').first();
  await multiBtn.scrollIntoViewIfNeeded().catch(()=>{});
  await multiBtn.click({ force: true });
  console.log('clicked 多頁分析, waiting…');
  await waitDone('multi');
  await page.waitForTimeout(3000);
  const multiText = await page.evaluate(() => {
    const t = document.body.innerText; const i = t.lastIndexOf('AI 分析結果');
    return (i >= 0 ? t.slice(i, i + 700) : t.slice(-700)).replace(/\s+/g, ' ');
  });
  console.log('MULTI RESULT >>>', multiText);
  await page.screenshot({ path: `${OUT}/analyze_multi.png`, fullPage: true });
  console.log('done');
} catch (e) {
  console.error('ERROR:', e.message);
  await page.screenshot({ path: `${OUT}/analyze_err.png`, fullPage: true }).catch(()=>{});
} finally { await browser.close(); }
