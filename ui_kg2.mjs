import { createRequire } from 'module';
import { mkdirSync } from 'fs';
const require = createRequire('C:/Users/G635LXG/AppData/Roaming/npm/node_modules/');
const { chromium } = require('playwright');

const OUT = 'C:/Users/G635LXG/Downloads/RAG/AI_Document_V3/.shots';
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });

try {
  await page.goto('http://localhost:5175/', { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(600);
  await page.fill('#login_username', 'admin');
  await page.fill('#login_password', 'Admin@123');
  await page.click('button[type="submit"]');
  await page.waitForTimeout(2500);

  await page.goto('http://localhost:5175/knowledge-graph', { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(6000);

  const canvas = await page.$('canvas');
  const box = await canvas.boundingBox();
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;

  // Zoom in well past the 1.2x label threshold by scrolling the wheel over the graph.
  await page.mouse.move(cx, cy);
  for (let i = 0; i < 4; i++) {
    await page.mouse.wheel(0, -240);
    await page.waitForTimeout(200);
  }
  await page.waitForTimeout(2500);
  await page.screenshot({ path: `${OUT}/kg_mid.png`, clip: box });
  console.log('mid-zoom screenshot done');
} catch (e) {
  console.error('ERROR:', e.message);
} finally {
  await browser.close();
}
