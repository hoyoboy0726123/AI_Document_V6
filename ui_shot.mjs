import { createRequire } from 'module';
import { mkdirSync } from 'fs';
const require = createRequire('C:/Users/G635LXG/AppData/Roaming/npm/node_modules/');
const { chromium } = require('playwright');

const OUT = 'C:/Users/G635LXG/Downloads/RAG/AI_Document_V3/.shots';
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

page.on('console', m => console.log('PAGE:', m.type(), m.text()));

try {
  console.log('goto login');
  await page.goto('http://localhost:5175/', { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${OUT}/01_login.png`, fullPage: true });
  console.log('login screenshot done');

  await page.fill('#login_username', 'admin');
  await page.fill('#login_password', 'Admin@123');
  await page.click('button[type="submit"]');

  // wait for navigation away from login
  await page.waitForLoadState('networkidle').catch(()=>{});
  await page.waitForTimeout(3000);
  await page.screenshot({ path: `${OUT}/02_home.png`, fullPage: true });
  console.log('home screenshot done, url=', page.url());
} catch (e) {
  console.error('ERROR:', e.message);
  await page.screenshot({ path: `${OUT}/99_error.png`, fullPage: true }).catch(()=>{});
} finally {
  await browser.close();
}
