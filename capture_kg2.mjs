import { createRequire } from 'module';
const require = createRequire('C:/Users/G635LXG/AppData/Roaming/npm/node_modules/');
const { chromium } = require('playwright');
const OUT = 'C:/Users/G635LXG/Downloads/RAG/AI_Document_V3/.shots/deck';
const F = 'http://localhost:5175';
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 }, deviceScaleFactor: 1.3 });

async function shotGraph(target, hops, name, zoomSteps) {
  const num = page.locator('input[role="spinbutton"]').first();
  if (await num.count()) { await num.click(); await num.fill(String(hops)); await page.keyboard.press('Enter').catch(()=>{}); await page.waitForTimeout(500); }
  const sel = page.locator('.ant-select-selector').first();
  await sel.click(); await page.waitForTimeout(500);
  await page.keyboard.type(target); await page.waitForTimeout(1600);
  const opt = page.locator('.ant-select-item-option').first();
  if (await opt.count()) await opt.click().catch(()=>{});
  await page.waitForTimeout(5500);
  const cv = page.locator('canvas').first();
  const bb = await cv.boundingBox();
  if (bb) {
    await page.mouse.move(bb.x + bb.width*0.5, bb.y + bb.height*0.5);
    for (let i=0;i<zoomSteps;i++){ await page.mouse.wheel(0,-260); await page.waitForTimeout(220); }
    await page.waitForTimeout(1500);
    // clip to the graph panel (exclude sidebar)
    await page.screenshot({ path: `${OUT}/${name}.png`, clip: { x: bb.x+2, y: bb.y+2, width: bb.width-4, height: bb.height-4 } });
  } else {
    await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true });
  }
  console.log('shot', name);
}
try {
  await page.goto(F + '/login', { waitUntil: 'networkidle' }); await page.waitForTimeout(1200);
  await page.fill('#login_username', 'admin'); await page.fill('#login_password', 'Admin@123');
  await page.click('button[type="submit"]'); await page.waitForTimeout(2500);
  await page.goto(F + '/knowledge-graph', { waitUntil: 'networkidle' }); await page.waitForTimeout(3500);
  await page.waitForSelector('canvas', { timeout: 20000 }).catch(()=>{});
  await shotGraph('MIL-PRF-7808', 1, 'kg_clean_7808', 6);
  await shotGraph('MIL-STD-810', 1, 'kg_clean_810fam', 6);
  console.log('done');
} catch (e) { console.error('ERROR:', e.message); }
finally { await browser.close(); }
