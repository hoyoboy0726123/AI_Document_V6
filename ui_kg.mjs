import { createRequire } from 'module';
import { mkdirSync } from 'fs';
const require = createRequire('C:/Users/G635LXG/AppData/Roaming/npm/node_modules/');
const { chromium } = require('playwright');

const OUT = 'C:/Users/G635LXG/Downloads/RAG/AI_Document_V3/.shots';
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
page.on('console', m => { if (m.type() === 'error') console.log('PAGE ERR:', m.text().slice(0, 200)); });

try {
  await page.goto('http://localhost:5175/', { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(600);
  // login
  await page.fill('#login_username', 'admin');
  await page.fill('#login_password', 'Admin@123');
  await page.click('button[type="submit"]');
  await page.waitForTimeout(2500);
  console.log('after login url=', page.url());

  // go to knowledge graph page
  await page.goto('http://localhost:5175/knowledge-graph', { waitUntil: 'networkidle', timeout: 30000 });
  // let the graph fetch + layout settle
  await page.waitForTimeout(6000);
  await page.screenshot({ path: `${OUT}/kg_graph.png`, fullPage: true });
  console.log('kg screenshot done, url=', page.url());
} catch (e) {
  console.error('ERROR:', e.message);
  await page.screenshot({ path: `${OUT}/kg_error.png`, fullPage: true }).catch(()=>{});
} finally {
  await browser.close();
}
