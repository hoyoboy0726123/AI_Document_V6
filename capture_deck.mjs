import { createRequire } from 'module';
import { mkdirSync } from 'fs';
const require = createRequire('C:/Users/G635LXG/AppData/Roaming/npm/node_modules/');
const { chromium } = require('playwright');
const OUT = 'C:/Users/G635LXG/Downloads/RAG/AI_Document_V3/.shots/deck';
mkdirSync(OUT, { recursive: true });
const F = 'http://localhost:5175';

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1100 }, deviceScaleFactor: 1.5 });
const shot = async (name) => { await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true }); console.log('shot', name); };
const go = async (path, waitMs = 2200) => { await page.goto(F + path, { waitUntil: 'networkidle', timeout: 30000 }).catch(()=>{}); await page.waitForTimeout(waitMs); };

try {
  // 1) login page (before auth)
  await go('/login', 1500);
  await shot('01_login');

  // login
  await page.fill('#login_username', 'admin').catch(()=>{});
  await page.fill('#login_password', 'Admin@123').catch(()=>{});
  await page.click('button[type="submit"]'); await page.waitForTimeout(2500);

  // 2) QA main interface (empty)
  await go('/qa', 2000);
  const clr = page.getByRole('button', { name: /清除|清空/ });
  if (await clr.count()) { await clr.first().click().catch(()=>{}); await page.waitForTimeout(300);
    const ok = page.getByRole('button', { name: /^(確定|確認|OK)$/ }); if (await ok.count()) await ok.first().click().catch(()=>{}); await page.waitForTimeout(600); }
  await shot('02_qa_main');

  // 5) documents list
  await go('/documents', 2500); await shot('05_documents');
  // 6) build document
  await go('/documents/new', 2200); await shot('06_doc_new');
  // 7) vector search test
  await go('/admin/vector-search', 2200); await shot('07_vector_search');
  // 8) vector health
  await go('/admin/vector-health', 2500); await shot('08_vector_health');
  // 9) admin / settings
  await go('/admin/metadata', 2500); await shot('09_admin');
  // 10) notebook
  await go('/notebook', 2000); await shot('10_notebook');

  // 3) KG full graph
  await go('/knowledge-graph', 3000);
  await page.waitForSelector('canvas', { timeout: 20000 }).catch(()=>console.log('no canvas'));
  await page.waitForTimeout(8000); // let force sim settle
  await shot('03_kg_full');

  // 4) KG centered on a method (focused subgraph with labels)
  const sel = page.locator('.ant-select-selector').first();
  if (await sel.count()) {
    await sel.click().catch(()=>{}); await page.waitForTimeout(600);
    await page.keyboard.type('510.7'); await page.waitForTimeout(1800);
    const opt = page.locator('.ant-select-item-option').first();
    if (await opt.count()) { await opt.click().catch(()=>{}); await page.waitForTimeout(7000); }
    await shot('04_kg_centered');
  }
  console.log('done');
} catch (e) { console.error('ERROR:', e.message); await page.screenshot({ path: `${OUT}/_err.png`, fullPage: true }).catch(()=>{}); }
finally { await browser.close(); }
