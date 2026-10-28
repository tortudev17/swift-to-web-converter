// Run with the generated LaligaBrains HTML open in playwright-cli.
async page => {
  const assert = (condition, message) => {if (!condition) throw new Error(message);};
  const walk = node => {
    if (!node || typeof node !== 'object') return [];
    return [node, ...Object.values(node).flatMap(value => Array.isArray(value) ? value.flatMap(walk) : walk(value))];
  };
  const errors = [];
  const requests = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => requests.push(request.url()));
  const initialModel = await page.evaluate(() => JSON.parse(document.getElementById('swiftweb-model').textContent));
  assert(initialModel.firebase?.path === 'main', 'Firebase main path is configured from the Swift source');
  const mockData = {
    matches: [{id: 91, eso3Score: 4, eso4Score: 2, scorers: [12, 12, 99], date: 1}],
    eso4players: [{id: 12, name: 'Ada', goals: 2, scoredWhere: [91], fastasyOwner: null}],
  };
  await page.route(`${initialModel.firebase.baseUrl}/**`, async route => {
    assert(route.request().method() === 'GET', 'Browser validation does not write to Firebase');
    await route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify(mockData)});
  });
  await page.reload();
  assert(await page.getByText('⚽ LaLiga Brains', {exact: true}).count() === 1, 'LaLigaBrains title renders');
  const model = await page.evaluate(() => JSON.parse(document.getElementById('swiftweb-model').textContent));
  const nodes = walk(model.root);
  const totalMethodTexts = nodes.filter(node => node.type === 'Text' && walk(node.text).some(value => value.op === 'call' && /^total/.test(value.name || '')));
  await page.getByText('Ada x2', {exact: true}).waitFor();
  assert(totalMethodTexts.length > 0 && await page.getByText('3', {exact: true}).count() > 0, 'Mock Firebase data hydrates model values and translated Swift total methods');
  const streakTexts = nodes.filter(node => node.type === 'Text' && walk(node.text).some(value => value.op === 'call' && /^currentStreak/.test(value.name || '')));
  assert(streakTexts.length === 2, 'Both translated streak methods are present');
  assert(await page.evaluate(() => typeof window.swiftToWebFirebase?.read === 'function' && window.swiftToWebFirebase?.write === undefined), 'Firebase exposes read access but no unused write capability');
  assert(await page.evaluate(sourceId => localStorage.getItem(`swiftweb-state:${document.title}:${sourceId}`)?.includes('Ada'), model.sourceId), 'Fetched Firebase state is cached under this source version');
  assert(nodes.some(node => node.modifiers?.some(modifier => modifier.name === 'padding')), 'SwiftUI padding modifiers are present in the converted view tree');
  assert(await page.locator('.modifier').evaluateAll(elements => elements.some(el => getComputedStyle(el).padding === '16px')), 'Default SwiftUI padding is applied in the rendered layout');
  for (const node of nodes.filter(node => node.type === 'Text' && node.text?.op === 'literal' && typeof node.text.value === 'string')) {
    const font = node.modifiers?.find(modifier => modifier.name === 'font')?.value;
    if (font?.op === 'literal' && typeof font.value === 'number') {
      const actualSize = await page.getByText(node.text.value, {exact: true}).first().evaluate(element => getComputedStyle(element).fontSize);
      assert(actualSize === `${font.value}px`, `SwiftUI font size ${font.value}px renders for ${node.text.value}`);
    }
  }
  const imageNodes = nodes.filter(node => node.type === 'Image');
  const missingNodes = nodes.filter(node => node.type === 'missing-image');
  const unsupportedNodes = nodes.filter(node => node.type === 'unsupported');
  const images = page.locator('img');
  assert(await images.count() >= imageNodes.filter(node => node.src).length, 'Referenced Xcode image assets render');
  assert(await images.evaluateAll(elements => elements.every(img => img.complete && img.naturalWidth > 0 && img.src.startsWith('data:image/png;base64,'))), 'Embedded Xcode PNG assets load offline');
  const placeholders = await page.locator('.image-missing').evaluateAll(elements => elements.map(el => el.getAttribute('aria-label')));
  assert(placeholders.length === missingNodes.length, 'Unavailable images receive compact visible placeholders');
  for (const node of missingNodes) assert(placeholders.some(text => text === node.message), `Missing image is visible and labeled: ${node.message}`);
  const warningLabel = `${model.diagnostics.length} warning${model.diagnostics.length === 1 ? '' : 's'}`;
  assert(await page.getByText(`Conversion report (${warningLabel})`, {exact: true}).count() === 1, 'Warnings are included in the report');
  const reportItems = await page.locator('#report-items li').allTextContents();
  assert(reportItems.length === model.diagnostics.length, 'Every conversion diagnostic appears in the embedded report');
  for (const diagnostic of model.diagnostics) {
    assert(reportItems.includes(`${diagnostic.file}:${diagnostic.line} — ${diagnostic.message}`), `Report includes ${diagnostic.file}:${diagnostic.line}: ${diagnostic.message}`);
  }
  const unsupportedViews = await page.locator('.unsupported').evaluateAll(elements => elements.map(el => ({text: el.textContent, location: el.title, visible: el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0})));
  assert(unsupportedViews.length === unsupportedNodes.length, 'Unsupported Swift syntax is preserved as visible placeholders');
  for (const node of unsupportedNodes) assert(unsupportedViews.some(view => view.text === node.message && view.location === node.location && view.visible), `Unsupported syntax is labeled at ${node.location}`);
  if (nodes.some(node => node.modifiers?.some(modifier => modifier.name === 'backgroundStyle'))) {
    assert(await page.locator('.modifier').evaluateAll(elements => elements.some(el => getComputedStyle(el).backgroundColor === 'rgb(0, 122, 255)')), 'backgroundStyle blue reaches no-argument background');
  }
  const capsules = nodes.filter(node => node.type === 'Text' && node.modifiers?.some(modifier => modifier.name === 'background' && modifier.shape === 'Capsule'));
  if (capsules.length) {
    const capsuleStyle = await page.getByText('0', {exact: true}).first().evaluate(element => {
      let current = element;
      while (current && !(current.classList.contains('modifier') && getComputedStyle(current).backgroundColor === 'rgb(0, 122, 255)')) current = current.parentElement;
      return current && {radius: getComputedStyle(current).borderRadius, width: current.style.width};
    });
    assert(capsuleStyle && capsuleStyle.radius === '9999px' && capsuleStyle.width === 'max-content', 'Capsule background and fixedSize render as a blue intrinsic-width pill');
  }
  const roundedBadge = nodes.find(node => node.type === 'Text' && node.modifiers?.some(modifier => modifier.shape === 'RoundedRectangle'));
  assert(roundedBadge, 'RoundedRectangle score badges are translated');
  const badgeStyle = await page.getByText('4', {exact: true}).first().evaluate(element => {
    let current = element;
    while (current && !(current.classList.contains('modifier') && getComputedStyle(current).borderRadius === '18px')) current = current.parentElement;
    return current && {weight: getComputedStyle(element).fontWeight, size: getComputedStyle(element).fontSize};
  });
  assert(badgeStyle?.weight === '700' && badgeStyle.size === '22px', 'Bold title2 score badge keeps SwiftUI font size and weight');
  assert(await page.getByText('Ada x2', {exact: true}).evaluate(element => getComputedStyle(element).textAlign === 'center'), 'Scorer text uses multiline center alignment');
  for (const label of ['puntos', 'goles']) {
    const box = await page.getByText(label, {exact: true}).first().boundingBox();
    assert(box && box.height < 25, `${label} stays on one line instead of breaking at each character`);
  }
  const fixedFrame = await page.locator('.modifier').evaluateAll(elements => {
    const frame = elements.find(el => getComputedStyle(el).width === '500px' && getComputedStyle(el).height === '300px');
    if (!frame) return null;
    const rect = frame.getBoundingClientRect();
    return {width: rect.width, height: rect.height};
  });
  assert(fixedFrame && fixedFrame.width === 500 && fixedFrame.height === 300, 'Fixed SwiftUI frame renders at 500x300 CSS points');
  const flexibleColumnCount = nodes.filter(node => node.modifiers?.some(modifier => modifier.name === 'frame' && modifier.values?.maxWidth?.value === 'infinity')).length;
  if (flexibleColumnCount) {
    const flexibleWidths = await page.locator('.modifier').evaluateAll(elements => elements
      .filter(el => el.style.width === '100%')
      .map(el => el.getBoundingClientRect().width));
    const minimumExpandedWidth = fixedFrame.width / (flexibleColumnCount + 1);
    assert(flexibleWidths.filter(width => width > minimumExpandedWidth).length >= flexibleColumnCount, 'frame(maxWidth: .infinity) expands the column stacks across the available row');
  }
  const buttonCount = nodes.filter(node => node.type === 'Button').length;
  if (buttonCount > 0) {
    assert(await page.getByRole('button').count() >= buttonCount, 'Button(action:label:) controls render');
    const branchTexts = (nodes.find(node => node.type === 'if')?.yes || []).flatMap(walk)
      .filter(node => node.type === 'Text' && node.text?.op === 'literal' && typeof node.text.value === 'string')
      .map(node => node.text.value)
      .filter(text => text.length > 0);
    const expectedTextCounts = new Map();
    for (const value of branchTexts) expectedTextCounts.set(value, (expectedTextCounts.get(value) || 0) + 1);
    assert(expectedTextCounts.size > 0, 'The conditional view exposes literal text for a visible-state check');
    const branchLabels = [...expectedTextCounts].map(([text, count]) => ({locator: page.getByText(text, {exact: true}), count}));
    for (const {locator, count} of branchLabels) {
      assert(await locator.count() === count && await locator.evaluateAll(elements => elements.every(el => {
        const style = getComputedStyle(el);
        return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) > 0;
      })), 'State-backed conditional custom view renders its source text visibly at launch');
    }
    assert(await page.locator('hr.divider').count() === 1 && await page.locator('hr.divider').isVisible(), 'SwiftUI Divider renders inside the horizontal stack');
    await page.getByRole('button').nth(1).click();
    for (const {locator} of branchLabels) assert(await locator.count() === 0, 'Button assignment reevaluates and hides the conditional custom view');
    await page.getByRole('button').first().click();
    for (const {locator, count} of branchLabels) assert(await locator.count() === count && await locator.evaluateAll(elements => elements.every(el => {
      const style = getComputedStyle(el);
      return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) > 0;
    })), 'Button assignment reevaluates and visibly restores the conditional custom view');
    const contentBox = await branchLabels[0].locator.first().boundingBox();
    assert(contentBox && contentBox.y > 180 && contentBox.y < 260, `VStack Spacer positions MatchesView content inside the 500x300 frame (y=${contentBox?.y})`);
    const branchNodes = (nodes.find(node => node.type === 'if')?.yes || []).flatMap(walk);
    const spacedTextRow = branchNodes.find(node => node.type === 'HStack'
      && node.children?.some(child => child.type === 'Spacer')
      && node.children.filter(child => child.type === 'Text' && child.text?.op === 'literal' && String(child.text.value).length > 0).length >= 2);
    if (spacedTextRow) {
      const rowTexts = spacedTextRow.children.filter(child => child.type === 'Text' && child.text?.op === 'literal' && String(child.text.value).length > 0);
      const firstBox = await page.getByText(String(rowTexts[0].text.value), {exact: true}).first().boundingBox();
      const lastBox = await page.getByText(String(rowTexts[rowTexts.length - 1].text.value), {exact: true}).first().boundingBox();
      const gap = firstBox && lastBox ? lastBox.x - (firstBox.x + firstBox.width) : 0;
      assert(gap > 40, `HStack Spacer separates its labels across the available width (gap=${gap})`);
    }
  }
  assert(await page.getByText('⚽ LaLiga Brains', {exact: true}).count() === 1, 'Button state update rerenders');
  await page.getByRole('button', {name: 'Ver todos los partidos', exact: true}).click();
  assert(await page.getByText('01/01/01', {exact: true}).count() === 1, 'List row formats the fetched Foundation reference date');
  assert(await page.getByText('4/2', {exact: true}).count() === 1, 'List row displays the fetched match score');
  await page.getByRole('button', {name: '‹ Back', exact: true}).click();
  await page.goto(page.url());
  await page.waitForTimeout(280);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({path: 'output/laligabrains/preview.png', fullPage: true});
  assert(requests.some(url => url.startsWith(`${model.firebase.baseUrl}/main.json`)), 'Firebase REST GET request is issued to the configured database path');
  assert(requests.every(url => url.startsWith('file:') || url.startsWith('data:') || url.startsWith(model.firebase.baseUrl)), 'Only the configured Firebase host receives external requests');
  assert(errors.length === 0, `Browser errors: ${errors.join(', ')}`);
  return 'PASS: LaligaBrains layout and embedded images, mocked Firebase read/model hydration and local cache, scorer grouping, optional values, rounded bold score badges, list/navigation, state updates, and Firebase-only requests (no writes).';
}
