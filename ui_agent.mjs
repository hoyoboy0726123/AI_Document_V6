import { createRequire } from 'module';
import { mkdirSync } from 'fs';
const require = createRequire('C:/Users/G635LXG/AppData/Roaming/npm/node_modules/');
const { chromium } = require('playwright');

const OUT = 'C:/Users/G635LXG/Downloads/RAG/AI_Document_V3/.shots';
mkdirSync(OUT, { recursive: true });
const Q = '鹽霧(salt fog)測試怎麼進行?是哪一個 Method?參考/引用了哪些規範?';

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1500, height: 1300 } });
page.on('console', m => { if (m.type() === 'error') console.log('PAGE ERR:', m.text().slice(0, 160)); });

try {
  await page.goto('http://localhost:5175/', { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(600);
  await page.fill('#login_username', 'admin');
  await page.fill('#login_password', 'Admin@123');
  await page.click('button[type="submit"]');
  await page.waitForTimeout(2500);

  await page.goto('http://localhost:5175/qa', { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(1500);

  // enable Agent mode if not already on
  const sw = page.locator('button[role="switch"]').first();
  if (await sw.count()) {
    const on = await sw.getAttribute('aria-checked');
    if (on !== 'true') { await sw.click(); await page.waitForTimeout(500); }
  }
  console.log('agent switch aria-checked=', await sw.getAttribute('aria-checked'));

  // clear prior conversation history for a clean page
  const clearBtn = page.getByRole('button', { name: /清除|清空|刪除歷史/ });
  if (await clearBtn.count()) {
    await clearBtn.first().click().catch(() => {});
    await page.waitForTimeout(400);
    const ok = page.getByRole('button', { name: /^(確定|確認|OK|Yes)$/ });
    if (await ok.count()) { await ok.first().click().catch(() => {}); }
    await page.waitForTimeout(800);
    console.log('cleared history');
  }

  // type question + submit
  await page.fill('textarea', Q);
  await page.waitForTimeout(300);
  await page.getByRole('button', { name: /送出查詢/ }).click();
  console.log('submitted, waiting for agent answer…');

  // wait until the KG reference block or method number renders (agent ~50-70s)
  // "全章節引用的外部規範" only appears in the FINAL deterministic answer (not the live
  // tool-observation steps), and history was cleared → unambiguous signal it finished.
  await page.waitForFunction(
    () => /全章節引用的外部規範/.test(document.body.innerText) &&
          !/Agent 推理中|啟動中/.test(document.body.innerText),
    { timeout: 180000 }
  ).catch(() => console.log('wait timed out, screenshotting anyway'));
  await page.waitForTimeout(2500);

  // extract the rendered answer text (for consistency check vs API)
  const ans = await page.evaluate(() => {
    const t = document.body.innerText;
    const i = t.lastIndexOf('全章節引用的外部規範');
    return i >= 0 ? t.slice(Math.max(0, i - 60), i + 1400) : t.slice(-1700);
  });
  console.log('\n=== RENDERED ANSWER (UI) ===\n' + ans + '\n=== END ===');

  await page.screenshot({ path: `${OUT}/qa_agent.png`, fullPage: true });
  // clipped readable shot of the right answer column
  await page.screenshot({ path: `${OUT}/qa_agent_answer.png`, clip: { x: 470, y: 70, width: 1020, height: 1220 } });
  console.log('screenshots done');
} catch (e) {
  console.error('ERROR:', e.message);
  await page.screenshot({ path: `${OUT}/qa_agent_error.png`, fullPage: true }).catch(() => {});
} finally {
  await browser.close();
}
