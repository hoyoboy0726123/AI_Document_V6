import { createRequire } from 'module';
const require = createRequire('C:/Users/G635LXG/AppData/Roaming/npm/node_modules/');
const { chromium } = require('playwright');
const OUT = 'C:/Users/G635LXG/Downloads/RAG/AI_Document_V3/.shots/deck';
const F = 'http://localhost:5175';
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1300 }, deviceScaleFactor: 1.5 });
try {
  await page.goto(F + '/login', { waitUntil: 'networkidle' }); await page.waitForTimeout(1200);
  await page.fill('#login_username', 'admin'); await page.fill('#login_password', 'Admin@123');
  await page.click('button[type="submit"]'); await page.waitForTimeout(2500);
  await page.goto(F + '/admin/metadata', { waitUntil: 'networkidle' }); await page.waitForTimeout(2000);
  await page.getByText('系統設置', { exact: true }).first().click().catch(()=>{});
  await page.waitForTimeout(6500); // let the settings form load
  await page.screenshot({ path: `${OUT}/09b_settings.png`, fullPage: true });
  console.log('done settings');
} catch (e) { console.error('ERROR:', e.message); }
finally { await browser.close(); }
