/* Focused Chromium geometry check: synthetic page, actual content script/CSS,
 * read-only status stub. No extension setup, app tab, API or user profile. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

async function main() {
  const playwright = require(process.env.LIC_PLAYWRIGHT_MODULE);
  const browser = await playwright.chromium.launch({
    executablePath: process.env.LIC_CHROMIUM_EXECUTABLE, headless: true,
    args: ['--disable-background-networking', '--disable-component-update', '--no-first-run'],
  });
  try {
    const context = await browser.newContext({viewport:{width:1280,height:900}});
    const page = await context.newPage();
    await context.route('**/*', route => route.request().url() === 'https://www.youtube.com/shorts/fixture0002'
      ? route.fulfill({contentType:'text/html',body:`<!doctype html><style>
        body{margin:0}ytd-shorts,ytd-reel-video-renderer{display:block;min-height:850px}
        yt-reel-channel-bar-view-model{display:block;position:absolute;top:750px;left:40px}
        reel-action-bar-view-model{display:flex;flex-direction:column;align-items:center;
          position:fixed;right:280px;top:120px;width:48px;transform:translateZ(0)}
        .native{width:48px;height:48px;flex-shrink:0;margin-bottom:12px}
        </style><ytd-shorts><ytd-reel-video-renderer>
        <yt-reel-channel-bar-view-model><a href="/@fixture">Fixture author</a></yt-reel-channel-bar-view-model>
        <reel-action-bar-view-model><button class="native">Like</button><button class="native">Share</button>
        </reel-action-bar-view-model></ytd-reel-video-renderer></ytd-shorts>`})
      : route.abort());
    await page.goto('https://www.youtube.com/shorts/fixture0002');
    await page.evaluate(() => { globalThis.chrome = {runtime:{sendMessage:async message => {
      if (message.type !== 'channel-status') throw new Error('Unexpected write');
      return {status:'unknown'};
    }}}; });
    const root = path.resolve(__dirname, '..', 'extensions', 'chromium');
    await page.addStyleTag({content:fs.readFileSync(path.join(root,'content.css'),'utf8')});
    await page.addScriptTag({content:fs.readFileSync(path.join(root,'content.js'),'utf8')});
    const button = page.locator('.leti-cook-add-channel');
    const message = page.locator('.leti-cook-channel-message');
    await button.waitFor();
    const results = [];
    for (const right of [280, 100, 40, 280]) {
      await page.locator('reel-action-bar-view-model').evaluate((node, gap) => { node.style.right=`${gap}px`; }, right);
      await page.waitForTimeout(200);
      const neighbors = await page.locator('.native').evaluateAll(nodes => nodes.map(node => {
        const {x,y,width,height} = node.getBoundingClientRect(); return {x,y,width,height};
      }));
      await button.hover();
      await page.waitForTimeout(150);
      const geometry = await message.evaluate(node => {
        const box=node.getBoundingClientRect(), host=node.getRootNode().host.getBoundingClientRect();
        return {left:box.left,right:box.right,top:box.top,bottom:box.bottom,width:box.width,
          hostRight:host.right,hostWidth:host.width,hostHeight:host.height,viewport:innerWidth,
          scrollWidth:node.scrollWidth,clientWidth:node.clientWidth};
      });
      assert.equal(geometry.hostWidth,48); assert.equal(geometry.hostHeight,48);
      if (right === 40) {
        assert.equal(await message.isVisible(),false,'No left fallback when right space is unavailable');
        assert(await button.getAttribute('aria-label'));
      } else {
        assert(await message.isVisible());
        assert(geometry.left >= geometry.hostRight + 9,'Tooltip is right of the rail');
        assert(geometry.right <= geometry.viewport - 7,'Tooltip stays in viewport');
        assert(geometry.scrollWidth <= geometry.clientWidth,'Message wraps');
        if (right === 100) {
          assert(geometry.width <= 82);
          await message.evaluate(node => { node.textContent='LC-'+ 'LongDiagnosticToken'.repeat(12); });
          assert(await message.evaluate(node => node.scrollWidth <= node.clientWidth),'Unbroken tokens wrap');
        }
      }
      assert.deepEqual(await page.locator('.native').evaluateAll(nodes => nodes.map(node => {
        const {x,y,width,height} = node.getBoundingClientRect(); return {x,y,width,height};
      })),neighbors,'Tooltip does not move action buttons');
      results.push({railRightGap:right,visible:await message.isVisible(),...geometry});
    }
    console.log(JSON.stringify({status:'passed',scope:'synthetic Shorts tooltip geometry',results},null,2));
  } finally { await browser.close(); }
}
main().catch(error => { console.error(error); process.exitCode=1; });
