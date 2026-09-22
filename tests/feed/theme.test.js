/* Контракт темы ленты и дашборда (аудит 2026-09-22, §3.3-31).

   Страницы автономны (`src/dashboard.js` намеренно без импортов и сборки), поэтому тему
   ставят ДВА независимых модуля — `main.js::initTheme` и `dashboard.js::_applyTheme` — плюс
   общий фрагмент предотрисовки `templates/theme-init.html.j2`. Дублирование осознанное, но
   КОНТРАКТ у них общий, и расходиться ему нельзя: разойдись ключ — светлая тема не переживёт
   переход между экранами, разойдись подпись — кнопка соврёт. Молчит и то, и другое, поэтому
   контракт пришпилен здесь (сам код — DOM-скрипты, в node:test их не исполнить).

   Значения — ЛИТЕРАЛЫ договора, а не вычитка из модулей: тест обязан упасть, если одну
   сторону правят под другую. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

const read = p => readFileSync(new URL(p, import.meta.url), 'utf8');
const MODULES = {
  'main.js (лента)': read('../../src/feed/main.js'),
  'dashboard.js (дашборд)': read('../../src/dashboard.js'),
};
const PREPAINT = read('../../templates/theme-init.html.j2');

const KEY = 'feed.theme';                  /* один ключ localStorage на оба экрана */
const TO_DARK = 'Включить тёмную тему';    /* активна светлая -> кнопка предложит тёмную */
const TO_LIGHT = 'Включить светлую тему';  /* активна тёмная -> предложит светлую */

describe('контракт темы: ключ, атрибут и aria-подписи общие для ленты и дашборда', () => {
  for (const [name, src] of Object.entries(MODULES)) {
    it(`${name} читает ключ ${KEY}`, () => {
      assert.ok(src.includes(`'${KEY}'`), `${name} не читает ключ ${KEY}`);
    });

    it(`${name} переключает атрибут data-theme на <html>`, () => {
      assert.ok(src.includes('dataset.theme'), `${name} не ставит <html data-theme>`);
    });

    it(`${name} несёт обе aria-подписи кнопки #theme-toggle`, () => {
      assert.ok(src.includes(TO_DARK), `${name}: нет подписи «${TO_DARK}»`);
      assert.ok(src.includes(TO_LIGHT), `${name}: нет подписи «${TO_LIGHT}»`);
    });
  }

  it('фрагмент предотрисовки читает тот же ключ (иначе тема мигнёт тёмной)', () => {
    assert.ok(PREPAINT.includes(KEY), `theme-init не читает ключ ${KEY}`);
    assert.ok(PREPAINT.includes('dataset.theme'), 'theme-init не ставит <html data-theme>');
  });
});
