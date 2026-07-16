import { createRequire } from 'module';
import { mkdirSync } from 'fs';
const require = createRequire('C:/Users/G635LXG/AppData/Roaming/npm/node_modules/');
const { chromium } = require('playwright');
const OUT = 'C:/Users/G635LXG/Downloads/RAG/AI_Document_V3/.shots/deck';
mkdirSync(OUT, { recursive: true });
const F = 'http://localhost:5175';
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1100 }, deviceScaleFactor: 1.5 });

async function centerZoom(target, hops, name, zoomSteps) {
  // set 展開層數 (hops)
  const num = page.locator('input[role="spinbutton"]').first();
  if (await num.count()) { await num.fill(String(hops)).catch(()=>{}); await page.keyboard.press('Enter').catch(()=>{}); await page.waitForTimeout(400); }
  // pick node via search select
  const sel = page.locator('.ant-select-selector').first();
  await sel.click(); await page.waitForTimeout(500);
  await page.keyboard.type(target); await page.waitForTimeout(1600);
  const opt = page.locator('.ant-select-item-option').first();
  if (await opt.count()) { await opt.click().catch(()=>{}); }
  await page.waitForTimeout(5500); // settle
  // zoom in with wheel over the canvas centre so labels (globalScale>1.2) appear
  const cv = page.locator('canvas').first();
  const bb = await cv.boundingBox();
  if (bb) {
    const cx = bb.x + bb.width * 0.62, cy = bb.y + bb.height * 0.5;
    await page.mouse.move(cx, cy);
    for (let i = 0; i < zoomSteps; i++) { await page.mouse.wheel(0, -260); await page.waitForTimeout(220); }
    await page.waitForTimeout(1500);
  }
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: true });
  console.log('shot', name);
}

try {
  await page.goto(F + '/login', { waitUntil: 'networkidle' }); await page.waitForTimeout(1200);
  await page.fill('#login_username', 'admin'); await page.fill('#login_password', 'Admin@123');
  await page.click('button[type="submit"]'); await page.waitForTimeout(2500);
  await page.goto(F + '/knowledge-graph', { waitUntil: 'networkidle' }); await page.waitForTimeout(3500);
  await page.waitForSelector('canvas', { timeout: 20000 }).catch(()=>{});

  await centerZoom('MIL-HDBK-310', 1, 'kg_zoom_310', 7);
  await centerZoom('Method 509.7', 1, 'kg_zoom_509', 8);
  console.log('done');
} catch (e) { console.error('ERROR:', e.message); await page.screenshot({ path: `${OUT}/kg_zoom_err.png`, fullPage: true }).catch(()=>{}); }
finally { await browser.close(); }
